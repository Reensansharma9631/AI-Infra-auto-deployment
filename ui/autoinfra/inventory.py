"""Ansible inventory + playbook steps built from the hosts table; generated secrets kept in <data>/secrets.json (0600)."""
import json, os, secrets as _secrets, shutil, string, threading, uuid

from . import db

ROLE_GROUPS = {          # host role (UI)      -> ansible group
    "management": "cs_mgmt", "database": "cs_db", "compute": "cs_kvm", "storage": "cs_nfs", "monitoring": "monitoring",
    "lb": "cs_lb", "mq": "mq", "controller": "os_control", "network": "os_network", "os-compute": "os_compute",
    "os-storage": "os_storage", "ai": "ai", "app": "app",
}
ROLE_LABELS = {"management": "Management", "database": "Database", "compute": "Compute (KVM)", "storage": "Storage",
               "monitoring": "Monitoring", "lb": "Load balancer", "mq": "Message queue", "controller": "OpenStack controller",
               "network": "OpenStack network", "os-compute": "OpenStack compute", "os-storage": "OpenStack storage",
               "ai": "AI (Ollama)", "app": "Applications"}
_slock = threading.Lock()


def master_net_helper():
    """Root helper the service user may run via sudoers (installed by install-autoinfra.sh); None when not installed."""
    p = os.environ.get("AUTOINFRA_MASTER_NET")
    if p is None:
        p = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "master", "autoinfra-master-net"))
        if not (os.path.exists(p) and os.path.exists("/etc/systemd/system/autoinfra-master-net.path")):
            return None
    return p or None


def master_net(*args, log=None):
    """Run the helper (sudo -n). Returns (ok, output); never raises - DNS records/routes on the master are a convenience."""
    import subprocess
    p = master_net_helper()
    if not p:
        return False, "master network helper not installed (install-autoinfra.sh sets it up)"
    try:
        if os.geteuid() == 0 or os.environ.get("AUTOINFRA_MASTER_NET"):
            r = subprocess.run([p, *args], capture_output=True, text=True, timeout=60)
            ok, out = r.returncode == 0, (r.stdout + r.stderr).strip()
        else:
            ok, out = _master_net_request(args)
        if log:
            log(f"[master] {' '.join(args)}: {out[-300:]}")
        return ok, out
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def _master_net_request(args, wait=60):
    """The service runs without privileges: write <data>/master-net/request.json (atomic rename -> the root path unit runs
    the helper) and wait for result.json with our id."""
    import time
    d = os.path.join(db.data_dir(), "master-net")
    os.makedirs(d, exist_ok=True)
    rid = uuid.uuid4().hex
    tmp, req, res = os.path.join(d, "request.json.tmp"), os.path.join(d, "request.json"), os.path.join(d, "result.json")
    with open(tmp, "w") as fh:
        json.dump({"id": rid, "args": list(args)}, fh)
    os.replace(tmp, req)
    end = time.time() + wait
    while time.time() < end:
        try:
            r = json.load(open(res))
            if r.get("id") == rid:
                return r.get("rc") == 0, str(r.get("output", ""))
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    return False, "no answer from the master network helper (is autoinfra-master-net.path active? re-run install-autoinfra.sh)"


def ansible_dir():
    d = os.environ.get("AUTOINFRA_ANSIBLE_DIR")
    if d:
        return d
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "ansible"))


def ansible_bin():
    return (os.environ.get("AUTOINFRA_ANSIBLE_PLAYBOOK") or db.get_setting("ansible_playbook_bin")
            or shutil.which("ansible-playbook") or "ansible-playbook")


def key_path():
    return os.environ.get("AUTOINFRA_SSH_KEY") or os.path.join(db.data_dir(), "ssh", "id_ed25519")


def _spath():
    return os.path.join(db.data_dir(), "secrets.json")


def secret(name, length=24, alphabet=string.ascii_letters + string.digits):
    """Stable generated secret (created once, reused by every later deployment)."""
    with _slock:
        p = _spath()
        data = json.load(open(p)) if os.path.exists(p) else {}
        if name not in data:
            # guarantee upper, lower and digit so the value also passes password policies
            while True:
                v = "".join(_secrets.choice(alphabet) for _ in range(length))
                if any(c.islower() for c in v) and any(c.isupper() for c in v) and any(c.isdigit() for c in v):
                    break
            data[name] = v
            fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
            os.replace(p + ".tmp", p)
        return data[name]


def host_name(h):
    return h.get("hostname") or h["ip"]


def groups_for(h):
    return sorted({ROLE_GROUPS[r] for r in (h.get("roles") or "").split(",") if r in ROLE_GROUPS})


def render(hosts, extra_groups=None):
    """INI inventory. extra_groups: {"group": [host_id,...]} for wizard selections not tied to roles."""
    by_group = {}
    lines = ["[all]"]
    for h in hosts:
        hv = [f"ansible_host={h['ip']}", f"ansible_port={int(h.get('ssh_port') or 22)}",
              f"ansible_user={h.get('ssh_user') or 'root'}", f"node_hostname={host_name(h)}"]
        if (h.get("ssh_user") or "root") != "root":
            hv.append("ansible_become=true")
        if h.get("os_family"):
            hv.append(f"autoinfra_os_family={h['os_family']}")
        lines.append(f"{host_name(h)} " + " ".join(hv))
        for gname in groups_for(h):
            by_group.setdefault(gname, []).append(host_name(h))
    ids = {h["id"]: h for h in hosts if "id" in h}
    for gname, hid_list in (extra_groups or {}).items():
        by_group[gname] = [host_name(ids[i]) for i in hid_list if i in ids]
    for gname in sorted(by_group):
        lines += ["", f"[{gname}]"] + by_group[gname]
    return "\n".join(lines) + "\n"


def work_dir():
    d = os.path.join(db.data_dir(), "work", uuid.uuid4().hex[:12])
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def playbook_steps(playbooks, hosts, extra_vars=None, extra_groups=None, tags=None, limit=None, check=False):
    """Write inventory + vars into a private work dir and return job steps that run each playbook in order."""
    wd = work_dir()
    inv = os.path.join(wd, "inventory.ini")
    open(inv, "w").write(render(hosts, extra_groups))
    hv_dir = os.path.join(wd, "host_vars")
    os.makedirs(hv_dir, exist_ok=True)
    for h in hosts:                          # per-host variables (Host Management > Edit > Ansible variables)
        try:
            hv = json.loads(h.get("hvars") or "{}")
        except ValueError:
            hv = {}
        if hv:
            open(os.path.join(hv_dir, host_name(h) + ".json"), "w").write(json.dumps(hv))
    ev = {"airgap_repo_base": repo_base(), "master_ip": master_ip(), "cs_key_dir": os.path.join(db.data_dir(), "cs-keys")}
    ev.update(repo_lists())
    ev.update(db.get_setting("ansible_extra_vars", {}) or {})   # Settings > global Ansible variables
    ev.update(extra_vars or {})
    vf = os.path.join(wd, "vars.json")
    fd = os.open(vf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(ev, fh, indent=1)
    adir = ansible_dir()
    env = {"ANSIBLE_CONFIG": os.path.join(adir, "ansible.cfg"), "ANSIBLE_PRIVATE_KEY_FILE": key_path(),
           "ANSIBLE_HOST_KEY_CHECKING": "False", "ANSIBLE_NOCOLOR": "1", "ANSIBLE_FORCE_COLOR": "0",
           "ANSIBLE_LOCAL_TEMP": os.path.join(wd, "tmp"), "PYTHONUNBUFFERED": "1"}
    steps = []
    for pb in playbooks:
        name = pb if isinstance(pb, str) else pb[0]
        label = name if isinstance(pb, str) else pb[1]
        cmd = [ansible_bin(), "-i", inv, "-e", "@" + vf, os.path.join(adir, name)]
        if tags:
            cmd += ["--tags", ",".join(tags)]
        if limit:
            cmd += ["--limit", limit]
        if check:
            cmd += ["--check", "--diff"]
        steps.append({"name": label, "cmd": cmd, "env": env, "cwd": adir})
    return steps, wd


def repo_lists():
    """Only repositories that really exist on the master go into the nodes' apt/dnf sources (a missing one breaks apt update)."""
    from . import catalog, packages
    root = packages.repo_root()
    return {"airgap_deb_repos": [r for r in catalog.REPOS if os.path.exists(os.path.join(root, r, "deb", "Packages"))],
            "airgap_rpm_repos": [r for r in catalog.REPOS if os.path.exists(os.path.join(root, r, "rpm", "repodata", "repomd.xml"))]}


def repo_base():
    return db.get_setting("repo_base_url") or os.environ.get("AUTOINFRA_REPO_URL") or f"http://{master_ip()}:8081/repo"


def master_ip():
    return db.get_setting("master_ip") or os.environ.get("AUTOINFRA_MASTER_IP", "127.0.0.1")


def _load_secrets():
    p = _spath()
    return json.load(open(p)) if os.path.exists(p) else {}


def secret_get(name, default=None):
    with _slock:
        return _load_secrets().get(name, default)


def secret_set(name, value):
    with _slock:
        data = _load_secrets()
        if value in (None, ""):
            data.pop(name, None)
        else:
            data[name] = value
        p = _spath()
        fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(p + ".tmp", p)
