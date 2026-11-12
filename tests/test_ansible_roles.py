"""Ansible content: syntax of every playbook, OS-family variables, and every role template rendered by real Ansible for
Ubuntu AND RHEL facts, then checked by the real tools (haproxy -c, keepalived -t, nginx -t, json, yaml, systemd-analyze)."""
import glob, json, os, pathlib, re, shutil, subprocess, sys, time

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
ANS = ROOT / "ansible"
PLAYBOOKS = sorted(p.name for p in ANS.glob("[0-9]*.yml"))
ROLES = sorted(p.name for p in (ANS / "roles").iterdir() if p.is_dir())
TEMPLATES = sorted(str(p.relative_to(ANS / "roles")) for p in (ANS / "roles").glob("*/templates/*.j2"))
FAMILIES = {"Debian": {"distribution": "Ubuntu", "distribution_version": "24.04", "distribution_major_version": "24"},
            "RedHat": {"distribution": "AlmaLinux", "distribution_version": "9.3", "distribution_major_version": "9"}}
INV = "\n".join(["[all]", "n1 ansible_host=10.0.0.41 node_hostname=db1", "n2 ansible_host=10.0.0.42 node_hostname=db2",
                 "n3 ansible_host=10.0.0.43 node_hostname=db3"] + [f"[{g}]\nn1\nn2\nn3" for g in
                ("cs_db", "cs_mgmt", "cs_lb", "cs_kvm", "cs_nfs", "mq", "mongodb", "nginx", "postgresql", "oracle", "zabbix_server",
                 "zabbix_agent", "ollama", "kvm", "cs_usage")]) + "\n"
SAMPLE_VARS = {"db_vip": "10.0.0.40", "db_dbha_password": "x" * 20, "db_acs_admin_password": "y" * 20, "db_vrrp_pass": "abcd1234",
               "db_server_id": 1, "vrrp_iface": "eth0", "pxc_iface": "eth0", "lb_iface": "eth0", "cs_mgmt_vip": "10.0.0.20",
               "cs_mgmt_vrrp_pass": "pass1234", "master_ip": "10.0.0.5", "ntp_server": "", "airgap_repo_base": "http://10.0.0.5:8081/repo",
               "kvm_bridge_port": "eno1", "kvm_gateway": "10.0.1.1", "kvm_prefix": 24, "primary_out": "", "db_ips": ["10.0.0.41", "10.0.0.42", "10.0.0.43"],
               "nfs_allowed": ["10.0.0.0/24"], "rabbitmq_password": "r" * 20, "rabbitmq_cookie": "c" * 32, "db_client_ips": ["10.0.0.10"],
               "kvm_internal_net": {"bridge": "cloudbr1", "addresses": ["10.200.0.1/24", "10.201.0.1/24"], "nat_cidrs": ["10.200.0.0/24", "10.201.0.0/24"],
                                    "nat_exempt": ["10.0.0.41", "10.0.0.42"]},
               "autoinfra_static_routes": [{"to": "10.200.0.0/24", "via": "10.0.0.43"}, {"to": "10.201.0.0/24", "via": "10.0.0.43"},
                                           {"to": "10.9.9.0/24", "via": "10.0.0.41"}]}


@pytest.mark.parametrize("pb", PLAYBOOKS)
def test_playbook_syntax(pb, tmp_path):
    inv = tmp_path / "inv.ini"
    inv.write_text(INV)
    r = subprocess.run(["ansible-playbook", "-i", str(inv), "--syntax-check", pb], cwd=ANS, capture_output=True, text=True,
                       env=dict(os.environ, ANSIBLE_CONFIG=str(ANS / "ansible.cfg")))
    assert r.returncode == 0, r.stdout + r.stderr


def test_playbook_count():
    assert len(PLAYBOOKS) == 23 and len(ROLES) == 19


@pytest.mark.parametrize("role", [r for r in ROLES if (ANS / "roles" / r / "vars").is_dir()])
def test_vars_for_both_families(role):
    d = ANS / "roles" / role / "vars"
    deb, rh = yaml.safe_load((d / "Debian.yml").read_text()), yaml.safe_load((d / "RedHat.yml").read_text())
    assert set(deb) == set(rh), (role, set(deb) ^ set(rh))


@pytest.mark.parametrize("role", ROLES)
def test_only_builtin_modules(role):
    """No collections needed on the air-gapped master: every module is ansible.builtin.*"""
    for f in (ANS / "roles" / role).glob("*/main.yml"):
        for item in _tasks(yaml.safe_load(f.read_text()) or []):
            mods = [k for k in item if "." in k and not k.startswith("ansible.builtin.")]
            assert not mods, (f, mods)


def _tasks(items):
    for t in items if isinstance(items, list) else []:
        if isinstance(t, dict):
            yield t
            for k in ("block", "rescue", "always"):
                yield from _tasks(t.get(k, []))


@pytest.mark.parametrize("role", ROLES)
def test_apt_module_only_on_debian(role):
    """apt/dnf-specific modules must sit behind an os_family condition; everything else uses ansible.builtin.package."""
    for f in (ANS / "roles" / role / "tasks").glob("*.yml"):
        data = yaml.safe_load(f.read_text()) or []

        def walk(items, cond=""):
            for t in items:
                c = cond + " " + str(t.get("when", ""))
                for k in ("ansible.builtin.apt", "ansible.builtin.dnf", "ansible.builtin.yum"):
                    if k in t:
                        assert "Debian" in c or "RedHat" in c, (f, t.get("name"))
                for k in ("block", "rescue", "always"):
                    if k in t:
                        walk(t[k], c)
        walk(data)


@pytest.mark.parametrize("role", [r for r in ROLES if (ANS / "roles" / r / "vars").is_dir()])
def test_role_includes_family_vars(role):
    text = (ANS / "roles" / role / "tasks" / "main.yml").read_text()
    assert "include_vars" in text and "os_family" in text


@pytest.mark.parametrize("role", ROLES)
def test_services_can_be_skipped_in_test_chroots(role):
    """Every service start honours autoinfra_services (so the roles can be exercised in chroot test nodes without systemd)."""
    for f in (ANS / "roles" / role).glob("*/main.yml"):
        for t in _tasks(yaml.safe_load(f.read_text()) or []):
            svc = t.get("ansible.builtin.service") or t.get("ansible.builtin.systemd")
            if isinstance(svc, dict) and svc.get("state") in ("started", "restarted", "reloaded") and role not in ("mysql_ha", "cs_lb", "cs_mgmt"):
                assert "autoinfra_services" in str(t.get("when", "")), (f, t.get("name"))


@pytest.fixture(scope="session")
def rendered(tmp_path_factory):
    """One ansible-playbook run per OS family that renders every template with that family's vars + sample values."""
    out = {}
    for fam, facts in FAMILIES.items():
        d = tmp_path_factory.mktemp(f"render-{fam}")
        tasks = []
        for t in TEMPLATES:
            role = t.split("/")[0]
            tasks.append({"name": t, "ansible.builtin.template": {"src": str(ANS / "roles" / t), "dest": str(d / t.replace("/", "__")[:-3])},
                          "vars": {"__role": role}})
        play = [{"hosts": "n1", "connection": "local", "gather_facts": False,
                 "vars": {"ansible_facts": dict(facts, os_family=fam, selinux={"status": "disabled"}), "ansible_python_interpreter": shutil.which("python3")},
                 "pre_tasks": [{"name": "role defaults", "ansible.builtin.include_vars": {"dir": str(ANS / "roles"), "files_matching": "^main.yml$",
                                                                                           "ignore_unknown_extensions": True}, "ignore_errors": True}],
                 "tasks": []}]
        # defaults + family vars of every role, then templates
        for role in ROLES:
            for sub in ("defaults/main.yml", f"vars/{fam}.yml"):
                p = ANS / "roles" / role / sub
                if p.exists():
                    play[0]["tasks"].append({"name": f"vars {role}/{sub}", "ansible.builtin.include_vars": str(p)})
        play[0]["tasks"] += tasks
        play[0].pop("pre_tasks")
        pb = d / "render.yml"
        pb.write_text(yaml.safe_dump(play, sort_keys=False))
        inv = d / "inv.ini"
        inv.write_text(INV)
        ev = d / "ev.json"
        ev.write_text(json.dumps(SAMPLE_VARS))
        r = subprocess.run(["ansible-playbook", "-i", str(inv), "-e", "@" + str(ev), str(pb)], capture_output=True, text=True,
                           env=dict(os.environ, ANSIBLE_CONFIG=str(ANS / "ansible.cfg"), ANSIBLE_ROLES_PATH=str(ANS / "roles")))
        assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
        out[fam] = d
    return out


@pytest.mark.parametrize("fam", list(FAMILIES))
@pytest.mark.parametrize("tpl", TEMPLATES)
def test_template_renders(rendered, fam, tpl):
    f = rendered[fam] / tpl.replace("/", "__")[:-3]
    text = f.read_text()
    assert text.strip() and "{{" not in text and "{%" not in text
    name = tpl.split("/")[-1][:-3]
    if name.endswith(".json"):
        json.loads(text)
    if name.endswith((".yaml", ".yml")):
        yaml.safe_load(text)
    if name.endswith(".service"):
        assert "[Service]" in text and "ExecStart=" in text


def _real_iface():
    """keepalived -t refuses interfaces that do not exist on the machine running the test: use a real one there."""
    names = sorted(n for n in os.listdir("/sys/class/net") if n != "lo")
    return names[0] if names else "lo"


def _needs(tool):
    return pytest.mark.skipif(not shutil.which(tool), reason=f"{tool} not installed (apt-get install {tool})")


def _needs_keepalived(fn):
    """keepalived -t run as non-root disables every track script as 'insecure' and fails - the check is only real as root."""
    fn = pytest.mark.skipif(os.geteuid() != 0, reason="keepalived -t must run as root (script security); run: sudo bash tests/run-all.sh")(fn)
    return _needs("keepalived")(fn)


@_needs("haproxy")
@pytest.mark.parametrize("fam", list(FAMILIES))
def test_haproxy_config_valid(rendered, fam):
    f = rendered[fam] / "cs_lb__templates__haproxy.cfg"
    r = subprocess.run(["haproxy", "-c", "-f", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@_needs_keepalived
@pytest.mark.parametrize("fam", list(FAMILIES))
@pytest.mark.parametrize("tpl", ["mysql_ha__templates__keepalived.conf", "pxc__templates__keepalived.conf", "cs_lb__templates__keepalived-base.conf"])
def test_keepalived_config_valid(rendered, fam, tpl, tmp_path):
    src = (rendered[fam] / tpl).read_text()
    if tpl.startswith("cs_lb"):
        vip = (rendered[fam] / "cs_lb__templates__mgmt-vip.conf").read_text()
        (tmp_path / "conf.d").mkdir()
        (tmp_path / "conf.d" / "mgmt-vip.conf").write_text(re.sub(r"\beth0\b", _real_iface(), vip))
        src = re.sub(r"include\s+\S+", f"include {tmp_path}/conf.d/*.conf", src)
    # health-check scripts live on the target nodes; keepalived -t insists they exist here
    src = re.sub(r'^(\s*)script\s+.*$', r'\1script "/bin/true"', src, flags=re.M)
    cfg = tmp_path / "k.conf"
    cfg.write_text(re.sub(r"\beth0\b", _real_iface(), src))
    r = subprocess.run(["keepalived", "-t", "-f", str(cfg)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr + src


@_needs("nginx")
@pytest.mark.parametrize("fam", list(FAMILIES))
@pytest.mark.parametrize("ssl", [True, False])
def test_nginx_site_valid(rendered, fam, ssl, tmp_path):
    site = (rendered[fam] / "nginx_proxy__templates__site.conf").read_text()
    if not ssl:
        site = site.split("    return 301")[0] + "    location / { proxy_pass http://autoinfra_backend; }\n}\n"
    crt, key = tmp_path / "c.crt", tmp_path / "c.key"
    subprocess.run(["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "1", "-keyout", str(key), "-out", str(crt), "-subj", "/CN=t"],
                   check=True, capture_output=True)
    site = site.replace("/etc/nginx/autoinfra-tls/server.crt", str(crt)).replace("/etc/nginx/autoinfra-tls/server.key", str(key))
    if os.geteuid() != 0:   # nginx -t binds the listen ports; non-root cannot bind <1024
        site = re.sub(r"\blisten\s+(80|443)\b", lambda m: f"listen {18000 + int(m.group(1))}", site)
    conf = tmp_path / "nginx.conf"
    conf.write_text(f"pid {tmp_path}/n.pid; error_log {tmp_path}/e.log; events {{}} http {{ access_log off; {site} }}")
    r = subprocess.run(["nginx", "-t", "-c", str(conf), "-p", str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("fam", list(FAMILIES))
def test_dbha_config_matches_family(rendered, fam):
    c = json.loads((rendered[fam] / "mysql_ha__templates__dbha-config.json").read_text())
    svc = "mysql" if fam == "Debian" else "mysqld"
    assert c["restart_cmd"] == ["systemctl", "restart", svc] and len(c["nodes"]) == 3
    assert c["socket"] == ("/var/run/mysqld/mysqld.sock" if fam == "Debian" else "/var/lib/mysql/mysql.sock")


@pytest.mark.parametrize("fam", list(FAMILIES))
def test_repo_files(rendered, fam):
    lst = (rendered[fam] / "airgap_repo__templates__airgap.list").read_text()
    repo = (rendered[fam] / "airgap_repo__templates__airgap.repo").read_text()
    assert "deb [trusted=yes] http://10.0.0.5:8081/repo/cloudstack/deb ./" in lst
    assert "baseurl=http://10.0.0.5:8081/repo/cloudstack/rpm/" in repo and "gpgcheck=0" in repo
    import configparser
    cp = configparser.ConfigParser()
    cp.read_string(repo)
    assert "autoinfra-core" in cp.sections()


@pytest.mark.parametrize("fam", list(FAMILIES))
def test_chrony_points_to_master(rendered, fam):
    assert "server 10.0.0.5 iburst" in (rendered[fam] / "common__templates__chrony.conf").read_text()


@pytest.mark.parametrize("fam", list(FAMILIES))
def test_pxc_cluster_address(rendered, fam):
    t = (rendered[fam] / "pxc__templates__pxc.cnf").read_text()
    assert "wsrep_cluster_address = gcomm://10.0.0.41,10.0.0.42,10.0.0.43" in t and "wsrep_node_address = 10.0.0.41" in t


@pytest.mark.parametrize("fam", list(FAMILIES))
def test_mongod_conf(rendered, fam):
    c = yaml.safe_load((rendered[fam] / "mongodb__templates__mongod.conf").read_text())
    assert c["net"]["port"] == 27017 and c["replication"]["replSetName"] == "rs0"
    assert c["storage"]["dbPath"] == ("/var/lib/mongodb" if fam == "Debian" else "/var/lib/mongo")


def test_systemd_units_verify(rendered, tmp_path):
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze not available")
    for fam in FAMILIES:
        for n in ("ollama__templates__ollama.service", "mysql_ha__templates__dbha-agent.service"):
            u = tmp_path / (fam + "-" + n.split("__")[-1])
            u.write_text((rendered[fam] / n).read_text().replace("/usr/local/bin/ollama", "/bin/true").replace("/usr/bin/python3", "/bin/true"))
            r = subprocess.run(["systemd-analyze", "verify", str(u)], capture_output=True, text=True)
            assert "Invalid" not in r.stderr and "unknown" not in r.stderr.lower().replace("unknown lvalue 'startlimit", ""), r.stderr


@_needs_keepalived
def test_single_lb_node_keepalived_valid(tmp_path):
    """One HAProxy node: no unicast peers may be emitted (keepalived -t rejects an empty unicast_peer block)."""
    inv = tmp_path / "inv.ini"
    inv.write_text("[all]\nlb1 ansible_host=10.0.0.10\n[cs_lb]\nlb1\n")
    play = [{"hosts": "lb1", "connection": "local", "gather_facts": False, "tasks": [
        {"ansible.builtin.include_vars": str(ANS / "roles/cs_lb/defaults/main.yml")},
        {"ansible.builtin.template": {"src": str(ANS / "roles/cs_lb/templates/mgmt-vip.conf.j2"), "dest": str(tmp_path / "vip.conf")}}]}]
    pb = tmp_path / "p.yml"
    pb.write_text(yaml.safe_dump(play))
    r = subprocess.run(["ansible-playbook", "-i", str(inv), "-e", json.dumps({"lb_iface": "eth0", "cs_mgmt_vip": "10.0.0.20", "cs_mgmt_vrrp_pass": "abcd1234"}), str(pb)],
                       capture_output=True, text=True, env=dict(os.environ, ANSIBLE_CONFIG=str(ANS / "ansible.cfg")))
    assert r.returncode == 0, r.stdout[-2000:]
    vip = (tmp_path / "vip.conf").read_text()
    assert "unicast_peer" not in vip
    conf = tmp_path / "k.conf"
    conf.write_text("global_defs {\n enable_script_security\n script_user root\n}\n" + re.sub(r'^(\s*)script\s+.*$', r'\1script "/bin/true"', re.sub(r"\beth0\b", _real_iface(), vip), flags=re.M))
    r = subprocess.run(["keepalived", "-t", "-f", str(conf)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("role", [r for r in ROLES if r != "airgap_repo"])
def test_every_role_refreshes_master_repos(role):
    """A repository uploaded after a node was set up must be visible to every later deployment."""
    meta = yaml.safe_load((ANS / "roles" / role / "meta" / "main.yml").read_text())
    assert {"role": "airgap_repo"} in meta["dependencies"]


ROLE_PKG_VARS = {"kvm_host": ["kvm_pkgs"], "mysql_ha": ["mysql_pkgs"], "mysql_single": ["mysql_pkgs"], "pxc": ["pxc_pkgs"], "cs_lb": ["lb_pkgs"],
                 "cs_mgmt": ["cs_mgmt_pkgs"], "nfs_server": ["nfs_pkgs"], "nginx_proxy": ["nginx_pkgs"], "postgresql": ["pg_pkgs"],
                 "mongodb": ["mongo_pkgs"], "oracle_prereq": ["oracle_pkgs"], "rabbitmq": ["rabbitmq_pkgs"], "zabbix_agent": ["zabbix_agent_pkgs"],
                 "zabbix_server": ["zabbix_server_pkgs"], "common": ["os_pkgs"]}


@pytest.mark.parametrize("fam,key", [("Debian", "deb"), ("RedHat", "rpm")])
@pytest.mark.parametrize("role", sorted(ROLE_PKG_VARS))
def test_download_kits_cover_role_packages(role, fam, key):
    """Every package a role installs is fetched by some download kit (product catalog) or is part of the OS base list -
    otherwise an air-gapped deployment would stop with 'No package matching ...'."""
    sys_path = str(ROOT / "ui")
    import sys
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    from autoinfra import catalog
    known = {p for prod in catalog.PRODUCTS.values() for c in prod["components"] for p in c.get(key, [])}
    base = yaml.safe_load((ANS / "roles/common/vars" / f"{fam}.yml").read_text())["os_pkgs"]
    known |= set(base) | set(catalog.BASE_EXTRA.get(key, []))
    v = yaml.safe_load((ANS / "roles" / role / "vars" / f"{fam}.yml").read_text())
    for var in ROLE_PKG_VARS[role]:
        missing = [p for p in v[var] if p not in known]
        assert not missing, f"{role} {fam}: {missing} not in any download kit"


# ---------------------------------------------------------------- production audit fixes
@pytest.mark.parametrize("kind", ["firewalld", "ufw", "none"])
def test_firewall_helper_opens_ports_only_when_a_firewall_is_active(tmp_path, kind):
    """Real ansible-playbook on localhost with stand-in firewall-cmd / ufw binaries that record their arguments."""
    b = tmp_path / "bin"; b.mkdir(); rec = tmp_path / "calls.txt"; rec.write_text("")
    (b / "firewall-cmd").write_text(f"#!/bin/sh\n[ \"$1\" = --state ] && exit {0 if kind == 'firewalld' else 1}\necho firewall-cmd \"$@\" >> {rec}\n")
    (b / "ufw").write_text(f"#!/bin/sh\n[ \"$1\" = status ] && {{ echo 'Status: {'active' if kind == 'ufw' else 'inactive'}'; exit 0; }}\necho ufw \"$@\" >> {rec}\n")
    for f in b.iterdir():
        f.chmod(0o755)
    pb = tmp_path / "pb.yml"
    pb.write_text(yaml.safe_dump([{"hosts": "localhost", "connection": "local", "gather_facts": False, "tasks": [
        {"ansible.builtin.include_tasks": str(ANS / "roles/airgap_repo/tasks/firewall.yml"),
         "vars": {"fw_ports": ["3306/tcp", "5900-6100/tcp"], "fw_protocols": ["vrrp"]}}]}]))
    r = subprocess.run(["ansible-playbook", "-i", "localhost,", str(pb)], capture_output=True, text=True,
                       env=dict(os.environ, PATH=f"{b}:{os.environ['PATH']}", ANSIBLE_NOCOLOR="1"))
    assert r.returncode == 0, r.stdout + r.stderr
    calls = rec.read_text().splitlines()
    if kind == "firewalld":
        assert calls == ["firewall-cmd -q --permanent --add-port=3306/tcp", "firewall-cmd -q --permanent --add-port=5900-6100/tcp",
                         "firewall-cmd -q --permanent --add-protocol=vrrp", "firewall-cmd -q --reload"]
    elif kind == "ufw":
        assert calls == ["ufw allow 3306/tcp", "ufw allow 5900:6100/tcp", "ufw allow proto vrrp from any to any"]
    else:
        assert calls == []


@pytest.mark.parametrize("role", ["mysql_ha", "mysql_single", "pxc", "cs_mgmt", "cs_lb", "nfs_server", "kvm_host", "rabbitmq", "nginx_proxy",
                                  "postgresql", "mongodb", "zabbix_server", "zabbix_agent", "ollama"])
def test_network_roles_open_their_ports_first(role):
    tasks = yaml.safe_load((ANS / "roles" / role / "tasks/main.yml").read_text())
    t = tasks[0]
    assert t["ansible.builtin.include_role"] == {"name": "airgap_repo", "tasks_from": "firewall"} and t["vars"]["fw_ports"]


@pytest.mark.parametrize("role", ["mysql_ha", "mysql_single"])
def test_mysqld_binary_path_per_os(role):
    deb = yaml.safe_load((ANS / "roles" / role / "vars/Debian.yml").read_text())
    rh = yaml.safe_load((ANS / "roles" / role / "vars/RedHat.yml").read_text())
    assert deb["mysqld_bin"] == "/usr/sbin/mysqld" and rh["mysqld_bin"] == "/usr/libexec/mysqld"
    assert "/usr/sbin/mysqld " not in (ANS / "roles" / role / "tasks/main.yml").read_text()


def test_mysql_ha_delegates_to_inventory_name_not_ip(tmp_path):
    """The primary is discovered as an IP; the delegate expression must give back the inventory host name."""
    inv = tmp_path / "inv"
    inv.write_text("[all]\ndb1 ansible_host=10.0.0.41\ndb2 ansible_host=10.0.0.42\n[cs_db]\ndb1\ndb2\n")
    expr = re.search(r'delegate_to: "(.*)"', (ANS / "roles/mysql_ha/tasks/main.yml").read_text()).group(1)
    pb = tmp_path / "pb.yml"
    pb.write_text(yaml.safe_dump([{"hosts": "localhost", "connection": "local", "gather_facts": False,
                                   "vars": {"primary_out": {"stdout": "10.0.0.42\n"}},
                                   "tasks": [{"ansible.builtin.debug": {"msg": "DELEGATE=" + expr}}]}]))
    r = subprocess.run(["ansible-playbook", "-i", str(inv), str(pb)], capture_output=True, text=True, env=dict(os.environ, ANSIBLE_NOCOLOR="1"))
    assert r.returncode == 0 and "DELEGATE=db2" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- automatic KVM bridge (autoinfra-bridge)
BRIDGE = ANS / "roles/kvm_host/files/autoinfra-bridge.py"
NP_STATIC = """network:
  version: 2
  ethernets:
    ens3:
      addresses: [10.0.0.21/24]
      routes:
        - {to: default, via: 10.0.0.1}
        - {to: 192.168.50.0/24, via: 10.0.0.254}
      nameservers: {addresses: [10.0.0.1]}
    ens4: {dhcp4: false}
"""
NP_DHCP = "network:\n  version: 2\n  ethernets:\n    ens3:\n      dhcp4: true\n"
NP_BOND = """network:
  version: 2
  ethernets:
    eno1: {dhcp4: false}
    eno2: {dhcp4: false}
  bonds:
    bond0:
      interfaces: [eno1, eno2]
      parameters: {mode: 802.3ad}
      addresses: [10.0.0.21/24]
      gateway4: 10.0.0.1
"""


def bridge_env(tmp_path, dev="ens3", kind="", netplan_files=None, nm=False, extra_links=(), bridge_has_ip=False):
    """Fake root with stub ip / nmcli / resolvectl; the REAL netplan validates what the tool writes (netplan generate)."""
    root = tmp_path / "root"; b = tmp_path / "bin"; b.mkdir(parents=True)
    for d in ("etc/netplan", "run", "etc/cloud/cloud.cfg.d", "var/lib"):
        (root / d).mkdir(parents=True)
    for name, text in (netplan_files or {}).items():
        (root / "etc/netplan" / name).write_text(text)
    carrier = "cloudbr0" if bridge_has_ip else dev
    addr = [{"ifname": "lo", "addr_info": [{"local": "127.0.0.1", "prefixlen": 8}]},
            {"ifname": carrier, "addr_info": [{"local": "10.0.0.21", "prefixlen": 24}]}]
    links = [{"ifname": "lo", "address": "00:00:00:00:00:00", "mtu": 65536},
             {"ifname": dev, "address": "52:54:00:aa:bb:01", "mtu": 1500, **({"linkinfo": {"info_kind": kind}} if kind else {})}]
    links += [{"ifname": n, "address": "52:54:00:aa:bb:09", "mtu": 1500} for n in extra_links]
    if bridge_has_ip:
        links.append({"ifname": "cloudbr0", "address": "52:54:00:aa:bb:01", "mtu": 1500, "linkinfo": {"info_kind": "bridge"}})
    route = [{"dst": "default", "gateway": "10.0.0.1", "dev": carrier}]
    known = " ".join(l["ifname"] for l in links)
    (b / "ip").write_text(f"""#!/bin/sh
case "$*" in
  "-j -4 addr show") echo '{json.dumps(addr)}';;
  "-j -d link show") echo '{json.dumps(links)}';;
  "-j -4 route show default") echo '{json.dumps(route)}';;
  "link show "*) for l in {known}; do [ "$3" = "$l" ] && exit 0; done; exit 1;;
esac
""")
    (b / "resolvectl").write_text("#!/bin/sh\necho 'Link 2 (x): 10.0.0.1 10.0.0.2 fe80::1'\n")
    rec = tmp_path / "calls.txt"; rec.write_text("")
    env = dict(os.environ, AUTOINFRA_BRIDGE_ROOT=str(root), PATH=f"{b}:{os.environ['PATH']}")
    if nm:
        prof = root / "etc/NetworkManager/system-connections"; prof.mkdir(parents=True)
        (prof / "ens3.nmconnection").write_text("[connection]\nid=ens3\n[ipv4]\nmethod=auto\n")
        (b / "nmcli").write_text(f"""#!/bin/sh
case "$*" in
  "-t -f RUNNING general") echo running;;
  "-g GENERAL.CONNECTION device show ens3") echo ens3;;
  "-g GENERAL.CONNECTION device show "*) ;;
  "-t -f NAME,FILENAME connection show") echo "ens3:{prof}/ens3.nmconnection";;
  *) echo "nmcli $*" >> {rec}; case "$*" in "connection modify ens3"*) echo "[connection]\\nid=ens3\\nmaster=cloudbr0" > {prof}/ens3.nmconnection;; esac;;
esac
""")
    else:
        real = shutil.which("netplan")
        (b / "netplan").write_text(f"#!/bin/sh\n[ \"$1\" = apply ] && {{ echo \"netplan apply\" >> {rec}; exit 0; }}\nexec {real} \"$@\"\n")
    for f in b.iterdir():
        f.chmod(0o755)
    return root, env, rec


def bridge_run(env, *args):
    return subprocess.run([sys.executable, str(BRIDGE), *args], capture_output=True, text=True, env=env)


needs_netplan = pytest.mark.skipif(not shutil.which("netplan"), reason="netplan.io not installed")


def test_bridge_plan_detects_nic_prefix_gateway_dns_mac(tmp_path):
    root, env, _ = bridge_env(tmp_path, netplan_files={"50-cloud-init.yaml": NP_STATIC})
    p = json.loads(bridge_run(env, "plan", "--ip", "10.0.0.21").stdout)
    assert (p["dev"], p["prefix"], p["gateway"], p["dns"], p["mac"], p["status"], p["backend"]) == \
           ("ens3", 24, "10.0.0.1", ["10.0.0.1", "10.0.0.2"], "52:54:00:aa:bb:01", "needed", "netplan")
    assert not list((root / "var/lib").iterdir())                       # plan changes nothing


def test_bridge_plan_leaves_an_existing_bridge_alone(tmp_path):
    root, env, _ = bridge_env(tmp_path, netplan_files={"50.yaml": NP_STATIC}, bridge_has_ip=True)
    assert json.loads(bridge_run(env, "plan", "--ip", "10.0.0.21").stdout)["status"] == "present"
    assert bridge_run(env, "write", "--ip", "10.0.0.21").returncode == 0
    assert (root / "etc/netplan/50.yaml").read_text() == NP_STATIC and not (root / "etc/netplan/60-autoinfra-cloudbr.yaml").exists()


@pytest.mark.parametrize("case,expect", [("wrong-ip", "no interface"), ("other-bridge", "already on another bridge"), ("no-manager", "by hand"),
                                         ("extra-missing", "does not exist"), ("extra-same-nic", "carries an IP")])
def test_bridge_plan_refuses_what_it_cannot_do_safely(tmp_path, case, expect):
    kw = {"netplan_files": {"50.yaml": NP_STATIC}}
    args = ["plan", "--ip", "10.0.0.21"]
    if case == "wrong-ip":
        args = ["plan", "--ip", "10.9.9.9"]
    elif case == "other-bridge":
        kw.update(dev="br0", kind="bridge")
    elif case == "no-manager":
        kw = {}
    elif case == "extra-missing":
        args += ['--extra={"cloudbr1": "eno9"}']
    elif case == "extra-same-nic":
        args += ['--extra={"cloudbr1": "ens3"}']
    root, env, _ = bridge_env(tmp_path, **kw)
    p = json.loads(bridge_run(env, *args).stdout)
    assert p["status"] == "error" and expect in " ".join(p["problems"]), p
    assert bridge_run(env, "write", *args[1:]).returncode == 1 and not (root / "etc/netplan/60-autoinfra-cloudbr.yaml").exists()


@needs_netplan
@pytest.mark.parametrize("name,src,dev,kind", [("static", NP_STATIC, "ens3", ""), ("dhcp", NP_DHCP, "ens3", ""), ("bond", NP_BOND, "bond0", "bond")])
def test_bridge_write_netplan_moves_the_ip_to_the_bridge(tmp_path, name, src, dev, kind):
    root, env, _ = bridge_env(tmp_path, dev=dev, kind=kind, netplan_files={"50-cloud-init.yaml": src})
    r = bridge_run(env, "write", "--ip", "10.0.0.21")
    assert r.returncode == 0, r.stdout + r.stderr                       # includes a real `netplan generate` of the result
    old = yaml.safe_load((root / "etc/netplan/50-cloud-init.yaml").read_text())["network"]
    sec = "bonds" if kind == "bond" else "ethernets"
    nic = old[sec][dev]
    assert nic["dhcp4"] is False and not any(k in nic for k in ("addresses", "routes", "gateway4", "nameservers"))
    if kind == "bond":
        assert nic["interfaces"] == ["eno1", "eno2"] and nic["parameters"] == {"mode": "802.3ad"}        # the bond itself is untouched
    br = yaml.safe_load((root / "etc/netplan/60-autoinfra-cloudbr.yaml").read_text())["network"]["bridges"]["cloudbr0"]
    assert br["interfaces"] == [dev] and br["addresses"] == ["10.0.0.21/24"] and br["macaddress"] == "52:54:00:aa:bb:01"
    assert {"to": "default", "via": "10.0.0.1"} in br["routes"] and br["nameservers"] == {"addresses": ["10.0.0.1", "10.0.0.2"]}
    assert br["parameters"] == {"stp": False, "forward-delay": 0}
    if name == "static":
        assert {"to": "192.168.50.0/24", "via": "10.0.0.254"} in br["routes"]                           # static routes follow the IP
    assert (root / "var/lib/autoinfra-bridge/backup/netplan/50-cloud-init.yaml").read_text() == src
    assert "disabled" in (root / "etc/cloud/cloud.cfg.d/99-autoinfra-disable-network-config.cfg").read_text()
    # the generated systemd-networkd units really put the address on the bridge and enslave the NIC
    units = {f.name: f.read_text() for f in (root / "run/systemd/network").iterdir()}
    assert any("Address=10.0.0.21/24" in t and "Name=cloudbr0" in t for t in units.values()), units.keys()
    assert any("Bridge=cloudbr0" in t and f"Name={dev}" in t for t in units.values())
    assert not any("Address=10.0.0.21/24" in t and f"Name={dev}" in t for t in units.values())


@needs_netplan
def test_bridge_extra_bridge_on_second_nic(tmp_path):
    root, env, _ = bridge_env(tmp_path, netplan_files={"50.yaml": NP_STATIC}, extra_links=("ens4",))
    assert bridge_run(env, "write", "--ip", "10.0.0.21", '--extra={"cloudbr1": "ens4"}').returncode == 0
    net = yaml.safe_load((root / "etc/netplan/60-autoinfra-cloudbr.yaml").read_text())["network"]
    assert net["bridges"]["cloudbr1"]["interfaces"] == ["ens4"] and "addresses" not in net["bridges"]["cloudbr1"]


@needs_netplan
def test_bridge_netplan_rejecting_the_config_restores_everything(tmp_path):
    root, env, _ = bridge_env(tmp_path, netplan_files={"50.yaml": NP_STATIC, "99-broken.yaml": "network:\n  version: 2\n  bogus_key: 1\n"})
    r = bridge_run(env, "write", "--ip", "10.0.0.21")
    assert r.returncode != 0 and "old files restored" in r.stderr
    assert (root / "etc/netplan/50.yaml").read_text() == NP_STATIC and not (root / "etc/netplan/60-autoinfra-cloudbr.yaml").exists()


@needs_netplan
def test_bridge_apply_rolls_back_without_confirmation(tmp_path):
    root, env, rec = bridge_env(tmp_path, netplan_files={"50.yaml": NP_STATIC})
    assert bridge_run(env, "write", "--ip", "10.0.0.21").returncode == 0
    r = bridge_run(env, "apply", "--timeout", "1")
    assert r.returncode == 1 and "ROLLBACK done" in r.stdout
    assert rec.read_text().splitlines() == ["netplan apply", "netplan apply"]                 # switch, then switch back
    assert (root / "etc/netplan/50.yaml").read_text() == NP_STATIC
    assert not (root / "etc/netplan/60-autoinfra-cloudbr.yaml").exists()
    assert not (root / "etc/cloud/cloud.cfg.d/99-autoinfra-disable-network-config.cfg").exists()


@needs_netplan
def test_bridge_apply_keeps_the_config_when_the_master_confirms(tmp_path):
    root, env, rec = bridge_env(tmp_path, netplan_files={"50.yaml": NP_STATIC})
    assert bridge_run(env, "write", "--ip", "10.0.0.21").returncode == 0
    p = subprocess.Popen([sys.executable, str(BRIDGE), "apply", "--timeout", "8"], stdout=subprocess.PIPE, text=True, env=env)
    time.sleep(1)
    # before the switch is visible the confirmation is refused (IP still on ens3) ...
    assert bridge_run(env, "confirm", "--ip", "10.0.0.21").returncode == 1
    # ... after it, the master's confirm keeps the config
    _, env2, _ = bridge_env(tmp_path / "after", netplan_files={}, bridge_has_ip=True)
    env2["AUTOINFRA_BRIDGE_ROOT"] = str(root)
    assert json.loads(bridge_run(env2, "confirm", "--ip", "10.0.0.21").stdout)["ok"] is True
    out, _ = p.communicate(timeout=20)
    assert p.returncode == 0 and "confirmed by the master" in out and "ROLLBACK" not in out
    assert rec.read_text().splitlines() == ["netplan apply"] and (root / "etc/netplan/60-autoinfra-cloudbr.yaml").exists()


def test_bridge_networkmanager_commands_and_rollback(tmp_path):
    """RHEL family: nmcli is a stand-in that records its arguments (NetworkManager itself is not available here)."""
    root, env, rec = bridge_env(tmp_path, nm=True)
    p = json.loads(bridge_run(env, "plan", "--ip", "10.0.0.21").stdout)
    assert p["backend"] == "nm" and p["status"] == "needed"
    assert bridge_run(env, "write", "--ip", "10.0.0.21").returncode == 0
    assert rec.read_text() == ""                                                               # write prepares only
    r = bridge_run(env, "apply", "--timeout", "1")
    calls = rec.read_text().splitlines()
    assert calls[0] == ("nmcli connection add type bridge ifname cloudbr0 con-name cloudbr0 bridge.stp no connection.autoconnect yes "
                        "connection.autoconnect-slaves 1 ipv6.method disabled ipv4.method manual ipv4.addresses 10.0.0.21/24 "
                        "ipv4.gateway 10.0.0.1 ipv4.dns 10.0.0.1,10.0.0.2 bridge.mac-address 52:54:00:aa:bb:01")
    assert calls[1] == "nmcli connection modify ens3 connection.master cloudbr0 connection.slave-type bridge connection.autoconnect yes"
    assert calls[2:4] == ["nmcli connection up cloudbr0", "nmcli connection up ens3"]
    # no confirmation -> the bridge is deleted and the original profile file comes back byte for byte
    assert r.returncode == 1 and calls[4:] == ["nmcli connection delete cloudbr0", "nmcli connection reload", "nmcli connection up ens3"]
    assert (root / "etc/NetworkManager/system-connections/ens3.nmconnection").read_text() == "[connection]\nid=ens3\n[ipv4]\nmethod=auto\n"


def test_kvm_role_bridge_tasks_are_wired():
    main = (ANS / "roles/kvm_host/tasks/main.yml").read_text()
    assert "include_tasks: bridge.yml" in main and "kvm_manage_network | bool" in main
    names = [t["name"] for t in yaml.safe_load((ANS / "roles/kvm_host/tasks/bridge.yml").read_text())]
    assert names.index("Detect the current network (changes nothing)") < names.index("Stop when the bridge cannot be created automatically") < names.index("Create the bridge")


@needs_netplan
@pytest.mark.skipif(os.geteuid() != 0, reason="installs the helper into /usr/local/sbin")
def test_bridge_tasks_run_under_real_ansible(tmp_path):
    """bridge.yml through real ansible-playbook (local connection, fake root, switch itself disabled)."""
    root, env, _ = bridge_env(tmp_path, netplan_files={"50.yaml": NP_STATIC}, extra_links=("ens4",))
    pb = tmp_path / "pb.yml"
    pb.write_text(yaml.safe_dump([{"hosts": "kvm1", "connection": "local", "gather_facts": False, "tasks": [
        {"ansible.builtin.include_vars": str(ANS / "roles/kvm_host/defaults/main.yml")},
        {"ansible.builtin.include_tasks": str(ANS / "roles/kvm_host/tasks/bridge.yml"),
         "vars": {"kvm_network_apply": False, "kvm_extra_bridges": {"cloudbr1": "ens4"}}}]}]))
    inv = tmp_path / "inv"; inv.write_text("kvm1 ansible_host=10.0.0.21\n")
    shutil.copytree(ANS / "roles/kvm_host/files", tmp_path / "files")           # outside a role, files are searched next to the playbook
    try:
        r = subprocess.run(["ansible-playbook", "-i", str(inv), str(pb)], capture_output=True, text=True, env=dict(env, ANSIBLE_NOCOLOR="1"),
                           cwd=str(ANS / "roles/kvm_host"))
    finally:
        if os.path.exists("/usr/local/sbin/autoinfra-bridge"):
            os.remove("/usr/local/sbin/autoinfra-bridge")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "10.0.0.21/24 on ens3 (ethernet), gateway 10.0.0.1, DNS 10.0.0.1,10.0.0.2, managed by netplan -> bridge cloudbr0: needed" in r.stdout
    net = yaml.safe_load((root / "etc/netplan/60-autoinfra-cloudbr.yaml").read_text())["network"]
    assert set(net["bridges"]) == {"cloudbr0", "cloudbr1"}


# ---------------------------------------------------------------- internal pod/public network, static routes, root growing
def _unit_exec(text):
    return [l.split("=", 1)[1] for l in text.splitlines() if l.startswith("ExecStart=")]


@pytest.mark.parametrize("fam", list(FAMILIES))
def test_internal_net_unit(rendered, fam):
    t = (rendered[fam] / "kvm_host__templates__autoinfra-internal-net.service").read_text()
    ex = _unit_exec(t)
    assert any("ip link add dummyai0 type dummy" in c for c in ex) and any("ip link add cloudbr1 type bridge" in c for c in ex)
    assert "/usr/sbin/ip addr replace 10.200.0.1/24 dev cloudbr1" in ex and "/usr/sbin/ip addr replace 10.201.0.1/24 dev cloudbr1" in ex
    assert sum("-s 10.20" in c and "-o cloudbr0 -m physdev ! --physdev-is-bridged -j MASQUERADE" in c for c in ex) == 2
    assert "! -o cloudbr1 -j MASQUERADE" not in "\n".join(ex)
    ret = [i for i, c in enumerate(ex) if "-j RETURN" in c]
    assert len(ret) == 4          # inserted with -I: at runtime they precede MASQUERADE (checked for real in the netns test)
    assert any("-s 10.200.0.0/24 -d 10.0.0.41 -j RETURN" in c and "-I POSTROUTING" in c for c in ex)
    assert "Before=libvirtd.service cloudstack-agent.service" in t
    # VLAN devices CloudStack creates on the dummy parent must fit IFNAMSIZ (15): dummyai0.4094
    assert len("dummyai0.4094") <= 15
    # the KVM agent only reports bridges whose member matches its interface patterns (4.22.1.1 LibvirtComputingResource);
    # 'aidummy0' did not -> "Can not find network: cloudbr1" on the real lab
    agent_patterns = [r"^eth", r"^bond", r"^vlan", r"^vx", r"^em", r"^ens", r"^eno", r"^enp", r"^team", r"^enx", r"^dummy", r"^lo",
                      r"^p\d+p\d+", r"^vni", r"^enc"]
    assert any(re.match(p, "dummyai0") for p in agent_patterns) and not any(re.match(p, "aidummy0") for p in agent_patterns)
    assert "ExecStartPre=-/usr/sbin/ip link del aidummy0" in t


def test_routes_unit_skips_hosts_in_except():
    import jinja2
    tpl = jinja2.Environment(trim_blocks=True).from_string((ANS / "roles/common/templates/autoinfra-routes.service.j2").read_text())
    routes = [{"to": "10.200.0.0/24", "via": "10.0.0.43", "except": ["10.0.0.43", "10.0.0.44"]}]
    assert "ip route replace" in tpl.render(autoinfra_static_routes=routes, ansible_host="10.0.0.41")
    assert "ip route replace" not in tpl.render(autoinfra_static_routes=routes, ansible_host="10.0.0.44")


def test_common_removes_route_unit_when_no_route_applies():
    tasks = {t["name"]: t for t in yaml.safe_load((ANS / "roles/common/tasks/main.yml").read_text())}
    assert "routes_remove.yml" in str(tasks["No routes for this host any more - remove an earlier route unit (and its routes)"])
    rm = yaml.safe_load((ANS / "roles/common/tasks/routes_remove.yml").read_text())
    assert "autoinfra-routes.service" in str(rm) and "absent" in str(rm)


@pytest.mark.parametrize("fam", list(FAMILIES))
def test_routes_unit_skips_own_gateway(rendered, fam):
    ex = _unit_exec((rendered[fam] / "common__templates__autoinfra-routes.service").read_text())
    assert ex == ["/usr/sbin/ip route replace 10.200.0.0/24 via 10.0.0.43", "/usr/sbin/ip route replace 10.201.0.0/24 via 10.0.0.43"]


def test_new_units_verify(rendered, tmp_path):
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze not available")
    for n in ("kvm_host__templates__autoinfra-internal-net.service", "common__templates__autoinfra-routes.service"):
        u = tmp_path / n.split("__")[-1]
        u.write_text((rendered["Debian"] / n).read_text())
        r = subprocess.run(["systemd-analyze", "verify", str(u)], capture_output=True, text=True)
        assert "Invalid" not in r.stderr and "Unknown" not in r.stderr, r.stderr


needs_root = pytest.mark.skipif(os.geteuid() != 0, reason="needs root (network namespaces / loop devices)")


@needs_root
@pytest.mark.skipif(not shutil.which("unshare") or not shutil.which("iptables"), reason="unshare/iptables missing")
def test_internal_net_unit_really_builds_the_network(rendered, tmp_path):
    """Run the unit's ExecStart commands for real in a private network namespace, twice (idempotent)."""
    ex = _unit_exec((rendered["Debian"] / "kvm_host__templates__autoinfra-internal-net.service").read_text())
    script = tmp_path / "net.sh"
    body = "\n".join(c.replace("/usr/sbin/sysctl -q -w", "sysctl -q -w") for c in ex)
    script.write_text("set -e\nip link set lo up\n" + body + "\n" + body + "\n"
                      "ip -br addr show cloudbr1\nip -d link show dummyai0\niptables -t nat -S POSTROUTING\nsysctl -n net.ipv4.ip_forward\n"
                      "ip link add link dummyai0 name dummyai0.4094 type vlan id 4094 && echo VLAN-OK\n")
    r = subprocess.run(["unshare", "-n", "sh", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "10.200.0.1/24" in out and "10.201.0.1/24" in out and "master cloudbr1" in out and "VLAN-OK" in out
    assert out.count("MASQUERADE") == 2 and out.count("-j RETURN") == 4, out     # second run added nothing
    rules = [l for l in out.splitlines() if l.startswith("-A POSTROUTING")]
    assert all("RETURN" in l for l in rules[:4]) and all("MASQUERADE" in l for l in rules[4:]), rules   # exemptions first
    assert out.strip().splitlines()[-2] == "1"


@needs_root
@pytest.mark.skipif(not all(shutil.which(t) for t in ("growpart", "mkfs.xfs", "losetup", "sfdisk")), reason="growpart/xfsprogs missing")
def test_growroot_really_grows_xfs(tmp_path):
    """growroot.yml's script on a loop disk: 600 MB disk, 300 MB xfs partition -> grown to the end of the disk; 2nd run unchanged."""
    img, mnt = tmp_path / "disk.img", tmp_path / "mnt"
    mnt.mkdir()
    subprocess.run(["truncate", "-s", "600M", str(img)], check=True)
    subprocess.run(["sfdisk", "-q", str(img)], input="label: gpt\n,300M\n", text=True, check=True)
    loop = subprocess.run(["losetup", "-fP", "--show", str(img)], capture_output=True, text=True, check=True).stdout.strip()
    try:
        subprocess.run(["mkfs.xfs", "-q", loop + "p1"], check=True)
        subprocess.run(["mount", loop + "p1", str(mnt)], check=True)
        tasks = yaml.safe_load((ANS / "roles/common/tasks/growroot.yml").read_text())
        sh = next(t for t in tasks if t["name"] == "Grow")["ansible.builtin.shell"].replace("{{ common_grow_mount | quote }}", str(mnt))
        size = lambda: int(subprocess.run(["df", "--output=size", "-B1M", str(mnt)], capture_output=True, text=True).stdout.split()[-1])
        before = size()
        r1 = subprocess.run(["bash", "-c", sh], capture_output=True, text=True)
        assert r1.returncode == 0 and "GROWN" in r1.stdout, r1.stdout + r1.stderr
        assert size() > before + 250, (before, size())
        r2 = subprocess.run(["bash", "-c", sh], capture_output=True, text=True)
        assert r2.returncode == 0 and "UNCHANGED" in r2.stdout, r2.stdout + r2.stderr
    finally:
        subprocess.run(["umount", str(mnt)])
        subprocess.run(["losetup", "-d", loop])


def test_kvm_role_leaves_libvirt_to_cloudstack_after_addhost():
    """cloudstack-setup-agent (run by addHost) owns libvirtd.conf/listen args afterwards and may enable TLS; a re-run of the
    role must not rewrite them (real lab: a re-run removed listen_tls and restarted libvirtd)."""
    tasks = {t["name"]: t for t in yaml.safe_load((ANS / "roles/kvm_host/tasks/main.yml").read_text())}
    probe = next(t for n, t in tasks.items() if n.startswith("Already set up by CloudStack"))
    assert "guid=" in probe["ansible.builtin.command"] and probe["failed_when"] is False and probe["register"] == "kvm_cs_setup"
    for n in ("libvirtd must listen on tcp (CloudStack requirement)", "libvirtd started with --listen",
              "Socket activation conflicts with --listen on Ubuntu - mask the sockets"):
        assert "kvm_cs_setup.rc != 0" in str(tasks[n]["when"]), n


def _render_internal(host, peers, gw):
    import jinja2
    env = jinja2.Environment(trim_blocks=True, lstrip_blocks=False, undefined=jinja2.StrictUndefined)
    env.filters["default"] = lambda v, d="", b=False: d if (v is None or isinstance(v, jinja2.Undefined) or (b and not v)) else v
    tpl = env.from_string((ANS / "roles/kvm_host/templates/autoinfra-internal-net.service.j2").read_text())
    net = {"bridge": "cloudbr1", "addresses": ["10.200.0.1/24"], "nat_cidrs": ["10.200.0.0/24"], "nat_exempt": [p for p in peers if p != gw],
           "gateway": gw, "peers": peers, "lan_dev": "ai-v" + ("a" if host == gw else "b"), "host_addresses": {p: "10.200.0.254/24" for p in peers if p != gw},
           "macs": {p: "02:a1:00:00:00:%02x" % i for i, p in enumerate(peers, 1)}}
    return tpl.render(kvm_internal_net=net, ansible_host=host)


def test_internal_net_multi_host_render():
    g, m = _render_internal("192.0.2.1", ["192.0.2.1", "192.0.2.2"], "192.0.2.1"), _render_internal("192.0.2.2", ["192.0.2.1", "192.0.2.2"], "192.0.2.1")
    for t in (g, m):
        assert "type vxlan id 4242 local" in t and "/usr/sbin/ip link set vxai0 master cloudbr1" in t and "ip link add dummyai0" not in t
        assert "ExecStartPre=-/usr/sbin/ip link del dummyai0" in t        # a host that gained peers drops its dummy
    assert "dst 192.0.2.2" in g and "dst 192.0.2.1" not in g and "dst 192.0.2.1" in m
    assert "ip addr replace 10.200.0.1/24" in g and "MASQUERADE" in g
    assert "ip addr replace 10.200.0.1/24" not in m and "MASQUERADE" not in m   # only the gateway owns the gateway address + NAT
    assert "ip addr replace 10.200.0.254/24 dev cloudbr1" in m and "10.200.0.254" not in g   # member: its own agent address
    assert "cloudbr1 address 02:a1:00:00:00:01" in g and "cloudbr1 address 02:a1:00:00:00:02" in m
    assert "vxai0 address 02:a2:00:00:00:01" in g and "vxai0 address 02:a2:00:00:00:02" in m
    assert "-o ai-va -m physdev ! --physdev-is-bridged -j MASQUERADE" in g      # NAT only routed traffic out of the LAN device
    assert re.match(r"^vx", "vxai0") and len("vxai0.4094") <= 15        # agent pattern ^vx, IFNAMSIZ


@needs_root
@pytest.mark.skipif(not shutil.which("iptables") or not shutil.which("bridge"), reason="iptables/iproute2 bridge missing")
def test_internal_net_two_hosts_really_connect_over_vxlan(tmp_path):
    """Two network namespaces = two KVM hosts on one LAN. Each runs its rendered unit (twice); a VM address on the member host's
    bridge reaches the gateway over VXLAN, and a guest VLAN on the VXLAN member crosses hosts too."""
    ns = {"a": "192.0.2.1", "b": "192.0.2.2"}
    RESULTS = {}
    sh = lambda n, c: subprocess.run(["ip", "netns", "exec", f"ai-{n}", "sh", "-c", c], capture_output=True, text=True)
    try:
        for n in ns:
            subprocess.run(["ip", "netns", "add", f"ai-{n}"], check=True)
        subprocess.run(["ip", "link", "add", "ai-va", "netns", "ai-a", "type", "veth", "peer", "name", "ai-vb", "netns", "ai-b"], check=True)
        for n, ip in ns.items():
            assert sh(n, f"ip link set lo up && ip addr add {ip}/24 dev ai-v{n} && ip link set ai-v{n} up").returncode == 0
            unit = _render_internal(ip, list(ns.values()), ns["a"])
            cmds = [l.split("=", 1)[1].lstrip("-") for l in unit.splitlines() if l.startswith(("ExecStartPre=", "ExecStart="))]
            script = "\n".join(c if c.startswith("/bin/sh") or "del " in c else c for c in cmds)
            for _ in range(2):                                                    # idempotent
                for c in cmds:
                    r = sh(n, c)
                    assert r.returncode == 0 or " del " in c, (n, c, r.stderr)
        assert "02:a1:00:00:00:02" in sh("b", "cat /sys/class/net/cloudbr1/address").stdout
        r = sh("b", "ping -c 2 -W 2 10.200.0.1")                              # from the member's own agent address .254
        assert r.returncode == 0, r.stdout + r.stderr
        # guest VLAN the way CloudStack builds it: <member>.<vlan> on each host
        for n, last in (("a", 1), ("b", 2)):
            assert sh(n, f"ip link add link vxai0 name vxai0.100 type vlan id 100 && ip addr add 10.99.0.{last}/24 dev vxai0.100 && ip link set vxai0.100 up").returncode == 0
        r = sh("b", "ping -c 2 -W 2 10.99.0.1")
        assert r.returncode == 0, r.stdout + r.stderr
        out = sh("a", "bridge fdb show dev vxai0").stdout
        assert out.count("dst 192.0.2.2") == 1, out                                # second run added no duplicate
        # a "VM" behind a port of the member host's bridge (like a tap), MTU 1500, talking to the gateway on the other host
        subprocess.run(["ip", "netns", "add", "ai-vm"], check=True)
        subprocess.run(["ip", "link", "add", "ai-tap", "netns", "ai-b", "type", "veth", "peer", "name", "eth0", "netns", "ai-vm"], check=True)
        assert sh("b", "ip link set ai-tap master cloudbr1 && ip link set ai-tap up").returncode == 0
        assert sh("vm", "ip link set lo up && ip addr add 10.200.0.60/24 dev eth0 && ip link set eth0 up").returncode == 0
        r = sh("vm", "ping -c 2 -W 2 10.200.0.1")
        assert r.returncode == 0, r.stdout + r.stderr
        # full-size frames from a VM: without DF they cross (the outer packet is fragmented on the 1500-byte LAN)
        r = sh("vm", "ping -c 1 -W 2 -s 1472 -M dont 10.200.0.1")
        RESULTS["vm_1500_no_df"] = r.returncode
        assert r.returncode == 0, r.stdout + r.stderr
    finally:
        for n in list(ns) + ["vm"]:
            subprocess.run(["ip", "netns", "del", f"ai-{n}"], capture_output=True)
