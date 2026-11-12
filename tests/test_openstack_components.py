"""OpenStack wizard (validation, generated Kolla files, deploy job with a kolla-ansible stand-in) and component deployments."""
import json, os, pathlib

import pytest
import yaml

from conftest import FAKES, add_host, ansible_calls, mark_ready, wait_job


@pytest.fixture
def oslab(app, operator):
    ids = {}
    for name, ip, roles in [("ctl1", "10.0.0.11", ["controller"]), ("ctl2", "10.0.0.12", ["controller"]), ("ctl3", "10.0.0.13", ["controller"]),
                            ("cmp1", "10.0.0.21", ["os-compute"]), ("cmp2", "10.0.0.22", ["os-compute"]), ("sto1", "10.0.0.31", ["os-storage"])]:
        ids[name] = add_host(operator, ip, hostname=name, roles=roles)
        mark_ready(app, ids[name])
    return ids


def os_fill(operator, lab, **over):
    st = {"requirements": {"release": "2026.1", "base_distro": "ubuntu"},
          "nodes": {"controller_ids": [lab["ctl1"]], "compute_ids": [lab["cmp1"], lab["cmp2"]], "storage_ids": [lab["sto1"]], "hostname": "openstack-controller"},
          "services": {"cinder": True, "heat": True, "horizon": True},
          "network": {"network_interface": "eth0", "external_interface": "eth1", "vip": "10.0.0.50", "vlan_range": "100-200", "storage_backend": "lvm"}}
    for k, v in over.items():
        st[k].update(v)
    for k, v in st.items():
        r = operator.post(f"/api/os/wizard/{k}", json=v)
        assert r.status_code == 200, (k, r.get_json())
    return st


OS_BAD = [("requirements", {"release": "2019.1"}, "release"), ("requirements", {"base_distro": "debian"}, "base_distro"),
          ("nodes", {"controller_ids": []}, "controller_ids"), ("nodes", {"controller_ids": [1, 2]}, "controller_ids"),
          ("nodes", {"compute_ids": []}, "compute_ids"), ("nodes", {"compute_ids": [999]}, "compute_ids"),
          ("nodes", {"storage_ids": "x"}, "storage_ids"), ("nodes", {"hostname": "bad host"}, "hostname"),
          ("nodes", {"database": "postgres"}, "database"), ("nodes", {"mq": "kafka"}, "mq"),
          ("services", {"keystone": False}, "keystone"), ("services", {"nova": False}, "nova"), ("services", {"neutron": False}, "neutron"),
          ("services", {"glance": False}, "glance"), ("services", {"placement": False}, "placement"),
          ("network", {"network_interface": "eth 0"}, "network_interface"), ("network", {"external_interface": "eth0"}, "external_interface"),
          ("network", {"vip": "10.0.0.11"}, "vip"), ("network", {"vip": "x"}, "vip"), ("network", {"vlan_range": "5-1"}, "vlan_range"),
          ("network", {"storage_backend": "zfs"}, "storage_backend"), ("network", {"virt_type": "xen"}, "virt_type"),
          ("network", {"neutron_plugin": "calico"}, "neutron_plugin"), ("network", {"external_cidr": "10.1.0.1/24"}, "external_cidr"),
          ("network", {"external_cidr": "10.1.0.0/24", "external_gateway": "10.2.0.1"}, "external_gateway"),
          ("network", {"registry": "bad host:5000"}, "registry"), ("network", {"registry": "10.0.0.5:99999"}, "registry"),
          ("network", {"lvm_vg": ""}, "lvm_vg"), ("network", {"storage_backend": "nfs", "nfs_shares": ""}, "nfs_shares"),
          ("network", {"storage_backend": "nfs", "nfs_shares": "server/path"}, "nfs_shares"), ("network", {"provider_network": ""}, "provider_network")]


@pytest.mark.parametrize("step,body,field", OS_BAD)
def test_os_validation(operator, oslab, step, body, field):
    os_fill(operator, oslab)
    r = operator.post(f"/api/os/wizard/{step}", json=body)
    assert r.status_code == 400 and field in r.get_json()["fields"], r.get_json()


def test_os_three_controllers_ok(operator, oslab):
    os_fill(operator, oslab, nodes={"controller_ids": [oslab["ctl1"], oslab["ctl2"], oslab["ctl3"]]})


def test_os_lvm_needs_storage_nodes(operator, oslab):
    os_fill(operator, oslab)
    operator.post("/api/os/wizard/nodes", json={"storage_ids": []})
    r = operator.post("/api/os/wizard/network", json={"storage_backend": "lvm"})
    assert r.status_code == 400 and "storage node" in r.get_json()["fields"]["storage_backend"]


def test_os_horizon_needs_keystone_rule():
    from autoinfra import openstack
    e = openstack.validate("services", {k: True for k in openstack.SERVICES} | {"keystone": False})
    assert "keystone" in e and "horizon" in e


SERVICE_IDS = ["keystone", "glance", "nova", "neutron", "placement", "horizon", "cinder", "heat", "octavia", "ceilometer", "manila", "swift"]


@pytest.mark.parametrize("svc", SERVICE_IDS)
@pytest.mark.parametrize("on", [True, False])
def test_globals_service_flags(app, operator, oslab, svc, on):
    os_fill(operator, oslab)
    from autoinfra import openstack
    mandatory = openstack.SERVICES[svc][2]
    if mandatory and not on:
        pytest.skip("mandatory service")
    assert operator.post("/api/os/wizard/services", json={svc: on}).status_code == 200
    g = yaml.safe_load(operator.get("/api/os/preview").get_json()["globals"])
    assert g[f"enable_{svc}"] == ("yes" if on else "no")


@pytest.mark.parametrize("backend,lvm,nfs", [("lvm", "yes", "no"), ("nfs", "no", "yes"), ("none", "no", "no"), ("ceph-external", "no", "no")])
def test_globals_storage_backend(operator, oslab, backend, lvm, nfs):
    os_fill(operator, oslab, network={"storage_backend": backend, "nfs_shares": "10.0.0.31:/export/cinder"})
    g = yaml.safe_load(operator.get("/api/os/preview").get_json()["globals"])
    assert (g["enable_cinder_backend_lvm"], g["enable_cinder_backend_nfs"]) == (lvm, nfs)
    if backend == "ceph-external":
        assert g["glance_backend_ceph"] == "yes"


def test_globals_airgap_and_network(operator, oslab):
    os_fill(operator, oslab, network={"registry": "", "neutron_plugin": "openvswitch", "virt_type": "qemu"})
    g = yaml.safe_load(operator.get("/api/os/preview").get_json()["globals"])
    # default engine: Podman - no Docker anywhere; images from the master registry, distro podman SDK
    assert g["kolla_container_engine"] == "podman" and g["podman_registry"] == "10.0.0.5:5000" and g["podman_registry_insecure"] is True
    assert g["podman_sdk_python_externally_managed"] is True and g["virtualenv"] is None and "enable_docker_repo" not in g
    assert g["docker_registry"] == "10.0.0.5:5000" and g["docker_namespace"] == "openstack.kolla"
    assert g["kolla_internal_vip_address"] == "10.0.0.50" and g["neutron_plugin_agent"] == "openvswitch" and g["nova_compute_virt_type"] == "qemu"
    assert g["enable_haproxy"] == "yes" and g["enable_keepalived"] == "yes" and g["enable_proxysql"] == "yes"
    assert g["openstack_release"] == "2026.1" and g["network_interface"] == "eth0" and g["neutron_external_interface"] == "eth1"


def test_multinode_groups(operator, oslab):
    os_fill(operator, oslab, nodes={"controller_ids": [oslab["ctl1"], oslab["ctl2"], oslab["ctl3"]]})
    m = operator.get("/api/os/preview").get_json()["multinode"]
    ctl = m.split("[control]\n")[1].split("\n\n")[0].splitlines()
    assert [l.split()[0] for l in ctl] == ["ctl1", "ctl2", "ctl3"]
    assert "[network]\nctl1" in m and "[compute]\ncmp1" in m and "[storage]\nsto1" in m and "[monitoring]\nctl1" in m
    assert "ansible_ssh_private_key_file=" in m


def test_gen_passwords():
    from autoinfra import openstack
    out = openstack.gen_passwords("---\na_password:\nb_secret:\nnova_ssh_key:\nkeep: 1\n")
    d = yaml.safe_load(out)
    assert len(d["a_password"]) == 32 and d["a_password"] != d["b_secret"] and d["keep"] == 1
    assert d["nova_ssh_key"]["public_key"].startswith("ssh-rsa ")       # Kolla needs the key pairs (real lab prechecks)


def test_os_deploy_needs_kolla(operator, oslab, monkeypatch):
    from autoinfra import openstack
    monkeypatch.setattr(openstack, "kolla_share", lambda: None)      # independent of a real Kolla install on the test machine
    os_fill(operator, oslab)
    assert operator.post("/api/os/deploy", json={}).status_code == 409


def test_os_deploy_invalid_wizard(operator):
    assert operator.post("/api/os/deploy", json={}).status_code == 400


def test_os_full_deploy(app, operator, oslab, monkeypatch):
    monkeypatch.setenv("AUTOINFRA_KOLLA_SHARE", str(FAKES / "kolla-share"))
    os_fill(operator, oslab)
    r = operator.post("/api/os/deploy", json={})
    assert r.status_code == 202, r.get_json()
    j = wait_job(operator, r.get_json()["job_id"])
    assert j["state"] == "success", operator.get(f"/api/jobs/{j['id']}/log.txt").data.decode()
    d = app.tmp / "data" / "openstack" / "etc"
    calls = [json.loads(l)["action"] for l in (d / "kolla-calls.log").read_text().splitlines()]
    assert calls == ["bootstrap-servers", "prechecks", "pull", "deploy", "post-deploy"]
    multi = (d / "multinode").read_text()
    assert "[baremetal:children]" in multi and multi.index("[control]") < multi.index("[baremetal:children]")
    pw = yaml.safe_load((d / "passwords.yml").read_text())
    assert len(pw["database_password"]) == 32 and pw["nova_ssh_key"]["private_key"].startswith("-----BEGIN")
    assert oct(os.stat(d / "passwords.yml").st_mode & 0o777) == "0o600"
    first = (d / "passwords.yml").read_text()
    wait_job(operator, operator.post("/api/os/deploy", json={}).get_json()["job_id"])
    assert (d / "passwords.yml").read_text() == first           # passwords are never regenerated silently


def test_os_deploy_stops_on_failed_prechecks(app, operator, oslab, monkeypatch):
    monkeypatch.setenv("AUTOINFRA_KOLLA_SHARE", str(FAKES / "kolla-share"))
    monkeypatch.setenv("FAKE_KOLLA_RC_prechecks", "1")
    os_fill(operator, oslab)
    j = wait_job(operator, operator.post("/api/os/deploy", json={}).get_json()["job_id"])
    names = [s["name"] for s in j["steps"]]
    i = names.index("Kolla pre-checks")
    assert j["state"] == "failed" and [s["state"] for s in j["steps"]][i - 1:i + 2] == ["ok", "failed", "skipped"]


def test_os_nfs_shares_file(app, operator, oslab, monkeypatch):
    monkeypatch.setenv("AUTOINFRA_KOLLA_SHARE", str(FAKES / "kolla-share"))
    os_fill(operator, oslab, network={"storage_backend": "nfs", "nfs_shares": "10.0.0.31:/export/a, 10.0.0.32:/export/b"})
    wait_job(operator, operator.post("/api/os/deploy", json={}).get_json()["job_id"])
    assert (app.tmp / "data/openstack/etc/config/nfs_shares").read_text() == "10.0.0.31:/export/a\n10.0.0.32:/export/b\n"


def test_os_regenerate_passwords_admin(admin, app):
    assert admin.post("/api/os/passwords/regenerate").status_code == 200


# ------------------------------------------------------------------ components
COMPS = ["cloudstack-management", "cloudstack-usage", "cloudstack-agent", "systemvm-templates", "mysql", "postgresql", "oracle", "mongodb",
         "rabbitmq", "nginx", "haproxy", "nfs", "kvm-libvirt", "zabbix-server", "zabbix-agent", "ollama"]


def test_component_catalog(operator):
    r = operator.get("/api/components").get_json()
    assert [c["id"] for c in r["components"]] == COMPS and all(c["state"] == "not_installed" for c in r["components"])
    assert {c[0] for c in r["categories"]} == {"all", "core", "databases", "monitoring", "kvm", "apps", "ai"}


@pytest.mark.parametrize("cat,n", [("core", 4), ("databases", 4), ("monitoring", 2), ("kvm", 1), ("apps", 4), ("ai", 1), ("all", 16)])
def test_component_categories(operator, cat, n):
    assert len(operator.get(f"/api/components?cat={cat}").get_json()["components"]) == n


@pytest.mark.parametrize("cid", COMPS)
def test_component_playbook_exists(cid):
    from autoinfra import components, inventory
    pb = components.playbook_for(cid, {k: f["default"] for k, f in components.COMPONENTS[cid]["config"].items()})
    assert os.path.isfile(os.path.join(inventory.ansible_dir(), pb)), pb


@pytest.mark.parametrize("cid", COMPS)
def test_component_playbook_targets_its_group(cid):
    """The group the UI fills must be the one the playbook's play runs on (or a default of it)."""
    from autoinfra import components, inventory
    c = components.COMPONENTS[cid]
    pb = components.playbook_for(cid, {k: f["default"] for k, f in c["config"].items()})
    plays = yaml.safe_load(open(os.path.join(inventory.ansible_dir(), pb)))
    hosts = " ".join(str(p["hosts"]) for p in plays)
    assert c["group"] in hosts, (cid, c["group"], hosts)


@pytest.mark.parametrize("cid", COMPS)
def test_component_defaults_valid(operator, cid):
    r = operator.post(f"/api/components/{cid}/config", json={})
    if cid == "haproxy":
        assert r.status_code == 400 and "vip" in r.get_json()["fields"]
    else:
        assert r.status_code == 200, r.get_json()


CFG_BAD = [("cloudstack-management", {"ui_port": 0}), ("cloudstack-management", {"ui_port": "x"}), ("cloudstack-agent", {"bridge": "bad bridge"}),
           ("cloudstack-agent", {"libvirt_tcp": "yes"}), ("mysql", {"strategy": "mariadb"}), ("mysql", {"vip": "x"}), ("mysql", {"port": 70000}),
           ("mysql", {"data_path": "relative"}), ("mysql", {"install_path": "/a/../b"}), ("postgresql", {"port": -1}),
           ("postgresql", {"allowed_cidr": "10.0.0.1/24"}), ("postgresql", {"data_path": "data"}), ("oracle", {"oracle_base": "u01"}),
           ("mongodb", {"port": 0}), ("mongodb", {"replset": ""}), ("rabbitmq", {"user": "Bad User"}), ("rabbitmq", {"management_ui": 1}),
           ("nginx", {"ssl": "true"}), ("haproxy", {"vip": "300.1.1.1"}), ("nfs", {"primary": "export"}), ("nfs", {"allowed_cidr": "x"}),
           ("kvm-libvirt", {"nested": "no"}), ("zabbix-server", {"web_port": 0}), ("zabbix-agent", {"server": "bad host!"}),
           ("mysql", {"unknown_key": 1}), ("ollama", {"bogus": True})]


@pytest.mark.parametrize("cid,cfg", CFG_BAD)
def test_component_config_validation(operator, cid, cfg):
    r = operator.post(f"/api/components/{cid}/config", json=cfg)
    assert r.status_code == 400 and set(cfg) & set(r.get_json()["fields"]), r.get_json()


def test_component_unknown(operator):
    for u in ("/api/components/nope", ):
        assert operator.get(u).status_code == 404
    assert operator.post("/api/components/nope/deploy", json={}).status_code == 404
    assert operator.post("/api/components/nope/config", json={}).status_code == 404
    assert operator.post("/api/components/nope/status").status_code == 404


def ready_host(app, operator, ip, roles, fam="debian"):
    hid = add_host(operator, ip, roles=roles)
    mark_ready(app, hid, fam, "ubuntu" if fam == "debian" else "rocky", "24.04" if fam == "debian" else "9.4")
    return hid


DEPLOY_CASES = [  # cid, roles of hosts, n hosts, config, expected vars subset
    ("postgresql", ["database"], 1, {"port": 5433, "allowed_cidr": "10.0.0.0/24"}, {"pg_port": 5433, "pg_allowed_cidr": "10.0.0.0/24"}),
    ("mongodb", ["database"], 3, {"replset": "rsA"}, {"mongo_replset": "rsA", "mongo_port": 27017}),
    ("rabbitmq", ["mq"], 3, {"user": "mqadmin"}, {"rabbitmq_user": "mqadmin"}),
    ("nginx", ["app"], 2, {"upstream": "10.0.0.20:8080"}, {"nginx_upstream": "10.0.0.20:8080", "nginx_ssl": True}),
    ("haproxy", ["lb"], 2, {"vip": "10.0.0.20"}, {"cs_mgmt_vip": "10.0.0.20"}),
    ("nfs", ["storage"], 1, {"allowed_cidr": "10.0.0.0/24"}, {"nfs_allowed_cidrs": ["10.0.0.0/24"]}),
    ("kvm-libvirt", ["compute"], 3, {"nested": True}, {"kvm_nested": True}),
    ("zabbix-server", ["monitoring"], 1, {}, {"zabbix_web_port": 8088}),
    ("zabbix-agent", ["compute"], 4, {}, {"zabbix_server": "10.0.0.5"}),
    ("ollama", ["ai"], 1, {"model": "mistral:7b"}, {"ollama_model": "mistral:7b"}),
    ("mysql", ["database"], 1, {"strategy": "single"}, {"db_strategy": "single", "db_single": True}),
    ("mysql", ["database"], 3, {"strategy": "pxc", "vip": "10.0.0.40"}, {"db_strategy": "pxc", "db_vip": "10.0.0.40"}),
    ("mysql", ["database"], 2, {"strategy": "gtid_agent", "vip": "10.0.0.40"}, {"db_strategy": "gtid_agent"}),
    ("oracle", ["database"], 1, {"installer_zip": "LINUX.X64_193000_db_home.zip"}, {"oracle_installer": "LINUX.X64_193000_db_home.zip"}),
    ("cloudstack-agent", ["compute"], 2, {"bridge": "cloudbr1"}, {"kvm_bridge_name": "cloudbr1"}),
    ("cloudstack-management", ["management"], 1, {"ui_port": 8080}, {"cs_ui_port": 8080}),
]


@pytest.mark.parametrize("cid,roles,n,cfg,expect", DEPLOY_CASES)
@pytest.mark.parametrize("fam", ["debian", "rhel"])
def test_component_deploy(app, operator, cid, roles, n, cfg, expect, fam):
    ids = [ready_host(app, operator, f"10.0.3.{i + 1}", roles, fam) for i in range(n)]
    r = operator.post(f"/api/components/{cid}/deploy", json={"host_ids": ids, "config": cfg})
    assert r.status_code == 202, r.get_json()
    j = wait_job(operator, r.get_json()["job_id"])
    assert j["state"] == "success"
    call = ansible_calls(app)[-1]
    from autoinfra import components
    assert call["playbook"] == components.playbook_for(cid, components.config_of(cid) if False else {**{k: f["default"] for k, f in components.COMPONENTS[cid]["config"].items()}, **cfg})
    for k, v in expect.items():
        assert call["vars"][k] == v, (k, call["vars"].get(k))
    grp = components.COMPONENTS[cid]["group"]
    section = call["inventory"].split(f"[{grp}]\n")[1].split("\n\n")[0].split()
    assert len(section) == n
    assert f"autoinfra_os_family={fam}" in call["inventory"]
    c = operator.get(f"/api/components/{cid}").get_json()["component"]
    assert c["state"] == "installed" and c["hosts"] == ids and c["last_job"] == j["id"]
    acts = operator.get("/api/components").get_json()["actions"]
    assert acts[0]["status"] == "Completed" and acts[0]["component"] == cid


def test_component_deploy_failure_marks_failed(app, operator, monkeypatch):
    monkeypatch.setenv("FAKE_RC_32_postgresql_yml", "2")
    hid = ready_host(app, operator, "10.0.3.9", ["database"])
    j = wait_job(operator, operator.post("/api/components/postgresql/deploy", json={"host_ids": [hid]}).get_json()["job_id"])
    assert j["state"] == "failed" and operator.get("/api/components/postgresql").get_json()["component"]["state"] == "failed"


@pytest.mark.parametrize("cid,n", [("postgresql", 2), ("ollama", 2), ("zabbix-server", 2), ("cloudstack-usage", 2), ("mysql", 4)])
def test_component_host_count(app, operator, cid, n):
    ids = [ready_host(app, operator, f"10.0.4.{i + 1}", ["database"]) for i in range(n)]
    assert operator.post(f"/api/components/{cid}/deploy", json={"host_ids": ids}).status_code == 400


def test_haproxy_refuses_management_servers(app, operator):
    hid = add_host(operator, "10.0.7.1", hostname="m1", roles=["management", "lb"])
    mark_ready(app, hid)
    r = operator.post("/api/components/haproxy/deploy", json={"host_ids": [hid], "config": {"vip": "10.0.7.20"}})
    assert r.status_code == 400 and "8080" in r.get_json()["error"]


def test_component_mysql_ha_needs_vip(app, operator):
    ids = [ready_host(app, operator, f"10.0.4.{i + 1}", ["database"]) for i in range(2)]
    r = operator.post("/api/components/mysql/deploy", json={"host_ids": ids, "config": {"strategy": "gtid_agent", "vip": ""}})
    assert r.status_code == 400 and "VIP" in r.get_json()["error"]


def test_component_requires_ready_hosts(app, operator):
    hid = add_host(operator, "10.0.5.1", roles=["database"])
    r = operator.post("/api/components/postgresql/deploy", json={"host_ids": [hid]})
    assert r.status_code == 409
    r = operator.post("/api/components/postgresql/deploy", json={"host_ids": [hid], "force": True})
    assert r.status_code == 202
    wait_job(operator, r.get_json()["job_id"])


def test_component_role_preselection(app, operator):
    a = ready_host(app, operator, "10.0.5.1", ["database"])
    ready_host(app, operator, "10.0.5.2", ["compute"])
    r = operator.post("/api/components/postgresql/deploy", json={})
    j = wait_job(operator, r.get_json()["job_id"])
    assert operator.get("/api/components/postgresql").get_json()["component"]["hosts"] == [a]


@pytest.mark.parametrize("body", [{"host_ids": "1"}, {"host_ids": [9999]}, {"host_ids": []}])
def test_component_bad_hosts(operator, body):
    assert operator.post("/api/components/postgresql/deploy", json=body).status_code == 400


def test_component_check_mode(app, operator):
    hid = ready_host(app, operator, "10.0.5.1", ["database"])
    wait_job(operator, operator.post("/api/components/postgresql/deploy", json={"host_ids": [hid], "check": True}).get_json()["job_id"])
    assert "--check" in ansible_calls(app)[-1]["args"]


def test_component_reset_admin(app, admin):
    hid = ready_host(app, admin, "10.0.5.1", ["database"])
    wait_job(admin, admin.post("/api/components/postgresql/deploy", json={"host_ids": [hid]}).get_json()["job_id"])
    assert admin.post("/api/components/postgresql/reset").status_code == 200
    assert admin.get("/api/components/postgresql").get_json()["component"]["state"] == "not_installed"


def test_component_status_job_unreachable(app, operator):
    hid = ready_host(app, operator, "127.0.0.1", ["database"])
    from autoinfra import db
    with app.app_context():
        db.ex("UPDATE hosts SET ssh_port=1 WHERE id=?", (hid,))
    wait_job(operator, operator.post("/api/components/postgresql/deploy", json={"host_ids": [hid]}).get_json()["job_id"])
    j = wait_job(operator, operator.post("/api/components/postgresql/status").get_json()["job_id"])
    assert "unreachable" in operator.get(f"/api/jobs/{j['id']}/log.txt").data.decode()
    assert operator.get("/api/components/postgresql").get_json()["component"]["state"] == "degraded"


def test_component_secrets_reused(app, operator):
    ids = [ready_host(app, operator, f"10.0.6.{i}", ["mq"]) for i in (1, 2, 3)]
    for _ in range(2):
        wait_job(operator, operator.post("/api/components/rabbitmq/deploy", json={"host_ids": ids}).get_json()["job_id"])
    a, b = ansible_calls(app)[-2:]
    assert a["vars"]["rabbitmq_password"] == b["vars"]["rabbitmq_password"] and len(a["vars"]["rabbitmq_cookie"]) == 32


def test_ansible_run_api(app, admin, operator):
    hid = add_host(admin, "10.0.7.1")
    assert "01-common.yml" in admin.get("/api/ansible/playbooks").get_json()["playbooks"]
    assert operator.post("/api/ansible/run", json={"playbook": "01-common.yml"}).status_code == 403
    for body in ({"playbook": "../../etc/passwd"}, {"playbook": "01-common.yml", "tags": ["a b"]}, {"playbook": "01-common.yml", "host_ids": [999]},
                 {"playbook": "01-common.yml", "extra_vars": {"ansible_connection": "local"}}):
        assert admin.post("/api/ansible/run", json=body).status_code == 400
    j = wait_job(admin, admin.post("/api/ansible/run", json={"playbook": "01-common.yml", "tags": ["repo"], "check": True}).get_json()["job_id"])
    assert j["state"] == "success"
    c = ansible_calls(app)[-1]
    assert c["playbook"] == "01-common.yml" and "--tags" in c["args"] and "--check" in c["args"]



def test_globals_docker_engine_option(operator, oslab):
    os_fill(operator, oslab, network={"container_engine": "docker"})
    g = yaml.safe_load(operator.get("/api/os/preview").get_json()["globals"])
    assert g["kolla_container_engine"] == "docker" and g["enable_docker_repo"] is False and "podman_registry" not in g


def test_single_controller_without_vip(operator, oslab):
    """No free IP on the lab LAN: one controller, no HAProxy/keepalived, the controller's own IP serves the APIs."""
    os_fill(operator, oslab, network={"vip": "", "network_interface": "auto", "external_interface": "osext0"})
    p = operator.get("/api/os/preview").get_json()
    g = yaml.safe_load(p["globals"])
    assert g["kolla_internal_vip_address"] == "10.0.0.11" and g["enable_haproxy"] == "no" and g["enable_keepalived"] == "no"
    assert g["enable_proxysql"] == "no"          # would share controller-IP:3306 with MariaDB (real lab)
    assert "network_interface" not in g and g["neutron_external_interface"] == "osext0"


def test_three_controllers_need_a_vip(operator, oslab):
    os_fill(operator, oslab, nodes={"controller_ids": [oslab["ctl1"], oslab["ctl2"], oslab["ctl3"]]})
    r = operator.post("/api/os/wizard/network", json={"vip": ""})
    assert r.status_code == 400 and "vip" in r.get_json()["fields"]


def test_deploy_runs_prep_before_kolla(app, operator, oslab, monkeypatch):
    monkeypatch.setenv("AUTOINFRA_KOLLA_SHARE", str(FAKES / "kolla-share"))
    os_fill(operator, oslab)
    j = wait_job(operator, operator.post("/api/os/deploy", json={}).get_json()["job_id"])
    names = [s["name"] for s in j["steps"]]
    assert names[:2] == ["Configure repositories, NTP and base packages", "OpenStack host preparation (external interface, clashing host services)"]
    assert names.index("OpenStack host preparation (external interface, clashing host services)") < names.index("Bootstrap servers (docker, users, kernel settings)")
    from conftest import ansible_calls
    calls = ansible_calls(app)
    prep = next(c for c in calls if c["playbook"] == "50-openstack-prep.yml")
    assert prep["vars"]["os_external_interface"] == "eth1" and prep["vars"]["os_stop_host_libvirt"] is True
    assert prep["vars"]["os_proxysql_enabled"] is True
    assert "[os_nodes]" in prep["inventory"] and "[os_network]\nctl1" in prep["inventory"]


def test_detect_interfaces_writes_per_host_interface(app, operator, oslab, monkeypatch, tmp_path):
    """network_interface=auto: each node's interface comes from the node (here a fake ssh)."""
    import subprocess as sp
    from autoinfra import openstack
    os_fill(operator, oslab, network={"network_interface": "auto"})
    answers = {"10.0.0.11": "ens160", "10.0.0.21": "cloudbr0", "10.0.0.22": "cloudbr0", "10.0.0.31": "eth0"}

    def fake_run(cmd, **kw):
        ip = cmd[-2].split("@")[1]
        return sp.CompletedProcess(cmd, 0, stdout=answers[ip] + "\n", stderr="")
    monkeypatch.setattr(sp, "run", fake_run)
    logs = []
    with app.app_context():
        d = openstack.config_dir()
        assert openstack.os_detect_interfaces(logs.append, d) is True
        m = open(f"{d}/multinode").read()
    assert "ansible_host=10.0.0.21" in m and "network_interface=cloudbr0" in m and "network_interface=ens160" in m



def test_second_deployment_refused_while_one_runs(app, operator, oslab, monkeypatch):
    monkeypatch.setenv("AUTOINFRA_KOLLA_SHARE", str(FAKES / "kolla-share"))
    os_fill(operator, oslab)
    from autoinfra import db
    with app.app_context():
        jid = db.ex("INSERT INTO jobs(kind,title,state,created,by_user,meta,steps) VALUES('deploy-openstack','x','running',0,'t','{}','[]')")
    r = operator.post("/api/os/deploy", json={})
    assert r.status_code == 409 and f"job #{jid}" in r.get_json()["error"]
    with app.app_context():
        db.ex("UPDATE jobs SET state='failed' WHERE id=?", (jid,))
    assert operator.post("/api/os/deploy", json={}).status_code == 202



def test_passwords_fallback_fills_ssh_keys_and_every_value():
    """Kolla prechecks: 'grep ^[^#].*:\\s*$ passwords.yml' must find nothing (real lab: empty *_ssh_key entries)."""
    import re as _re
    from autoinfra import openstack
    tmpl = "database_password:\nnova_ssh_key:\n  private_key:\n  public_key:\nkeystone_admin_password:\n"
    out = openstack.gen_passwords(tmpl)
    # Kolla's exact check: grep '^[^#].*:\s*$', drop lines with 'ssh_key', nothing with ':' may remain
    hits = [l for l in out.splitlines() if _re.match(r"^[^#].*:\s*$", l) and "ssh_key" not in l]
    assert not hits, hits
    d = yaml.safe_load(out)
    assert d["nova_ssh_key"]["private_key"].startswith("-----BEGIN RSA PRIVATE KEY") and d["nova_ssh_key"]["public_key"].startswith("ssh-rsa ")
    assert len(d["database_password"]) == 32


def test_passwords_use_kolla_genpwd_when_installed(app, operator, oslab, monkeypatch, tmp_path):
    from autoinfra import openstack
    gp = tmp_path / "kolla-genpwd"
    gp.write_text("#!/bin/sh\nprintf 'database_password: from-genpwd\\n' > \"$2\"\n")
    gp.chmod(0o755)
    monkeypatch.setattr(openstack, "kolla_bin", lambda: str(tmp_path / "kolla-ansible"))
    monkeypatch.setenv("AUTOINFRA_KOLLA_SHARE", str(FAKES / "kolla-share"))
    os_fill(operator, oslab)
    with app.app_context():
        s = openstack.state()
        d, _ = openstack.write_config(s)
    assert open(f"{d}/passwords.yml").read() == "database_password: from-genpwd\n"



def test_localhost_never_escalates(operator, oslab):
    os_fill(operator, oslab)
    m = operator.get("/api/os/preview").get_json()["multinode"]
    assert "[deployment]\nlocalhost ansible_connection=local ansible_become=false" in m


ROOT = pathlib.Path(__file__).resolve().parent.parent
KOLLA_ROLES = pathlib.Path("/opt/airgap/venv-kolla/share/kolla-ansible/ansible/roles")
# roles behind the options the wizard offers (core services, OVN or Open vSwitch, Cinder LVM/NFS, Heat, Horizon, HAProxy/keepalived)
WIZARD_ROLES = ["cron", "fluentd", "mariadb", "memcached", "rabbitmq", "keystone", "glance", "placement", "nova", "nova-cell", "neutron",
                "ovn-controller", "ovn-db", "openvswitch", "horizon", "heat", "cinder", "iscsi", "multipathd", "proxysql", "loadbalancer"]
OPTIONAL_IMAGES = {"ironic-neutron-agent", "letsencrypt-lego", "neutron-bgp-dragent", "neutron-infoblox-ipam-agent", "neutron-metering-agent",
                   "neutron-mlnx-agent", "neutron-ovn-vpn-agent", "neutron-sriov-agent", "nova-compute-ironic", "nova-serialproxy",
                   "nova-spicehtml5proxy"}     # features the wizard does not offer


@pytest.mark.skipif(not KOLLA_ROLES.is_dir(), reason="needs an installed kolla-ansible (master/install-kolla-tools.sh)")
def test_image_list_covers_every_wizard_option():
    """Real lab job 44: 'image does not exist: .../ovn-sb-db-relay' - the hand-written list missed an image Kolla 2026.1 needs."""
    import re as _re
    need = set()
    for r in WIZARD_ROLES:
        f = KOLLA_ROLES / r / "defaults" / "main.yml"
        if f.exists():
            need |= {m.group(1) for m in _re.finditer(r'_image:\s*"\{\{\s*docker_image_url\s*\}\}([a-z0-9][a-z0-9._-]*)"', f.read_text())}
    have = {l.strip() for l in (ROOT / "online/lists/openstack-images.txt").read_text().splitlines() if l.strip() and not l.startswith("#")}
    assert not (need - have - OPTIONAL_IMAGES), sorted(need - have - OPTIONAL_IMAGES)
