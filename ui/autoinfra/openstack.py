"""OpenStack (Kolla-Ansible) deployment wizard: node configuration, services, network & storage -> globals.yml, multinode,
passwords.yml, then bootstrap-servers / prechecks / deploy / post-deploy as one job."""
import ipaddress, os, re, secrets, shutil, string

from flask import Blueprint, jsonify, render_template, request

from . import auth, catalog, db, inventory, jobs, packages, validators as V

bp = Blueprint("openstack", __name__)
STEPS = ["requirements", "nodes", "services", "network", "deploy"]
STEP_LABELS = ["Check Requirements", "Node Configuration", "Select Packages", "Deploy Services", "Validate"]
SERVICES = {
    "keystone": ("Keystone (Identity)", True, True), "glance": ("Glance (Image)", True, True), "nova": ("Nova (Compute)", True, True),
    "neutron": ("Neutron (Networking)", True, True), "placement": ("Placement", True, True), "horizon": ("Horizon (Dashboard)", True, False),
    "cinder": ("Cinder (Block Storage)", True, False), "heat": ("Heat (Orchestration)", True, False),
    "octavia": ("Octavia (Load Balancer)", False, False), "ceilometer": ("Ceilometer (Telemetry)", False, False),
    "manila": ("Manila (Shared File System)", False, False), "swift": ("Swift (Object Storage)", False, False),
}   # id -> (label, default on, mandatory)
ADVANCED = {"octavia": "needs amphora image, certificates and a management network after deploy",
            "swift": "needs ring files in /etc/kolla/config/swift before deploy", "manila": "needs a share backend (generic/cephfs)"}
DEFAULTS = {
    "requirements": {"release": catalog.PRODUCTS["openstack"]["versions"][0], "base_distro": "ubuntu"},
    "nodes": {"controller_ids": [], "compute_ids": [], "storage_ids": [], "network_ids": [], "hostname": "openstack-controller",
              "database": "mariadb", "mq": "rabbitmq"},
    "services": {k: v[1] for k, v in SERVICES.items()},
    "network": {"network_interface": "eth0", "external_interface": "eth1", "vip": "", "provider_network": "physnet1",
                "vlan_range": "100-200", "storage_backend": "lvm", "lvm_vg": "cinder-volumes", "external_cidr": "",
                "external_gateway": "", "registry": "", "virt_type": "kvm", "neutron_plugin": "ovn", "nfs_shares": ""},
}


def state():
    raw = db.get_setting("os_wizard") or {}
    out = {k: dict(v) for k, v in DEFAULTS.items()}
    for k, v in raw.items():
        if k in out and isinstance(v, dict):
            out[k].update(v)
    return out


def _ids(v, what, allow_empty=False):
    if not isinstance(v, list) or not all(isinstance(i, int) for i in v):
        return f"{what} must be a list of host ids"
    if not v and not allow_empty:
        return f"select at least one {what}"
    miss = [i for i in v if not db.one("SELECT id FROM hosts WHERE id=?", (i,))]
    return f"unknown host id(s) {miss}" if miss else None


def validate(step, d, full=None):
    full = full or state()
    e = {}
    if step == "requirements":
        e = V.collect(("release", V.choice(d.get("release"), catalog.PRODUCTS["openstack"]["versions"], "release")),
                      ("base_distro", V.choice(d.get("base_distro"), ["ubuntu", "rocky"], "base distro")))
    elif step == "nodes":
        e = V.collect(("controller_ids", _ids(d.get("controller_ids", []), "controller")),
                      ("compute_ids", _ids(d.get("compute_ids", []), "compute node")),
                      ("storage_ids", _ids(d.get("storage_ids", []), "storage node", True)),
                      ("network_ids", _ids(d.get("network_ids", []), "network node", True)),
                      ("hostname", V.hostname(d.get("hostname"))),
                      ("database", V.choice(d.get("database"), ["mariadb"], "database")),
                      ("mq", V.choice(d.get("mq"), ["rabbitmq"], "message queue")))
        if "controller_ids" not in e and len(d["controller_ids"]) not in (1, 3):
            e["controller_ids"] = "use 1 controller (lab) or 3 (HA - Galera and RabbitMQ need an odd quorum)"
    elif step == "services":
        for k, (label, _, mandatory) in SERVICES.items():
            if mandatory and not d.get(k):
                e[k] = f"{label} is required"
        if d.get("horizon") and not d.get("keystone"):
            e["horizon"] = "Horizon needs Keystone"
        if d.get("octavia") and not d.get("neutron"):
            e["octavia"] = "Octavia needs Neutron"
        unknown = [k for k in d if k not in SERVICES]
        if unknown:
            e["unknown"] = f"unknown services: {unknown}"
    elif step == "network":
        e = V.collect(("network_interface", V.iface(d.get("network_interface"))),
                      ("external_interface", V.iface(d.get("external_interface"))),
                      ("vip", V.ipv4(d.get("vip", ""))), ("provider_network", V.name(d.get("provider_network"), "provider network")),
                      ("vlan_range", V.vlan_range(d.get("vlan_range"))),
                      ("storage_backend", V.choice(d.get("storage_backend"), ["lvm", "nfs", "ceph-external", "none"], "storage backend")),
                      ("virt_type", V.choice(d.get("virt_type"), ["kvm", "qemu"], "virt type")),
                      ("neutron_plugin", V.choice(d.get("neutron_plugin"), ["ovn", "openvswitch"], "neutron plugin")))
        if d.get("network_interface") and d.get("network_interface") == d.get("external_interface"):
            e["external_interface"] = "the external (provider) interface must differ from the management interface"
        if "vip" not in e:
            ips = [h["ip"] for h in db.q("SELECT ip FROM hosts")]
            if d["vip"] in ips:
                e["vip"] = "the VIP must be a free address, not a host IP"
        if d.get("external_cidr"):
            err = V.cidr(d["external_cidr"])
            if err:
                e["external_cidr"] = err
            elif d.get("external_gateway"):
                err = V.ip_in(d["external_gateway"], d["external_cidr"])
                if err:
                    e["external_gateway"] = err
        if d.get("registry"):
            host, _, port = str(d["registry"]).partition(":")
            err = V.host_or_ip(host) or (V.port(port) if port else None)
            if err:
                e["registry"] = err
        if d.get("storage_backend") == "lvm" and not re.match(r"^[A-Za-z0-9_.+-]{1,64}$", str(d.get("lvm_vg", ""))):
            e["lvm_vg"] = "volume group name is required for LVM"
        if d.get("storage_backend") == "nfs":
            shares = [x.strip() for x in str(d.get("nfs_shares", "")).split(",") if x.strip()]
            if not shares:
                e["nfs_shares"] = "enter at least one NFS share (server:/path)"
            for sh in shares:
                if not re.match(r"^[A-Za-z0-9.-]+:/[A-Za-z0-9._/-]*$", sh):
                    e["nfs_shares"] = f"'{sh}' is not server:/path"
        if d.get("storage_backend") in ("lvm",) and full["services"].get("cinder") and not full["nodes"].get("storage_ids"):
            e["storage_backend"] = "LVM backend needs at least one storage node (Node Configuration)"
    else:
        e["step"] = "unknown step"
    return e


def hosts_by_id():
    return {h["id"]: h for h in db.q("SELECT * FROM hosts")}


def render_globals(s):
    n, net, sv = s["nodes"], s["network"], s["services"]
    yes = lambda b: '"yes"' if b else '"no"'
    reg = net.get("registry") or f"{inventory.master_ip()}:5000"
    backend = net["storage_backend"]
    lines = [
        "---", "# generated by AutoInfra - re-generated on every deployment from the wizard",
        f'kolla_base_distro: "{s["requirements"]["base_distro"]}"', f'openstack_release: "{s["requirements"]["release"]}"',
        f'kolla_internal_vip_address: "{net["vip"]}"', f'network_interface: "{net["network_interface"]}"',
        f'neutron_external_interface: "{net["external_interface"]}"', f'neutron_plugin_agent: "{net["neutron_plugin"]}"',
        f'nova_compute_virt_type: "{net["virt_type"]}"',
        "", "# air-gap: every image comes from the master registry, docker from the local OS repo",
        f'docker_registry: "{reg}"', 'docker_namespace: "openstack.kolla"', "docker_registry_insecure: true",
        "enable_docker_repo: false", 'docker_apt_package: "docker.io"',
        "", "# HA / load balancing of the API endpoints", 'enable_haproxy: "yes"', 'enable_keepalived: "yes"',
        "enable_neutron_provider_networks: \"yes\"",
        "", "# services",
    ]
    for k in SERVICES:
        lines.append(f"enable_{k}: {yes(sv.get(k))}")
    lines += ["", "# storage",
              f"enable_cinder_backend_lvm: {yes(sv.get('cinder') and backend == 'lvm')}",
              f'cinder_volume_group: "{net.get("lvm_vg", "cinder-volumes")}"',
              f"enable_cinder_backend_nfs: {yes(sv.get('cinder') and backend == 'nfs')}"]
    if backend == "ceph-external" and sv.get("cinder"):
        lines += ["cinder_backend_ceph: \"yes\"", "glance_backend_ceph: \"yes\"", "nova_backend_ceph: \"yes\""]
    return "\n".join(lines) + "\n"


def render_multinode(s, share_template=None):
    hb = hosts_by_id()
    n = s["nodes"]

    def block(ids):
        out = []
        for i in ids:
            h = hb.get(i)
            if not h:
                continue
            line = f"{h.get('hostname') or h['ip']} ansible_host={h['ip']} ansible_user={h.get('ssh_user') or 'root'} " \
                   f"ansible_port={h.get('ssh_port') or 22} ansible_ssh_private_key_file={inventory.key_path()}"
            if (h.get("ssh_user") or "root") != "root":
                line += " ansible_become=true"
            out.append(line)
        return "\n".join(out)
    net_ids = n.get("network_ids") or n["controller_ids"]
    head = (f"[control]\n{block(n['controller_ids'])}\n\n[network]\n{block(net_ids)}\n\n[compute]\n{block(n['compute_ids'])}\n\n"
            f"[monitoring]\n{block(n['controller_ids'][:1])}\n\n[storage]\n{block(n.get('storage_ids') or [])}\n\n"
            "[deployment]\nlocalhost ansible_connection=local\n\n")
    if share_template and "[baremetal:children]" in share_template:
        return head + share_template[share_template.index("[baremetal:children]"):]
    return head


def kolla_share():
    d = db.get_setting("kolla_share_dir") or os.environ.get("AUTOINFRA_KOLLA_SHARE")
    for cand in [d, "/opt/airgap/venv-kolla/share/kolla-ansible", "/usr/local/share/kolla-ansible", "/usr/share/kolla-ansible"]:
        if cand and os.path.isfile(os.path.join(cand, "ansible", "inventory", "multinode")):
            return cand
    return None


def kolla_bin():
    return (db.get_setting("kolla_ansible_bin") or os.environ.get("AUTOINFRA_KOLLA_BIN") or shutil.which("kolla-ansible")
            or ("/opt/airgap/venv-kolla/bin/kolla-ansible" if os.path.exists("/opt/airgap/venv-kolla/bin/kolla-ansible") else "kolla-ansible"))


def gen_passwords(template_text):
    """Fill every empty key of kolla's passwords.yml (what kolla-genpwd does, without needing it)."""
    out = []
    alpha = string.ascii_letters + string.digits
    for line in template_text.splitlines():
        m = re.match(r"^([A-Za-z0-9_]+):\s*$", line)
        if m and not m.group(1).endswith(("_ssh_key", "_keys")):
            out.append(f"{m.group(1)}: {''.join(secrets.choice(alpha) for _ in range(32))}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def config_dir():
    d = os.path.join(db.data_dir(), "openstack", "etc")
    os.makedirs(os.path.join(d, "config"), mode=0o700, exist_ok=True)
    return d


def write_config(s):
    d = config_dir()
    share = kolla_share()
    tmpl = open(os.path.join(share, "ansible", "inventory", "multinode")).read() if share else None
    open(os.path.join(d, "globals.yml"), "w").write(render_globals(s))
    open(os.path.join(d, "multinode"), "w").write(render_multinode(s, tmpl))
    pw = os.path.join(d, "passwords.yml")
    if not os.path.exists(pw):
        src = os.path.join(share, "etc_examples", "kolla", "passwords.yml") if share else None
        text = open(src).read() if src and os.path.exists(src) else "\n".join(f"{k}:" for k in (
            "database_password", "rabbitmq_password", "keystone_admin_password", "keystone_database_password", "glance_database_password",
            "glance_keystone_password", "nova_database_password", "nova_api_database_password", "nova_keystone_password",
            "placement_keystone_password", "placement_database_password", "neutron_database_password", "neutron_keystone_password",
            "cinder_database_password", "cinder_keystone_password", "heat_database_password", "heat_keystone_password",
            "horizon_secret_key", "horizon_database_password", "keepalived_password", "memcache_secret_key", "metadata_secret"))
        fd = os.open(pw, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(gen_passwords(text))
    if s["network"]["storage_backend"] == "nfs" and s["services"].get("cinder"):
        os.makedirs(os.path.join(d, "config", "cinder"), exist_ok=True)
        open(os.path.join(d, "config", "nfs_shares"), "w").write("\n".join(x.strip() for x in s["network"]["nfs_shares"].split(",")) + "\n")
    return d, bool(share)


# ------------------------------------------------------------------ pages + API
@bp.route("/openstack/wizard")
@auth.login_required
def wizard_page():
    return render_template("os_wizard.html", steps=list(zip(STEPS, STEP_LABELS)), services=SERVICES, advanced=ADVANCED,
                           versions=catalog.PRODUCTS["openstack"]["versions"])


@bp.route("/api/os/wizard")
@auth.login_required
def get_wizard():
    s = state()
    fam = "rhel" if s["requirements"]["base_distro"] == "rocky" else "debian"
    return jsonify(state=s, steps=STEPS, labels=STEP_LABELS, services={k: v[0] for k, v in SERVICES.items()},
                   advanced=ADVANCED, required=packages.presence("openstack", fam), kolla_share=kolla_share())


@bp.route("/api/os/wizard/<step>", methods=["POST"])
@auth.role_required("operator")
def save_step(step):
    if step not in STEPS[:-1]:
        return jsonify(error="unknown step"), 404
    d = request.get_json(silent=True) or {}
    s = state()
    merged = dict(s[step])
    if step == "services":
        merged.update({k: bool(v) for k, v in d.items()})
    else:
        merged.update({k: v for k, v in d.items() if k in DEFAULTS[step]})
    errs = validate(step, merged, s)
    if errs:
        return jsonify(error="validation failed", fields=errs), 400
    raw = db.get_setting("os_wizard") or {}
    raw[step] = merged
    db.set_setting("os_wizard", raw)
    auth.audit("os.wizard.save", step)
    return jsonify(ok=True, state=state())


@bp.route("/api/os/preview")
@auth.login_required
def preview():
    s = state()
    return jsonify(globals=render_globals(s), multinode=render_multinode(s))


@bp.route("/api/os/passwords/regenerate", methods=["POST"])
@auth.role_required("admin")
def regen_passwords():
    p = os.path.join(config_dir(), "passwords.yml")
    if os.path.exists(p):
        os.unlink(p)
    auth.audit("os.passwords.regenerate")
    return jsonify(ok=True, note="new passwords are generated at the next deployment (do NOT do this on a deployed cloud)")


@bp.route("/api/os/deploy", methods=["POST"])
@auth.role_required("operator")
def deploy():
    j = request.get_json(silent=True) or {}
    s = state()
    errs = {st: e for st in STEPS[:-1] for e in [validate(st, s[st], s)] if e}
    if errs:
        return jsonify(error="the wizard has errors", steps=errs), 400
    d, have_share = write_config(s)
    if not have_share and not j.get("force"):
        return jsonify(error="kolla-ansible is not installed on the master (run master/install-kolla-tools.sh or set Settings > Kolla share dir)"), 409
    kb = kolla_bin()
    inv = os.path.join(d, "multinode")
    env = {"ANSIBLE_PRIVATE_KEY_FILE": inventory.key_path(), "ANSIBLE_HOST_KEY_CHECKING": "False", "PYTHONUNBUFFERED": "1"}
    coll = db.get_setting("kolla_collections_dir") or "/opt/airgap/collections"     # written by master/install-kolla-tools.sh
    if os.path.isdir(coll):
        env["ANSIBLE_COLLECTIONS_PATH"] = coll
    kb_dir = os.path.dirname(kb) if os.path.isabs(kb) else ""
    if kb_dir:                                   # kolla-ansible calls the ansible-playbook of its own venv
        env["PATH"] = kb_dir + os.pathsep + os.environ.get("PATH", "")
    base = ["-i", inv, "--configdir", d, "--passwords", os.path.join(d, "passwords.yml")]
    actions = [("bootstrap-servers", "Bootstrap servers (docker, users, kernel settings)"), ("prechecks", "Kolla pre-checks"),
               ("pull", "Pull images from the master registry"), ("deploy", "Deploy OpenStack services"),
               ("post-deploy", "Post-deploy (admin-openrc.sh / clouds.yaml)")]
    steps = [{"name": label, "cmd": [kb, act] + base, "env": env, "cwd": d} for act, label in actions]
    steps.append({"name": "Validate (service endpoints)", "fn": "os_validate", "args": {"config_dir": d}})
    jid = jobs.start("deploy-openstack", f"Deploy OpenStack {s['requirements']['release']}", steps, meta={"config_dir": d})
    auth.audit("os.deploy", s["requirements"]["release"], f"job {jid}")
    return jsonify(job_id=jid), 202


@jobs.register("os_validate")
def os_validate(log, config_dir):
    rc = os.path.join(config_dir, "admin-openrc.sh")
    if not os.path.exists(rc) and os.path.exists("/etc/kolla/admin-openrc.sh"):
        rc = "/etc/kolla/admin-openrc.sh"
    if os.path.exists(rc):
        log(f"admin credentials: {rc}  (source it, then: openstack endpoint list)")
        return True
    log("admin-openrc.sh not found - post-deploy did not finish")
    return False
