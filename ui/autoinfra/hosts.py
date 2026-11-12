"""Host management and pre-deployment checks (connectivity, system checks, update & upgrade, package validation)."""
import csv, io, json, os, subprocess, time
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, g, jsonify, render_template, request

from . import auth, db, drivers, inventory, jobs, sshops, validators as V

bp = Blueprint("hosts", __name__)
ROLE_SET = list(inventory.ROLE_GROUPS)
DEFAULT_TH = {"min_cpu": 2, "min_mem_mb": 3500, "min_disk_gb": 20}
REQUIRED_BY_ROLE = {
    "management": ["cloudstack-management", "mysql-client|mysql", "chrony", "python3"],
    "database": ["mysql-server|mysql-server", "keepalived", "chrony"],
    "compute": ["cloudstack-agent", "qemu-system-x86|qemu-kvm", "libvirt-daemon-system|libvirt", "nfs-common|nfs-utils"],
    "storage": ["nfs-kernel-server|nfs-utils"],
    "lb": ["haproxy", "keepalived"], "mq": ["rabbitmq-server"], "monitoring": ["zabbix-server-mysql|zabbix-server-mysql", "zabbix-agent2"],
    "controller": ["docker.io|docker-ce", "python3"], "network": ["docker.io|docker-ce"], "os-compute": ["docker.io|docker-ce"],
    "os-storage": ["lvm2"], "ai": ["ollama"], "app": ["nginx"],
}


def thresholds():
    t = dict(DEFAULT_TH)
    t.update(db.get_setting("thresholds", {}) or {})
    return t


def ensure_ssh_key():
    kp = inventory.key_path()
    if os.path.exists(kp):
        return kp
    os.makedirs(os.path.dirname(kp), mode=0o700, exist_ok=True)
    if sshops.paramiko is None:
        return kp
    k = sshops.paramiko.Ed25519Key.generate() if hasattr(sshops.paramiko.Ed25519Key, "generate") else None
    if k is None:
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "autoinfra-master", "-f", kp], check=True)
    else:
        k.write_private_key_file(kp)
    os.chmod(kp, 0o600)
    return kp


def pub_key():
    kp = ensure_ssh_key()
    if os.path.exists(kp + ".pub"):
        return open(kp + ".pub").read().strip()
    k = sshops.paramiko.Ed25519Key.from_private_key_file(kp)
    s = f"{k.get_name()} {k.get_base64()} autoinfra-master"
    open(kp + ".pub", "w").write(s + "\n")
    return s


def validate_host(j, partial=False):
    errs = {}
    if not partial or "ip" in j:
        e = V.ipv4(j.get("ip", ""))
        if e:
            errs["ip"] = e
    if j.get("hostname"):
        e = V.hostname(j["hostname"])
        if e:
            errs["hostname"] = e
    if not partial or "ssh_user" in j:
        e = V.username(j.get("ssh_user") or "root")
        if e:
            errs["ssh_user"] = e
    if "ssh_port" in j or not partial:
        sp = j.get("ssh_port", 22)
        e = V.port(22 if sp in (None, "") else sp)
        if e:
            errs["ssh_port"] = e
    if "roles" in j:
        roles = j["roles"] if isinstance(j["roles"], list) else [r for r in str(j["roles"]).replace(";", ",").replace("|", ",").split(",") if r]
        bad = [r for r in roles if r.strip() not in ROLE_SET]
        if bad:
            errs["roles"] = f"unknown role(s): {', '.join(bad)}; allowed: {', '.join(ROLE_SET)}"
    if j.get("grp"):
        e = V.name(j["grp"], "group")
        if e:
            errs["grp"] = e
    return errs


VAR_RE = __import__("re").compile(r"^[a-z_][a-z0-9_]{0,63}$")
FORBIDDEN_VARS = {"ansible_connection", "ansible_shell_executable", "ansible_python_interpreter", "ansible_become_exe", "ansible_ssh_executable"}


def validate_vars(v):
    if not isinstance(v, dict):
        return "variables must be a JSON object"
    if len(json.dumps(v)) > 20000:
        return "too many variables"
    for k in v:
        if not VAR_RE.match(str(k)):
            return f"'{k}' is not a valid variable name"
        if k in FORBIDDEN_VARS:
            return f"'{k}' cannot be set from the UI"
    return None


def norm_roles(v):
    roles = v if isinstance(v, list) else str(v or "").replace(";", ",").replace("|", ",").split(",")
    return ",".join(sorted({r.strip() for r in roles if r.strip()}))


def host_out(h):
    h = dict(h)
    h["roles"] = [r for r in (h.get("roles") or "").split(",") if r]
    try:
        f = json.loads(h.pop("facts") or "{}")
    except ValueError:
        f = {}
    h["cpus"], h["mem_mb"], h["disk_free_gb"] = f.get("CPUS"), f.get("MEM_MB"), f.get("DISK_FREE_GB")
    h["os_short"] = drivers.short_name(h.get("os_id"), h.get("os_version")) if h.get("os_id") else ""
    try:
        h["vars"] = json.loads(h.pop("hvars", None) or "{}")
    except ValueError:
        h["vars"] = {}
    return h


def add_host(j, password=None, user=None, do_bootstrap=True):
    """Insert a host; with a password, install our key now (password is never stored)."""
    errs = validate_host(j)
    if errs:
        return None, errs
    ip = j["ip"].strip()
    port = int(22 if j.get("ssh_port") in (None, "") else j["ssh_port"])
    if db.one("SELECT id FROM hosts WHERE ip=? AND ssh_port=?", (ip, port)):
        return None, {"ip": f"{ip}:{port} is already in the inventory"}
    hid = db.ex("INSERT INTO hosts(ip,hostname,ssh_user,ssh_port,roles,grp,created) VALUES(?,?,?,?,?,?,?)",
                (ip, (j.get("hostname") or "").strip(), (j.get("ssh_user") or "root").strip(), port,
                 norm_roles(j.get("roles", "")), (j.get("grp") or "default").strip(), time.time()))
    msg = "added"
    if password and do_bootstrap:
        h = db.one("SELECT * FROM hosts WHERE id=?", (hid,))
        try:
            sshops.with_retries(lambda: sshops.bootstrap_key(h, password, pub_key()), attempts=2, delay=1)
            db.ex("UPDATE hosts SET key_ok=1, ssh='ok' WHERE id=?", (hid,))
            msg = "added, SSH key installed"
        except sshops.AuthFailed:
            db.ex("UPDATE hosts SET status='auth-failed', ssh='auth' WHERE id=?", (hid,))
            msg = "added, but the password was rejected"
        except Exception as e:  # noqa: BLE001
            db.ex("UPDATE hosts SET status='unreachable', ssh='fail' WHERE id=?", (hid,))
            msg = f"added, but not reachable: {e}"
    return hid, msg


def check_host(h, opts, log, master_epoch):
    """Connectivity + facts + checks for one host. Returns (status, results)."""
    name = f"{h.get('hostname') or h['ip']} ({h['ip']})"
    res = []
    retries, timeout = int(opts.get("retries", 3)), int(opts.get("timeout", 10))
    ok, rtt = sshops.tcp_ping(h["ip"], h.get("ssh_port") or 22, timeout=min(timeout, 5))
    if opts.get("ping", True):
        res.append({"name": "Ping (TCP/SSH port)", "status": "pass" if ok else "fail", "detail": f"{rtt} ms" if ok else str(rtt)})
        log(f"[{name}] Ping: {'OK (%s ms)' % rtt if ok else 'FAILED ' + str(rtt)}")
    if not ok:
        db.ex("UPDATE hosts SET status='unreachable', ping='fail', ssh='fail', last_check=? WHERE id=?", (time.time(), h["id"]))
        return "unreachable", res
    password = opts.get("password") or None
    if password and not h.get("key_ok"):
        try:
            hh = dict(h, ssh_user=opts.get("username") or h.get("ssh_user"))
            sshops.with_retries(lambda: sshops.bootstrap_key(hh, password, pub_key(), timeout=timeout), attempts=retries, delay=1,
                                log=log)
            db.ex("UPDATE hosts SET key_ok=1 WHERE id=?", (h["id"],))
            log(f"[{name}] SSH key installed with the supplied password (password not stored)")
        except sshops.AuthFailed:
            res.append({"name": "SSH login", "status": "fail", "detail": "password rejected"})
            db.ex("UPDATE hosts SET status='auth-failed', ping='ok', ssh='auth', last_check=? WHERE id=?", (time.time(), h["id"]))
            log(f"[{name}] SSH login: FAILED (authentication)")
            return "auth-failed", res
        except Exception as e:  # noqa: BLE001
            log(f"[{name}] key bootstrap failed: {e}")
    try:
        client = sshops.with_retries(lambda: sshops.connect(h, key_path=inventory.key_path(), timeout=timeout),
                                     attempts=retries, delay=1, log=log)
    except sshops.AuthFailed:
        res.append({"name": "SSH login", "status": "fail", "detail": "key not accepted - add the host with its password once"})
        db.ex("UPDATE hosts SET status='auth-failed', ping='ok', ssh='auth', key_ok=0, last_check=? WHERE id=?", (time.time(), h["id"]))
        log(f"[{name}] SSH login: FAILED (key not accepted)")
        return "auth-failed", res
    except Exception as e:  # noqa: BLE001
        res.append({"name": "SSH login", "status": "fail", "detail": str(e)})
        db.ex("UPDATE hosts SET status='unreachable', ping='ok', ssh='fail', last_check=? WHERE id=?", (time.time(), h["id"]))
        return "unreachable", res
    try:
        res.append({"name": "SSH login", "status": "pass", "detail": f"as {h.get('ssh_user') or 'root'} with the master key"})
        log(f"[{name}] SSH Login: OK")
        rc, out, err = sshops.run_script(client, sshops.COLLECT, env={"REPO_URL": opts.get("repo_url", "")}, timeout=120)
    finally:
        client.close()
    facts = sshops.parse_facts(out)
    roles = [r for r in (h.get("roles") or "").split(",") if r]
    checks = sshops.evaluate(facts, thresholds(), master_epoch=master_epoch, roles=roles)
    if not opts.get("check_kvm", True):
        checks = [c for c in checks if not c["name"].startswith("CPU virtualization")]
    if not opts.get("check_os", True):
        checks = [c for c in checks if c["name"] != "OS compatibility"]
    res += checks
    for c in checks:
        log(f"[{name}] {c['name']}: {c['status'].upper()} - {c['detail']}")
    status = sshops.overall(res)
    s = sshops.summarize(facts)
    root = next((c["status"] for c in checks if c["name"] == "Root / sudo access"), "")
    db.ex("""UPDATE hosts SET status=?, ping='ok', ssh='ok', root=?, key_ok=1, os_id=?, os_version=?, os_family=?, os_pretty=?,
             os_support=?, kernel=?, updates=?, facts=?, last_check=?, hostname=CASE WHEN hostname='' THEN ? ELSE hostname END WHERE id=?""",
          (status, "ok" if root == "pass" else "fail", s["os_id"], s["os_version"], s["os_family"], s["os_pretty"], s["os_support"],
           s["kernel"], s["updates"], json.dumps(facts), time.time(), facts.get("HOSTNAME", ""), h["id"]))
    log(f"[{name}] RESULT: {status}")
    return status, res


@jobs.register("run_checks")
def run_checks(log, host_ids, opts, job_id=None):
    hosts = [db.one("SELECT * FROM hosts WHERE id=?", (i,)) for i in host_ids]
    hosts = [h for h in hosts if h]
    epoch = int(time.time())
    log(f"Checking {len(hosts)} host(s): retries={opts.get('retries', 3)} timeout={opts.get('timeout', 10)}s")
    summary = {}
    pin = (db.path(), db.data_dir())

    def one_host(h):
        db.use(*pin)
        try:
            st, res = check_host(h, opts, log, epoch)
        except Exception as e:  # noqa: BLE001
            st, res = "not-ready", [{"name": "Check", "status": "fail", "detail": f"{type(e).__name__}: {e}"}]
            db.ex("UPDATE hosts SET status='not-ready', last_check=? WHERE id=?", (time.time(), h["id"]))
            log(f"[{h['ip']}] ERROR {e}")
        for c in res:
            db.ex("INSERT INTO checks(run_id,host_id,name,status,detail) VALUES(?,?,?,?,?)",
                  (job_id, h["id"], c["name"], c["status"], c["detail"]))
        summary[h["id"]] = st

    with ThreadPoolExecutor(max_workers=max(1, min(16, int(opts.get("parallel", 8))))) as ex:
        list(ex.map(one_host, hosts))
    counts = {}
    for st in summary.values():
        counts[st] = counts.get(st, 0) + 1
    log("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return True


@jobs.register("host_packages")
def host_packages(log, host_ids, action):
    ok_all = True
    for hid in host_ids:
        h = db.one("SELECT * FROM hosts WHERE id=?", (hid,))
        if not h:
            continue
        fam = h.get("os_family") or ""
        pm = drivers.PKG_MANAGERS.get(fam)
        if not pm:
            log(f"[{h['ip']}] unknown OS family - run the system checks first")
            ok_all = False
            continue
        cmd = pm["refresh"] if action == "refresh" else pm["refresh"] + " && " + pm["upgrade"]
        if (h.get("ssh_user") or "root") != "root":
            cmd = f"sudo -n sh -c {json.dumps(cmd)}"
        log(f"[{h['ip']}] $ {cmd}")
        try:
            c = sshops.connect(h, key_path=inventory.key_path())
            try:
                rc, out, err = sshops.run(c, cmd, timeout=3600)
                for line in (out + err).splitlines()[-200:]:
                    log(f"[{h['ip']}] {line}")
                if rc == 0:
                    rc2, out2, _ = sshops.run(c, pm["count"], timeout=600)
                    n = int(out2.strip() or 0) if out2.strip().isdigit() else -1
                    db.ex("UPDATE hosts SET updates=? WHERE id=?", (n, hid))
                    log(f"[{h['ip']}] OK - {n} update(s) still pending")
                else:
                    ok_all = False
                    log(f"[{h['ip']}] FAILED rc={rc}")
            finally:
                c.close()
        except Exception as e:  # noqa: BLE001
            ok_all = False
            log(f"[{h['ip']}] ERROR {e}")
    return ok_all


# ------------------------------------------------------------------ pages
@bp.route("/hosts")
@auth.login_required
def hosts_page():
    return render_template("hosts.html", roles=inventory.ROLE_LABELS)


@bp.route("/predeploy")
@auth.login_required
def predeploy_page():
    return render_template("predeploy.html", roles=inventory.ROLE_LABELS)


# ------------------------------------------------------------------ API
@bp.route("/api/hosts")
@auth.login_required
def list_hosts():
    sql, args = "SELECT * FROM hosts", []
    wh = []
    if request.args.get("group"):
        wh.append("grp=?")
        args.append(request.args["group"])
    if request.args.get("role"):
        wh.append("(','||roles||',') LIKE ?")
        args.append(f"%,{request.args['role']},%")
    if request.args.get("q"):
        wh.append("(ip LIKE ? OR hostname LIKE ? OR os_pretty LIKE ?)")
        args += [f"%{request.args['q']}%"] * 3
    if wh:
        sql += " WHERE " + " AND ".join(wh)
    rows = [host_out(h) for h in db.q(sql + " ORDER BY id", args)]
    return jsonify(hosts=rows, stats=stats())


def stats():
    rows = db.q("SELECT status, updates, root FROM hosts")
    return {"total": len(rows), "reachable": sum(r["status"] in ("ready", "ready-with-warnings", "not-ready") for r in rows),
            "unreachable": sum(r["status"] == "unreachable" for r in rows), "auth_failed": sum(r["status"] == "auth-failed" for r in rows),
            "ready": sum(r["status"] in ("ready", "ready-with-warnings") for r in rows),
            "needs_update": sum((r["updates"] or 0) > 0 for r in rows), "root_ok": sum(r["root"] == "ok" for r in rows)}


@bp.route("/api/hosts/<int:hid>")
@auth.login_required
def get_host(hid):
    h = db.one("SELECT * FROM hosts WHERE id=?", (hid,))
    if not h:
        return jsonify(error="no such host"), 404
    facts = json.loads(h.get("facts") or "{}")
    last = db.one("SELECT MAX(run_id) AS r FROM checks WHERE host_id=?", (hid,))
    checks = db.q("SELECT name,status,detail FROM checks WHERE host_id=? AND run_id IS ? ORDER BY id", (hid, last["r"])) if last else []
    facts.pop("os", None)
    return jsonify(host=host_out(h), facts=facts, checks=checks)


@bp.route("/api/hosts", methods=["POST"])
@auth.role_required("operator")
def create_host():
    j = request.get_json(silent=True) or {}
    pw = j.pop("password", None)
    hid, msg = add_host(j, password=pw)
    if hid is None:
        return jsonify(error="validation failed", fields=msg), 400
    auth.audit("host.add", j.get("ip"), msg)
    return jsonify(id=hid, message=msg), 201


@bp.route("/api/hosts/import", methods=["POST"])
@auth.role_required("operator")
def import_hosts():
    """CSV: ip,hostname,ssh_user,ssh_port,roles,group,password  (header row required; password optional)"""
    if "file" in request.files:
        text = request.files["file"].read().decode("utf-8-sig", errors="replace")
    else:
        text = (request.get_json(silent=True) or {}).get("csv", "")
    if len(text) > 2_000_000:
        return jsonify(error="CSV too large"), 413
    rd = csv.DictReader(io.StringIO(text))
    if not rd.fieldnames or "ip" not in [f.strip().lower() for f in rd.fieldnames]:
        return jsonify(error="CSV needs a header row with at least an 'ip' column"), 400
    added, errors = [], []
    for n, row in enumerate(rd, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        j = {"ip": row.get("ip", ""), "hostname": row.get("hostname", ""), "ssh_user": row.get("ssh_user") or "root",
             "ssh_port": row.get("ssh_port") or 22, "roles": row.get("roles", ""), "grp": row.get("group") or "default"}
        hid, msg = add_host(j, password=row.get("password") or None)
        if hid is None:
            errors.append({"line": n, "errors": msg})
        else:
            added.append({"line": n, "id": hid, "message": msg})
    auth.audit("host.import", f"{len(added)} added", f"{len(errors)} errors")
    return jsonify(added=added, errors=errors)


@bp.route("/api/hosts/<int:hid>", methods=["PATCH"])
@auth.role_required("operator")
def update_host(hid):
    if not db.one("SELECT id FROM hosts WHERE id=?", (hid,)):
        return jsonify(error="no such host"), 404
    j = request.get_json(silent=True) or {}
    allowed = {k: j[k] for k in ("hostname", "ssh_user", "ssh_port", "roles", "grp") if k in j}
    errs = validate_host(allowed, partial=True)
    if "vars" in j:
        if not auth.can("admin"):
            errs["vars"] = "admin role required for Ansible variables"
        else:
            e = validate_vars(j["vars"])
            if e:
                errs["vars"] = e
            else:
                allowed["hvars"] = json.dumps(j["vars"])
    if errs:
        return jsonify(error="validation failed", fields=errs), 400
    if "roles" in allowed:
        allowed["roles"] = norm_roles(allowed["roles"])
    for k, v in allowed.items():
        db.ex(f"UPDATE hosts SET {k}=? WHERE id=?", (v, hid))
    auth.audit("host.update", hid, json.dumps(allowed))
    return jsonify(ok=True)


@bp.route("/api/hosts/<int:hid>", methods=["DELETE"])
@auth.role_required("operator")
def delete_host(hid):
    h = db.one("SELECT ip FROM hosts WHERE id=?", (hid,))
    if not h:
        return jsonify(error="no such host"), 404
    db.ex("DELETE FROM checks WHERE host_id=?", (hid,))
    db.ex("DELETE FROM metrics WHERE host_id=?", (hid,))
    db.ex("DELETE FROM hosts WHERE id=?", (hid,))
    auth.audit("host.delete", h["ip"])
    return jsonify(ok=True)


@bp.route("/api/hosts/groups")
@auth.login_required
def groups():
    return jsonify(groups=db.q("SELECT grp AS name, COUNT(*) AS hosts FROM hosts GROUP BY grp ORDER BY grp"))


@bp.route("/api/ssh/pubkey")
@auth.login_required
def ssh_pubkey():
    return jsonify(pubkey=pub_key())


def _select_hosts(j):
    if j.get("host_ids"):
        ids = [int(i) for i in j["host_ids"]]
    elif j.get("group"):
        ids = [r["id"] for r in db.q("SELECT id FROM hosts WHERE grp=?", (j["group"],))]
    else:
        ids = [r["id"] for r in db.q("SELECT id FROM hosts")]
    return ids


@bp.route("/api/checks/run", methods=["POST"])
@auth.role_required("operator")
def start_checks():
    j = request.get_json(silent=True) or {}
    errs = V.collect(("retries", V.int_between(j.get("retries", 3), 1, 10, "retries")),
                     ("timeout", V.int_between(j.get("timeout", 10), 1, 120, "timeout")),
                     ("parallel", V.int_between(j.get("parallel", 8), 1, 16, "parallel")))
    if j.get("username"):
        e = V.username(j["username"])
        if e:
            errs["username"] = e
    if errs:
        return jsonify(error="validation failed", fields=errs), 400
    ids = _select_hosts(j)
    if not ids:
        return jsonify(error="no hosts selected"), 400
    opts = {k: j.get(k) for k in ("retries", "timeout", "parallel", "username", "password") if j.get(k) is not None}
    opts.update({k: bool(j.get(k, True)) for k in ("ping", "check_os", "check_kvm")})
    opts["repo_url"] = inventory.repo_base() + "/"
    jid = jobs.start("checks", f"Pre-deployment checks ({len(ids)} hosts)", [{"name": "Connectivity + system checks", "fn": "run_checks",
                     "args": {"host_ids": ids, "opts": opts}}], meta={"host_ids": ids}, secrets=[opts.get("password")])
    auth.audit("checks.run", f"{len(ids)} hosts", f"job {jid}")
    return jsonify(job_id=jid), 202


@bp.route("/api/checks/latest")
@auth.login_required
def latest_checks():
    out = {}
    for h in db.q("SELECT id FROM hosts"):
        last = db.one("SELECT MAX(run_id) AS r FROM checks WHERE host_id=?", (h["id"],))
        if last and last["r"] is not None:
            out[h["id"]] = db.q("SELECT name,status,detail FROM checks WHERE host_id=? AND run_id=? ORDER BY id", (h["id"], last["r"]))
    return jsonify(checks=out)


@bp.route("/api/hosts/packages", methods=["POST"])
@auth.role_required("operator")
def host_pkg_action():
    j = request.get_json(silent=True) or {}
    action = j.get("action")
    if action not in ("refresh", "upgrade"):
        return jsonify(error="action must be refresh or upgrade"), 400
    ids = _select_hosts(j)
    if not ids:
        return jsonify(error="no hosts selected"), 400
    jid = jobs.start("host-packages", f"{'Update' if action == 'refresh' else 'Upgrade'} packages on {len(ids)} host(s)",
                     [{"name": f"package {action}", "fn": "host_packages", "args": {"host_ids": ids, "action": action}}])
    auth.audit(f"hosts.{action}", f"{len(ids)} hosts", f"job {jid}")
    return jsonify(job_id=jid), 202


@bp.route("/api/hosts/required")
@auth.login_required
def required_packages():
    """For each host role: which packages it needs and whether the master repository has them."""
    from .packages import available_names
    have = available_names()
    out = []
    for h in db.q("SELECT * FROM hosts ORDER BY id"):
        fam = h.get("os_family") or "debian"
        for role in [r for r in (h.get("roles") or "").split(",") if r]:
            for spec in REQUIRED_BY_ROLE.get(role, []):
                deb, _, rpm = spec.partition("|")
                pkg = (rpm or deb) if fam == "rhel" else deb
                out.append({"host_id": h["id"], "host": h.get("hostname") or h["ip"], "role": role, "package": pkg,
                            "in_repo": pkg in have})
    return jsonify(required=out)
