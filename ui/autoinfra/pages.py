"""Dashboard, audit log, settings, users, global search, notifications."""
import csv, io, time

from flask import Blueprint, Response, g, jsonify, render_template, request
from werkzeug.security import generate_password_hash

from . import auth, components, db, inventory, jobs, monitoring, validators as V

bp = Blueprint("pages", __name__)
SETTINGS = {   # key -> (validator, admin-only, secret)
    "master_ip": (V.ipv4, True, False), "repo_base_url": (lambda v: None if str(v).startswith(("http://", "https://")) else "must start with http(s)://", True, False),
    "online_mode": (lambda v: None if isinstance(v, bool) else "true/false", True, False),
    "cs_api_url": (lambda v: None if not v or str(v).startswith(("http://", "https://")) else "must start with http(s)://", True, False),
    "cs_api_key": (lambda v: None, True, False), "cs_secret_key": (lambda v: None, True, True),
    "cs_poll_interval": (lambda v: V.number_between(v, 0.01, 60, "poll interval"), True, False),
    "ollama_url": (lambda v: None if str(v).startswith(("http://", "https://")) else "must start with http(s)://", True, False),
    "ollama_model": (lambda v: None if v and len(str(v)) < 100 else "model name required", False, False),
    "ollama_timeout": (lambda v: V.int_between(v, 5, 1800, "timeout"), True, False),
    "zabbix_url": (lambda v: None if not v or str(v).startswith(("http://", "https://")) else "must start with http(s)://", True, False),
    "zabbix_token": (lambda v: None, True, True),
    "monitor_interval": (lambda v: V.int_between(v, 0, 86400, "interval"), True, False),
    "metrics_retention_days": (lambda v: V.int_between(v, 1, 365, "retention"), True, False),
    "kolla_ansible_bin": (lambda v: V.abspath(v) if v else None, True, False), "kolla_share_dir": (lambda v: V.abspath(v) if v else None, True, False),
    "kolla_collections_dir": (lambda v: V.abspath(v) if v else None, True, False),
    "ansible_playbook_bin": (lambda v: V.abspath(v) if v else None, True, False),
    "max_extract_gb": (lambda v: V.int_between(v, 1, 10000, "max extract"), True, False),
    "ansible_extra_vars": (lambda v: __import__("autoinfra.hosts", fromlist=["x"]).validate_vars(v), True, False),
}


@bp.route("/")
@auth.login_required
def dashboard():
    return render_template("dashboard.html")


@bp.route("/audit")
@auth.role_required("admin")
def audit_page():
    return render_template("audit.html")


@bp.route("/settings")
@auth.login_required
def settings_page():
    return render_template("settings.html")


@bp.route("/jobs")
@auth.login_required
def jobs_page():
    return render_template("jobs.html")


@bp.route("/api/dashboard")
@auth.login_required
def api_dashboard():
    ov = monitoring.overview()
    comps = [components.card(c) for c in components.COMPONENTS]
    from .hosts import stats
    up = db.one("SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM repo_items")
    return jsonify(overview=ov, hosts=stats(), packages={"items": up["n"], "bytes": up["s"]},
                   components={"installed": sum(c["state"] in ("installed", "running") for c in comps),
                               "failed": sum(c["state"] == "failed" for c in comps), "total": len(comps)},
                   jobs=[jobs.row_out(j) for j in db.q("SELECT * FROM jobs ORDER BY id DESC LIMIT 8")],
                   alerts=db.q("SELECT * FROM alerts WHERE resolved=0 ORDER BY id DESC LIMIT 6"))


@bp.route("/api/audit")
@auth.role_required("admin")
def api_audit():
    q = request.args.get("q", "")
    sql, args = "SELECT * FROM audit", []
    if q:
        sql += " WHERE action LIKE ? OR target LIKE ? OR user LIKE ? OR detail LIKE ?"
        args = [f"%{q}%"] * 4
    rows = db.q(sql + " ORDER BY id DESC LIMIT 2000", args)
    if request.args.get("format") == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time", "user", "ip", "action", "target", "detail", "ok"])
        for r in rows:
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])), r["user"], r["ip"], r["action"], r["target"], r["detail"], r["ok"]])
        return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=audit.csv"})
    return jsonify(audit=rows)


@bp.route("/api/settings")
@auth.login_required
def get_settings():
    out = {}
    for k, (_, _, secret) in SETTINGS.items():
        if secret:
            out[k] = "********" if inventory.secret_get(k) else ""
        else:
            out[k] = db.get_setting(k, "")
    from .hosts import thresholds
    from .monitoring import rules
    out["thresholds"] = thresholds()
    out["alert_rules"] = rules()
    out["pubkey"] = ""
    try:
        from .hosts import pub_key
        out["pubkey"] = pub_key()
    except Exception:  # noqa: BLE001
        pass
    return jsonify(settings=out)


@bp.route("/api/settings", methods=["POST"])
@auth.role_required("operator")
def save_settings():
    j = request.get_json(silent=True) or {}
    errs = {}
    for k, v in j.items():
        if k == "thresholds":
            for tk, lo, hi in (("min_cpu", 1, 512), ("min_mem_mb", 256, 4194304), ("min_disk_gb", 1, 100000)):
                if tk in v:
                    e = V.int_between(v[tk], lo, hi, tk)
                    if e:
                        errs[f"thresholds.{tk}"] = e
            continue
        if k == "alert_rules":
            for rk in monitoring.DEFAULT_RULES:
                if rk in v:
                    e = V.int_between(v[rk], 1, 100, rk)
                    if e:
                        errs[f"alert_rules.{rk}"] = e
            continue
        if k not in SETTINGS:
            errs[k] = "unknown setting"
            continue
        fn, admin_only, secret = SETTINGS[k]
        if admin_only and not auth.can("admin"):
            errs[k] = "admin role required"
            continue
        if secret and v == "********":
            continue
        e = fn(v)
        if e:
            errs[k] = e
    if errs:
        return jsonify(error="validation failed", fields=errs), 400
    for k, v in j.items():
        if k in ("thresholds", "alert_rules"):
            cur = db.get_setting(k, {}) or {}
            cur.update({kk: int(vv) for kk, vv in v.items()})
            db.set_setting(k, cur)
        elif SETTINGS[k][2]:
            if v != "********":
                inventory.secret_set(k, v)
        else:
            db.set_setting(k, v)
    auth.audit("settings.save", ",".join(sorted(j)))
    return jsonify(ok=True)


@bp.route("/api/users")
@auth.role_required("admin")
def list_users():
    return jsonify(users=db.q("SELECT id, username, full_name, role, created, last_login, disabled FROM users ORDER BY id"))


@bp.route("/api/users", methods=["POST"])
@auth.role_required("admin")
def create_user():
    j = request.get_json(silent=True) or {}
    errs = V.collect(("username", V.username(j.get("username"))), ("password", V.password(j.get("password"))),
                     ("role", V.choice(j.get("role"), list(auth.RANK), "role")))
    if not errs and db.one("SELECT id FROM users WHERE username=?", (j["username"],)):
        errs["username"] = "user already exists"
    if errs:
        return jsonify(error="validation failed", fields=errs), 400
    uid = db.ex("INSERT INTO users(username, full_name, pw_hash, role, created) VALUES(?,?,?,?,?)",
                (j["username"], str(j.get("full_name", ""))[:80], generate_password_hash(j["password"]), j["role"], time.time()))
    auth.audit("user.create", j["username"], j["role"])
    return jsonify(id=uid), 201


@bp.route("/api/users/<int:uid>", methods=["PATCH"])
@auth.role_required("admin")
def update_user(uid):
    u = db.one("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        return jsonify(error="no such user"), 404
    j = request.get_json(silent=True) or {}
    if "role" in j:
        e = V.choice(j["role"], list(auth.RANK), "role")
        if e:
            return jsonify(error=e), 400
        if u["id"] == g.user["id"] and j["role"] != "admin":
            return jsonify(error="you cannot remove your own admin role"), 400
        db.ex("UPDATE users SET role=? WHERE id=?", (j["role"], uid))
    if "disabled" in j:
        if u["id"] == g.user["id"]:
            return jsonify(error="you cannot disable yourself"), 400
        db.ex("UPDATE users SET disabled=? WHERE id=?", (1 if j["disabled"] else 0, uid))
    if "password" in j:
        e = V.password(j["password"])
        if e:
            return jsonify(error=e), 400
        db.ex("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(j["password"]), uid))
    auth.audit("user.update", u["username"], ",".join(k for k in j if k != "password") + (",password" if "password" in j else ""))
    return jsonify(ok=True)


@bp.route("/api/search")
@auth.login_required
def search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify(results=[])
    like = f"%{q}%"
    res = []
    for h in db.q("SELECT id, ip, hostname, status FROM hosts WHERE ip LIKE ? OR hostname LIKE ? LIMIT 10", (like, like)):
        res.append({"type": "host", "label": f"{h['hostname'] or h['ip']} ({h['ip']}) - {h['status']}", "url": f"/hosts#host-{h['id']}"})
    for p in db.q("SELECT repo, name, version FROM repo_items WHERE name LIKE ? LIMIT 10", (like,)):
        res.append({"type": "package", "label": f"{p['name']} {p['version']} [{p['repo']}]", "url": f"/packages#repo"})
    for cid, c in components.COMPONENTS.items():
        if q.lower() in c["name"].lower() or q.lower() in cid:
            res.append({"type": "component", "label": c["name"], "url": f"/components#{cid}"})
    for j in db.q("SELECT id, title, state FROM jobs WHERE title LIKE ? ORDER BY id DESC LIMIT 5", (like,)):
        res.append({"type": "job", "label": f"#{j['id']} {j['title']} ({j['state']})", "url": f"/jobs#job-{j['id']}"})
    return jsonify(results=res)


@bp.route("/api/notifications")
@auth.login_required
def notifications():
    al = db.q("SELECT id, ts, severity, message FROM alerts WHERE resolved=0 AND ack=0 ORDER BY id DESC LIMIT 10")
    running = db.one("SELECT COUNT(*) n FROM jobs WHERE state IN ('queued','running')")["n"]
    return jsonify(count=len(al), alerts=al, running_jobs=running)
