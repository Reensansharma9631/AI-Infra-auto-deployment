"""Component deployment & management: each card = one Ansible playbook run on the hosts you pick, plus live service status."""
import json, time

from flask import Blueprint, jsonify, render_template, request

from . import auth, db, drivers, inventory, jobs, sshops, validators as V

bp = Blueprint("components", __name__)

F = lambda typ, default, check=None, label=None, options=None: {"type": typ, "default": default, "check": check, "label": label, "options": options}

COMPONENTS = {
    "cloudstack-management": {"name": "Management Server", "cat": "core", "icon": "server", "color": "#025cfc",
        "playbook": "20-cloudstack-mgmt.yml", "group": "cs_mgmt", "role": "management", "version": "4.22",
        "services": {"debian": ["cloudstack-management"], "rhel": ["cloudstack-management"]}, "min": 1, "max": 3,
        "config": {"ui_port": F("int", 8080, lambda v: V.port(v), "UI/API port")}},
    "cloudstack-usage": {"name": "Usage Server", "cat": "core", "icon": "chart", "color": "#7c3aed",
        "playbook": "24-cloudstack-usage.yml", "group": "cs_usage", "role": "management", "version": "4.22",
        "services": {"debian": ["cloudstack-usage"], "rhel": ["cloudstack-usage"]}, "min": 1, "max": 1, "config": {}},
    "cloudstack-agent": {"name": "KVM Agent (libvirt)", "cat": "core", "icon": "chip", "color": "#16a34a",
        "playbook": "23-cloudstack-kvm.yml", "group": "cs_kvm", "role": "compute", "version": "4.22",
        "services": {"debian": ["cloudstack-agent", "libvirtd"], "rhel": ["cloudstack-agent", "libvirtd"]}, "min": 1, "max": 500,
        "config": {"bridge": F("str", "cloudbr0", V.iface, "Bridge name"), "libvirt_tcp": F("bool", True, None, "libvirt over TCP"),
                   "auto_bridge": F("bool", False, None, "Create the bridge automatically (detect NIC/IP/gateway, rollback if unreachable)")}},
    "systemvm-templates": {"name": "System VM Templates", "cat": "core", "icon": "layers", "color": "#0891b2",
        "playbook": "25-systemvm-template.yml", "group": "cs_mgmt", "role": "management", "version": "4.22",
        "services": {}, "min": 1, "max": 1,
        "config": {"secondary": F("str", "", None, "Secondary storage (server:/path)"), "template": F("str", "", None, "Template path in repo")}},
    "mysql": {"name": "MySQL Database", "cat": "databases", "icon": "db", "color": "#0e7490",
        "playbook": None, "group": "cs_db", "role": "database", "version": "8.0",
        "services": {"debian": ["mysql"], "rhel": ["mysqld"]}, "min": 1, "max": 3,
        "config": {"strategy": F("choice", "gtid_agent", None, "HA strategy", list(drivers.DB_STRATEGIES)),
                   "vip": F("str", "", lambda v: V.ipv4(v) if v else None, "VIP (2-3 nodes)"),
                   "port": F("int", 3306, V.port, "Port"), "backup": F("bool", True, None, "Configure backup (automated)"),
                   "replication": F("bool", True, None, "Configure replication"), "monitoring": F("bool", True, None, "Create monitoring (Zabbix)"),
                   "install_path": F("str", "/opt/mysql", V.abspath, "Install path"), "data_path": F("str", "/var/lib/mysql", V.abspath, "Data path")}},
    "postgresql": {"name": "PostgreSQL Database", "cat": "databases", "icon": "db", "color": "#1d4ed8",
        "playbook": "32-postgresql.yml", "group": "postgresql", "role": "database", "version": "16",
        "services": {"debian": ["postgresql"], "rhel": ["postgresql"]}, "min": 1, "max": 1,
        "config": {"port": F("int", 5432, V.port, "Port"), "listen": F("str", "*", None, "Listen addresses"),
                   "allowed_cidr": F("str", "", lambda v: V.cidr(v) if v else None, "Allowed client network (CIDR)"),
                   "data_path": F("str", "", lambda v: V.abspath(v) if v else None, "Data path (empty = distro default)")}},
    "oracle": {"name": "Oracle Database (19c prerequisites)", "cat": "databases", "icon": "db", "color": "#dc2626",
        "playbook": "34-oracle-prereq.yml", "group": "oracle", "role": "database", "version": "19c",
        "services": {}, "min": 1, "max": 1,
        "config": {"oracle_base": F("str", "/u01/app/oracle", V.abspath, "ORACLE_BASE"),
                   "installer_zip": F("str", "", None, "Installer zip in repo 'databases/files' (licensed - you supply it)")}},
    "mongodb": {"name": "MongoDB Database", "cat": "databases", "icon": "db", "color": "#15803d",
        "playbook": "33-mongodb.yml", "group": "mongodb", "role": "database", "version": "7.0",
        "services": {"debian": ["mongod"], "rhel": ["mongod"]}, "min": 1, "max": 3,
        "config": {"port": F("int", 27017, V.port, "Port"), "replset": F("str", "rs0", lambda v: V.name(v, "replica set"), "Replica set name"),
                   "bind": F("str", "0.0.0.0", None, "Bind IP")}},
    "rabbitmq": {"name": "RabbitMQ", "cat": "apps", "icon": "queue", "color": "#f97316",
        "playbook": "30-rabbitmq.yml", "group": "mq", "role": "mq", "version": "3.12",
        "services": {"debian": ["rabbitmq-server"], "rhel": ["rabbitmq-server"]}, "min": 1, "max": 3,
        "config": {"user": F("str", "autoinfra", V.username, "Admin user"), "management_ui": F("bool", True, None, "Management plugin")}},
    "nginx": {"name": "Nginx (SSL)", "cat": "apps", "icon": "globe", "color": "#16a34a",
        "playbook": "31-nginx.yml", "group": "nginx", "role": "app", "version": "1.24",
        "services": {"debian": ["nginx"], "rhel": ["nginx"]}, "min": 1, "max": 10,
        "config": {"upstream": F("str", "", None, "Upstream (ip:port) e.g. CloudStack VIP:8080"),
                   "server_name": F("str", "_", None, "Server name"), "ssl": F("bool", True, None, "Self-signed offline certificate")}},
    "haproxy": {"name": "HAProxy + Keepalived", "cat": "apps", "icon": "split", "color": "#0f766e",
        "playbook": "21-cloudstack-lb.yml", "group": "cs_lb", "role": "lb", "version": "2.8",
        "services": {"debian": ["haproxy", "keepalived"], "rhel": ["haproxy", "keepalived"]}, "min": 1, "max": 3,
        "config": {"vip": F("str", "", lambda v: V.ipv4(v) if v else "VIP is required", "Management VIP")}},
    "nfs": {"name": "Primary / Secondary Storage (NFS)", "cat": "apps", "icon": "disk", "color": "#9333ea",
        "playbook": "22-nfs.yml", "group": "cs_nfs", "role": "storage", "version": "4",
        "services": {"debian": ["nfs-server"], "rhel": ["nfs-server"]}, "min": 1, "max": 2,
        "config": {"primary": F("str", "/export/primary", V.abspath, "Primary export"), "secondary": F("str", "/export/secondary", V.abspath, "Secondary export"),
                   "allowed_cidr": F("str", "", lambda v: V.cidr(v) if v else None, "Allowed network (CIDR)")}},
    "kvm-libvirt": {"name": "KVM / Libvirt", "cat": "kvm", "icon": "chip", "color": "#4f46e5",
        "playbook": "26-kvm-libvirt.yml", "group": "kvm", "role": "compute", "version": "distro",
        "services": {"debian": ["libvirtd"], "rhel": ["libvirtd"]}, "min": 1, "max": 500,
        "config": {"nested": F("bool", False, None, "Enable nested virtualization")}},
    "zabbix-server": {"name": "Zabbix Monitoring", "cat": "monitoring", "icon": "pulse", "color": "#dc2626",
        "playbook": "40-zabbix-server.yml", "group": "zabbix_server", "role": "monitoring", "version": "7.0",
        "services": {"debian": ["zabbix-server", "nginx"], "rhel": ["zabbix-server", "nginx"]}, "min": 1, "max": 1,
        "config": {"web_port": F("int", 8088, V.port, "Frontend port")}},
    "zabbix-agent": {"name": "Zabbix Agent", "cat": "monitoring", "icon": "pulse", "color": "#b91c1c",
        "playbook": "41-zabbix-agent.yml", "group": "zabbix_agent", "role": None, "version": "7.0",
        "services": {"debian": ["zabbix-agent2"], "rhel": ["zabbix-agent2"]}, "min": 1, "max": 1000,
        "config": {"server": F("str", "", lambda v: V.host_or_ip(v) if v else None, "Zabbix server (empty = master)")}},
    "ollama": {"name": "Ollama AI Platform", "cat": "ai", "icon": "spark", "color": "#111827",
        "playbook": "42-ollama.yml", "group": "ollama", "role": "ai", "version": "0.3",
        "services": {"debian": ["ollama"], "rhel": ["ollama"]}, "min": 1, "max": 1,
        "config": {"listen": F("str", "0.0.0.0:11434", None, "Listen address"), "model": F("str", "llama3.1:8b", None, "Model to import")}},
}
CATEGORIES = [("all", "All Components"), ("core", "Core Components"), ("databases", "Databases"), ("monitoring", "Monitoring"),
              ("kvm", "KVM / Libvirt"), ("apps", "Applications"), ("ai", "AI / Ollama")]


def comp_row(cid):
    r = db.one("SELECT * FROM components WHERE id=?", (cid,))
    if not r:
        db.ex("INSERT OR IGNORE INTO components(id, updated) VALUES(?,?)", (cid, time.time()))
        r = db.one("SELECT * FROM components WHERE id=?", (cid,))
    r["hosts"] = json.loads(r["hosts"] or "[]")
    r["config"] = json.loads(r["config"] or "{}")
    return r


def config_of(cid):
    c = COMPONENTS[cid]
    cfg = {k: f["default"] for k, f in c["config"].items()}
    cfg.update(comp_row(cid)["config"])
    return cfg


def validate_config(cid, cfg):
    errs = {}
    for k, v in cfg.items():
        f = COMPONENTS[cid]["config"].get(k)
        if not f:
            errs[k] = "unknown setting"
            continue
        if f["type"] == "int":
            try:
                v = int(v)
            except (TypeError, ValueError):
                errs[k] = f"{k} must be a whole number"
                continue
        if f["type"] == "bool" and not isinstance(v, bool):
            errs[k] = f"{k} must be true/false"
            continue
        if f["type"] == "choice" and v not in f["options"]:
            errs[k] = f"{k} must be one of {', '.join(f['options'])}"
            continue
        if f["check"]:
            e = f["check"](v)
            if e:
                errs[k] = e
    return errs


def card(cid):
    c, r = COMPONENTS[cid], comp_row(cid)
    return {"id": cid, "name": c["name"], "cat": c["cat"], "icon": c["icon"], "color": c["color"], "version": r["version"] or c["version"],
            "state": r["state"], "hosts": r["hosts"], "config": config_of(cid), "last_job": r["last_job"], "updated": r["updated"],
            "fields": {k: {kk: vv for kk, vv in f.items() if kk != "check"} for k, f in c["config"].items()},
            "role": c["role"], "source": "Local Repository", "min": c["min"], "max": c["max"]}


def playbook_for(cid, cfg):
    if cid == "mysql":
        return drivers.DB_STRATEGIES[cfg.get("strategy", "gtid_agent")]["playbook"]
    return COMPONENTS[cid]["playbook"]


def component_vars(cid, cfg, hosts):
    ev = {f"comp_{k}": v for k, v in cfg.items()}
    if cid == "mysql":
        ips = [h["ip"] for h in hosts]
        ev.update({"db_strategy": cfg["strategy"], "db_port": int(cfg["port"]), "db_ips": ips, "db_vip": cfg.get("vip") or (ips[0] if ips else ""),
                   "db_single": cfg["strategy"] == "single", "db_acs_admin_password": inventory.secret("db_acs_admin_password", 24),
                   "db_dbha_password": inventory.secret("db_dbha_password", 24), "db_vrrp_pass": inventory.secret("db_vrrp_pass", 8),
                   "db_root_password": inventory.secret("db_root_password", 24), "mysql_datadir": cfg.get("data_path", "/var/lib/mysql")})
    elif cid == "rabbitmq":
        ev.update({"rabbitmq_user": cfg["user"], "rabbitmq_password": inventory.secret("rabbitmq_password", 20),
                   "rabbitmq_cookie": inventory.secret("rabbitmq_cookie", 32), "rabbitmq_management": cfg["management_ui"]})
    elif cid == "postgresql":
        ev.update({"pg_port": int(cfg["port"]), "pg_listen": cfg["listen"], "pg_allowed_cidr": cfg["allowed_cidr"],
                   "pg_admin_password": inventory.secret("pg_admin_password", 24), "pg_data_path": cfg.get("data_path", "")})
    elif cid == "mongodb":
        ev.update({"mongo_port": int(cfg["port"]), "mongo_replset": cfg["replset"], "mongo_bind": cfg["bind"],
                   "mongo_admin_password": inventory.secret("mongo_admin_password", 24)})
    elif cid == "nginx":
        ev.update({"nginx_upstream": cfg["upstream"], "nginx_server_name": cfg["server_name"], "nginx_ssl": cfg["ssl"]})
    elif cid == "haproxy":
        ev.update({"cs_mgmt_vip": cfg["vip"], "cs_mgmt_vrrp_pass": inventory.secret("cs_mgmt_vrrp_pass", 8)})
    elif cid == "nfs":
        ev.update({"nfs_exports": [{"name": "primary", "path": cfg["primary"]}, {"name": "secondary", "path": cfg["secondary"]}],
                   "nfs_allowed_cidrs": [cfg["allowed_cidr"]] if cfg.get("allowed_cidr") else []})
    elif cid == "zabbix-server":
        ev.update({"zabbix_web_port": int(cfg["web_port"]), "zabbix_db_password": inventory.secret("zabbix_db_password", 24)})
    elif cid == "zabbix-agent":
        ev.update({"zabbix_server": cfg.get("server") or inventory.master_ip()})
    elif cid == "ollama":
        ev.update({"ollama_listen": cfg["listen"], "ollama_model": cfg["model"]})
    elif cid == "kvm-libvirt":
        ev.update({"kvm_nested": cfg["nested"]})
    elif cid == "cloudstack-agent":
        ev.update({"kvm_bridge_name": cfg["bridge"], "kvm_libvirt_tcp": cfg["libvirt_tcp"], "kvm_manage_network": bool(cfg.get("auto_bridge"))})
    elif cid == "systemvm-templates":
        ev.update({"systemvm_secondary": cfg.get("secondary", ""), "systemvm_template_url": inventory.repo_base() + "/" + cfg["template"] if cfg.get("template") else ""})
    elif cid == "oracle":
        ev.update({"oracle_base": cfg["oracle_base"], "oracle_installer": cfg.get("installer_zip", "")})
    elif cid == "cloudstack-management":
        ev.update({"cs_ui_port": int(cfg["ui_port"])})
    return ev


def log_action(cid, action, status, message="", job_id=None, user=None):
    db.ex("INSERT INTO component_actions(ts,component,action,status,message,user,job_id) VALUES(?,?,?,?,?,?,?)",
          (time.time(), cid, action, status, message[:300], user or "system", job_id))


def _hook(job, ok):
    meta = json.loads(job["meta"] or "{}")
    cid, action = meta.get("component"), meta.get("action", "deploy")
    if cid not in COMPONENTS:
        return
    if action == "deploy":
        db.ex("UPDATE components SET state=?, updated=? WHERE id=?", ("installed" if ok else "failed", time.time(), cid))
    log_action(cid, action, "Completed" if ok else "Failed", f"{COMPONENTS[cid]['name']} {action} {'completed' if ok else 'failed'}",
               job["id"], job["by_user"])


jobs.HOOKS["component"] = _hook


@jobs.register("component_status")
def component_status(log, cid):
    c = COMPONENTS[cid]
    r = comp_row(cid)
    results = {}
    for hid in r["hosts"]:
        h = db.one("SELECT * FROM hosts WHERE id=?", (hid,))
        if not h:
            continue
        svcs = c["services"].get(h.get("os_family") or "debian", [])
        if not svcs:
            results[h["ip"]] = "n/a"
            continue
        try:
            cl = sshops.connect(h, key_path=inventory.key_path())
            try:
                rc, out, _ = sshops.run(cl, " ; ".join(f"systemctl is-active {s} 2>/dev/null | head -1" for s in svcs), timeout=30)
            finally:
                cl.close()
            states = out.split()
            results[h["ip"]] = "running" if states and all(s == "active" for s in states) else "stopped"
        except Exception as e:  # noqa: BLE001
            results[h["ip"]] = f"unreachable ({e})"
        log(f"{h['ip']}: {results[h['ip']]}")
    if results:
        st = "running" if all(v == "running" for v in results.values()) else "degraded"
        if r["state"] in ("installed", "running", "degraded"):
            db.ex("UPDATE components SET state=?, updated=? WHERE id=?", (st, time.time(), cid))
    return True


# ------------------------------------------------------------------ pages + API
@bp.route("/components")
@auth.login_required
def page():
    return render_template("components.html", categories=CATEGORIES)


@bp.route("/api/components")
@auth.login_required
def list_components():
    cat = request.args.get("cat", "all")
    cards = [card(c) for c in COMPONENTS if cat in ("all", COMPONENTS[c]["cat"])]
    acts = db.q("SELECT * FROM component_actions ORDER BY id DESC LIMIT 20")
    for a in acts:
        a["component_name"] = COMPONENTS.get(a["component"], {}).get("name", a["component"])
    return jsonify(components=cards, actions=acts, categories=CATEGORIES)


@bp.route("/api/components/<cid>")
@auth.login_required
def get_component(cid):
    if cid not in COMPONENTS:
        return jsonify(error="unknown component"), 404
    return jsonify(component=card(cid))


@bp.route("/api/components/<cid>/config", methods=["POST"])
@auth.role_required("operator")
def save_config(cid):
    if cid not in COMPONENTS:
        return jsonify(error="unknown component"), 404
    j = request.get_json(silent=True) or {}
    cfg = config_of(cid)
    cfg.update(j)
    errs = validate_config(cid, cfg)
    if errs:
        return jsonify(error="validation failed", fields=errs), 400
    for k, f in COMPONENTS[cid]["config"].items():
        if f["type"] == "int":
            cfg[k] = int(cfg[k])
    db.ex("UPDATE components SET config=?, updated=? WHERE id=?", (json.dumps(cfg), time.time(), cid))
    log_action(cid, "Configure", "Completed", "configuration saved", user=auth.g.user["username"])
    auth.audit("component.config", cid, json.dumps(cfg))
    return jsonify(ok=True, config=cfg)


@bp.route("/api/components/<cid>/deploy", methods=["POST"])
@auth.role_required("operator")
def deploy(cid):
    if cid not in COMPONENTS:
        return jsonify(error="unknown component"), 404
    c = COMPONENTS[cid]
    j = request.get_json(silent=True) or {}
    ids = j.get("host_ids")
    if ids is None and c["role"]:
        ids = [h["id"] for h in db.q("SELECT id FROM hosts WHERE (','||roles||',') LIKE ?", (f"%,{c['role']},%",))]
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        return jsonify(error="host_ids must be a list of host ids"), 400
    hosts = [db.one("SELECT * FROM hosts WHERE id=?", (i,)) for i in ids]
    if not ids or None in hosts:
        return jsonify(error="select existing target hosts"), 400
    cfg = config_of(cid)
    if j.get("config"):
        cfg.update(j["config"])
    errs = validate_config(cid, cfg)
    if errs:
        return jsonify(error="validation failed", fields=errs), 400
    if not c["min"] <= len(ids) <= c["max"]:
        return jsonify(error=f"{c['name']} needs {c['min']}-{c['max']} host(s); {len(ids)} selected"), 400
    if cid == "mysql":
        st = drivers.DB_STRATEGIES[cfg["strategy"]]
        if len(ids) not in st["nodes"]:
            return jsonify(error=f"{st['name']} needs {' or '.join(map(str, st['nodes']))} node(s)"), 400
        if len(ids) > 1 and not cfg.get("vip"):
            return jsonify(error="a VIP is required for a 2-3 node MySQL set"), 400
    if cid == "haproxy":
        clash = [h["ip"] for h in hosts if "management" in (h.get("roles") or "").split(",")]
        if clash:
            return jsonify(error="HAProxy cannot run on a CloudStack management server (both use ports 8080 and 8250): " + ", ".join(clash)), 400
    not_ready = [h["ip"] for h in hosts if h["status"] not in ("ready", "ready-with-warnings")]
    if not_ready and not j.get("force"):
        return jsonify(error=f"run the pre-deployment checks first; not ready: {', '.join(not_ready)}"), 409
    all_hosts = db.q("SELECT * FROM hosts")
    pb = playbook_for(cid, cfg)
    steps, wd = inventory.playbook_steps([(pb, f"{c['name']}: {pb}")], all_hosts, extra_vars=component_vars(cid, cfg, hosts),
                                         extra_groups={c["group"]: ids}, check=bool(j.get("check")))
    db.ex("UPDATE components SET state='deploying', hosts=?, config=?, version=?, updated=? WHERE id=?",
          (json.dumps(ids), json.dumps(cfg), j.get("version") or c["version"], time.time(), cid))
    jid = jobs.start("component", f"Deploy {c['name']}", steps, meta={"component": cid, "action": "deploy", "workdir": wd})
    db.ex("UPDATE components SET last_job=? WHERE id=?", (jid, cid))
    log_action(cid, "Deploy", "Running", f"deploying to {len(ids)} host(s)", jid, auth.g.user["username"])
    auth.audit("component.deploy", cid, f"job {jid} hosts={ids}")
    return jsonify(job_id=jid), 202


@bp.route("/api/components/<cid>/status", methods=["POST"])
@auth.role_required("operator")
def refresh_status(cid):
    if cid not in COMPONENTS:
        return jsonify(error="unknown component"), 404
    jid = jobs.start("component-status", f"Status of {COMPONENTS[cid]['name']}", [{"name": "service status", "fn": "component_status",
                     "args": {"cid": cid}}], meta={"component": cid, "action": "status"})
    return jsonify(job_id=jid), 202


@bp.route("/api/components/<cid>/reset", methods=["POST"])
@auth.role_required("admin")
def reset(cid):
    if cid not in COMPONENTS:
        return jsonify(error="unknown component"), 404
    db.ex("UPDATE components SET state='not_installed', hosts='[]', updated=? WHERE id=?", (time.time(), cid))
    log_action(cid, "Reset", "Completed", "marked as not installed (nothing removed from hosts)", user=auth.g.user["username"])
    auth.audit("component.reset", cid)
    return jsonify(ok=True)


# ------------------------------------------------------------------ Automation: run any shipped playbook (admin)
def shipped_playbooks():
    import glob, os
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(inventory.ansible_dir(), "[0-9]*.yml")))


@bp.route("/api/ansible/playbooks")
@auth.login_required
def list_playbooks():
    return jsonify(playbooks=shipped_playbooks())


@bp.route("/api/ansible/run", methods=["POST"])
@auth.role_required("admin")
def run_playbook():
    j = request.get_json(silent=True) or {}
    pb = j.get("playbook")
    if pb not in shipped_playbooks():
        return jsonify(error="unknown playbook"), 400
    ids = j.get("host_ids") or [h["id"] for h in db.q("SELECT id FROM hosts")]
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        return jsonify(error="host_ids must be a list of host ids"), 400
    hosts = [h for h in db.q("SELECT * FROM hosts") if h["id"] in ids]
    if not hosts:
        return jsonify(error="no hosts selected"), 400
    tags = j.get("tags") or None
    if tags and (not isinstance(tags, list) or not all(isinstance(t, str) and t.replace("_", "").isalnum() for t in tags)):
        return jsonify(error="tags must be a list of words"), 400
    ev = j.get("extra_vars") or {}
    from .hosts import validate_vars
    e = validate_vars(ev)
    if e:
        return jsonify(error=e), 400
    steps, wd = inventory.playbook_steps([pb], hosts, extra_vars=ev, tags=tags, check=bool(j.get("check")))
    jid = jobs.start("ansible", f"Run {pb} on {len(hosts)} host(s)", steps, meta={"playbook": pb, "workdir": wd})
    auth.audit("ansible.run", pb, f"job {jid} hosts={ids}")
    return jsonify(job_id=jid), 202
