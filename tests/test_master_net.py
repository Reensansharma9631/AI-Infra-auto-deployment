"""Master-side DNS/routes helper (master/autoinfra-master-net), its installer wiring and the UI hooks that call it."""
import json, os, pathlib, sqlite3, subprocess

import pytest

from conftest import add_host, wait_job
from test_cloudstack import lab  # noqa: F401  (fixture)

ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER = ROOT / "master/autoinfra-master-net"


def run_helper(tmp_path, *args, hosts=(), master_ip="10.0.0.5"):
    etc, data, units = tmp_path / "etc", tmp_path / "data", tmp_path / "units"
    for d in (etc, data, units):
        d.mkdir(exist_ok=True)
    c = sqlite3.connect(data / "autoinfra.db")
    c.execute("CREATE TABLE IF NOT EXISTS hosts(id INTEGER PRIMARY KEY, ip TEXT, hostname TEXT)")
    c.executemany("INSERT INTO hosts(ip, hostname) VALUES(?,?)", hosts)
    c.commit()
    env = dict(os.environ, AUTOINFRA_ETC=str(etc), AUTOINFRA_DATA=str(data), AUTOINFRA_UNIT_DIR=str(units), AUTOINFRA_NO_SYSTEMD="1",
               AUTOINFRA_MASTER_IP=master_ip)
    r = subprocess.run(["bash", str(HELPER), *args], capture_output=True, text=True, env=env)
    return r, etc, units


def test_dns_update_writes_inventory_records(tmp_path):
    r, etc, _ = run_helper(tmp_path, "dns-update", hosts=[("10.0.0.20", "cs-mgmt-01"), ("10.0.0.21", "kvm-01"), ("10.0.0.22", ""),
                                                            ("10.0.0.23", "bad name;rm")])
    assert r.returncode == 0, r.stdout + r.stderr
    lines = [l for l in (etc / "dns/hosts").read_text().splitlines() if not l.startswith("#")]
    assert lines[0].startswith("10.0.0.5 autoinfra-master") and "10.0.0.20 cs-mgmt-01" in lines and "10.0.0.21 kvm-01" in lines
    assert len(lines) == 3                      # empty and invalid hostnames are left out
    assert "3 host(s)" in r.stdout


def test_routes_unit_and_idempotency(tmp_path):
    r, _, units = run_helper(tmp_path, "routes", "10.0.1.21", "10.200.0.0/24", "10.201.0.0/24")
    assert r.returncode == 0 and "routes set" in r.stdout, r.stdout + r.stderr
    u = (units / "autoinfra-routes.service").read_text()
    assert "ExecStart=/usr/sbin/ip route replace 10.200.0.0/24 via 10.0.1.21" in u and "ExecStop=-/usr/sbin/ip route del 10.201.0.0/24 via 10.0.1.21" in u
    r2, _, _ = run_helper(tmp_path, "routes", "10.0.1.21", "10.200.0.0/24", "10.201.0.0/24")
    assert "routes unchanged" in r2.stdout
    r3, _, _ = run_helper(tmp_path, "routes", "10.0.1.21", "10.200.0.0/24; rm -rf /")
    assert r3.returncode != 0 and "bad cidr" in r3.stderr
    r4, _, units = run_helper(tmp_path, "routes-clear")
    assert r4.returncode == 0 and not (units / "autoinfra-routes.service").exists()


def test_installer_ships_dns_and_sudoers():
    inst = (ROOT / "master/install-autoinfra.sh").read_text()
    assert "dnsmasq" in inst and "listen-address=$MASTER_IP" in inst and "no-resolv" in inst and "addn-hosts=" in inst
    assert "/etc/sudoers.d/autoinfra" in inst and "visudo -cf" in inst and "autoinfra-master-net" in inst
    for f in ("master-ubuntu.pkgs", "master-rhel.pkgs"):
        assert "dnsmasq" in (ROOT / "online/lists" / f).read_text().split()


def test_common_role_points_nodes_at_the_master_dns():
    import yaml
    tasks = yaml.safe_load((ROOT / "ansible/roles/common/tasks/main.yml").read_text())
    deb = next(t for t in tasks if "systemd-resolved drop-in" in t["name"])
    rh = next(t for t in tasks if "NetworkManager global DNS" in t["name"])
    assert "DNS={{ master_ip }}" in deb["ansible.builtin.copy"]["content"] and "Domains=~." in deb["ansible.builtin.copy"]["content"]
    assert "servers={{ master_ip }}" in rh["ansible.builtin.copy"]["content"]
    for t in (deb, rh):
        assert "autoinfra_node_dns" in t["when"] and "os_family" in t["when"]
    handlers = {h["name"]: h for h in yaml.safe_load((ROOT / "ansible/roles/common/handlers/main.yml").read_text())}
    assert "autoinfra_services" in str(handlers["restart systemd-resolved"]["when"]) and "autoinfra_services" in str(handlers["reload NetworkManager"]["when"])


@pytest.fixture
def fake_helper(tmp_path, monkeypatch):
    log = tmp_path / "calls.log"
    h = tmp_path / "helper"
    h.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\necho ok\n")
    h.chmod(0o755)
    monkeypatch.setenv("AUTOINFRA_MASTER_NET", str(h))
    return log


def test_host_changes_refresh_dns_records(operator, fake_helper):
    hid = add_host(operator, "10.7.0.1", hostname="n7")
    assert operator.patch(f"/api/hosts/{hid}", json={"hostname": "n7b"}).status_code == 200
    assert operator.patch(f"/api/hosts/{hid}", json={"grp": "x"}).status_code == 200        # no hostname change: no refresh
    assert operator.delete(f"/api/hosts/{hid}").status_code == 200
    assert fake_helper.read_text().splitlines() == ["dns-update"] * 3


def test_deploy_sets_master_routes_for_the_internal_network(app, operator, fake_helper, cs_mock, lab):
    from test_cloudstack import fill, _single
    from autoinfra import db
    fill(operator, lab, **_single(lab))
    with app.app_context():
        db.set_setting("cs_api_url", cs_mock.url)
        db.set_setting("cs_poll_interval", 0.01)
    fake_helper.write_text("")
    j = wait_job(operator, operator.post("/api/cs/wizard/deploy", json={"force": True, "admin_password": "password"}).get_json()["job_id"], 120)
    assert j["state"] == "success", operator.get(f"/api/jobs/{j['id']}/log.txt").data.decode()
    assert j["steps"][-1]["name"].startswith("Routes from the master")
    assert "routes 10.0.1.21 10.200.0.0/24 10.201.0.0/24" in fake_helper.read_text()


def test_zone_dns_defaults_to_the_master(app, operator):
    from autoinfra import db
    with app.app_context():
        db.set_setting("master_ip", "10.0.0.5")
    s = operator.get("/api/cs/wizard").get_json()["state"]
    assert s["zone"]["dns1"] == "10.0.0.5" and s["zone"]["internal_dns1"] == "10.0.0.5"
