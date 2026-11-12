"""Monitoring: host metrics over SSH, threshold alerts, topology, logs & events, optional Zabbix problems."""
import json, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, jsonify, render_template, request

from . import auth, components, csapi, db, inventory, jobs, sshops

bp = Blueprint("monitoring", __name__)
DEFAULT_RULES = {"cpu_warn": 80, "cpu_crit": 95, "mem_warn": 85, "mem_crit": 95, "disk_warn": 80, "disk_crit": 90}
_bg = {"thread": None, "stop": threading.Event()}


def rules():
    r = dict(DEFAULT_RULES)
    r.update(db.get_setting("alert_rules", {}) or {})
    return r


def raise_alert(key, severity, source, message, host_id=None):
    now = time.time()
    a = db.one("SELECT * FROM alerts WHERE akey=? AND resolved=0", (key,))
    if a:
        db.ex("UPDATE alerts SET last_seen=?, severity=?, message=? WHERE id=?", (now, severity, message, a["id"]))
        return a["id"]
    return db.ex("INSERT INTO alerts(ts,severity,source,message,host_id,akey,last_seen) VALUES(?,?,?,?,?,?,?)",
                 (now, severity, source, message, host_id, key, now))


def resolve(key):
    db.ex("UPDATE alerts SET resolved=1, last_seen=? WHERE akey=? AND resolved=0", (time.time(), key))


def evaluate_metrics(h, m):
    """Raise/resolve threshold alerts for one host's metric sample."""
    r = rules()
    name = h.get("hostname") or h["ip"]
    for k, label in (("cpu", "CPU usage"), ("mem", "Memory usage"), ("disk", "Storage usage")):
        v = m.get(k)
        key = f"{k}:{h['id']}"
        if v is None:
            continue
        if v >= r[f"{k}_crit"]:
            raise_alert(key, "critical", name, f"{label} {v:.0f}% on {name} (>= {r[f'{k}_crit']}%)", h["id"])
        elif v >= r[f"{k}_warn"]:
            raise_alert(key, "warning", name, f"High {label.lower()} {v:.0f}% on {name} (>= {r[f'{k}_warn']}%)", h["id"])
        else:
            resolve(key)


def collect_host(h):
    try:
        c = sshops.connect(h, key_path=inventory.key_path(), timeout=8)
        try:
            rc, out, _ = sshops.run_script(c, sshops.METRICS, timeout=30)
        finally:
            c.close()
        m = sshops.parse_metrics(out)
        db.ex("INSERT INTO metrics(ts,host_id,cpu,mem,disk,net_rx,net_tx,load1) VALUES(?,?,?,?,?,?,?,?)",
              (time.time(), h["id"], m["cpu"], m["mem"], m["disk"], m["net_rx"], m["net_tx"], m["load"]))
        resolve(f"down:{h['id']}")
        evaluate_metrics(h, m)
        return m
    except Exception as e:  # noqa: BLE001
        raise_alert(f"down:{h['id']}", "critical", h.get("hostname") or h["ip"], f"Host {h.get('hostname') or h['ip']} not reachable ({type(e).__name__})", h["id"])
        return None


def evaluate_platform():
    for c in db.q("SELECT id, state FROM components"):
        key = f"component:{c['id']}"
        nm = components.COMPONENTS.get(c["id"], {}).get("name", c["id"])
        if c["state"] == "failed":
            raise_alert(key, "critical", nm, f"{nm} deployment failed - see Deployment Jobs")
        elif c["state"] == "degraded":
            raise_alert(key, "warning", nm, f"{nm}: one or more services are not running")
        else:
            resolve(key)
    for j in db.q("SELECT id, title FROM jobs WHERE state='failed' AND finished > ?", (time.time() - 86400,)):
        raise_alert(f"job:{j['id']}", "warning", "jobs", f"Job #{j['id']} failed: {j['title']}")


@jobs.register("collect_metrics")
def collect_all(log=print):
    hosts = db.q("SELECT * FROM hosts WHERE key_ok=1")
    pin = (db.path(), db.data_dir())

    def one(h):
        db.use(*pin)
        return collect_host(h)
    with ThreadPoolExecutor(max_workers=16) as ex:
        res = list(ex.map(one, hosts))
    for h, m in zip(hosts, res):
        log(f"{h['ip']}: " + (f"cpu {m['cpu']:.0f}% mem {m['mem']:.0f}% disk {m['disk']:.0f}%" if m else "unreachable"))
    evaluate_platform()
    keep = float(db.get_setting("metrics_retention_days", 14)) * 86400
    db.ex("DELETE FROM metrics WHERE ts < ?", (time.time() - keep,))
    return True


def start_background(app):
    def loop():
        while not _bg["stop"].wait(5):
            iv = int(db.get_setting("monitor_interval", 300) or 0)
            last = float(db.get_setting("monitor_last", 0) or 0)
            if iv > 0 and time.time() - last >= iv:
                db.set_setting("monitor_last", time.time())
                try:
                    collect_all(lambda *_: None)
                except Exception:  # noqa: BLE001
                    pass
    if _bg["thread"] is None:
        t = threading.Thread(target=loop, daemon=True, name="autoinfra-monitor")
        _bg["thread"] = t
        t.start()


def latest():
    rows = db.q("SELECT m.* FROM metrics m JOIN (SELECT host_id, MAX(ts) mts FROM metrics GROUP BY host_id) x "
                "ON m.host_id=x.host_id AND m.ts=x.mts")
    return {r["host_id"]: r for r in rows}


def cs_counts():
    try:
        from . import cloudstack
        cs = cloudstack.api_client()
        vms = cs.items("listVirtualMachines", "virtualmachine", listall="true")
        pools = cs.items("listStoragePools", "storagepool")
        return {"configured": True, "vms": len(vms), "vms_running": sum(v.get("state") == "Running" for v in vms),
                "vms_stopped": sum(v.get("state") == "Stopped" for v in vms),
                "storage_total": sum(int(p.get("disksizetotal", 0) or 0) for p in pools),
                "storage_used": sum(int(p.get("disksizeused", 0) or 0) for p in pools)}
    except Exception as e:  # noqa: BLE001
        return {"configured": False, "error": str(e), "vms": 0, "vms_running": 0, "vms_stopped": 0, "storage_total": 0, "storage_used": 0}


def overview():
    hosts = db.q("SELECT * FROM hosts")
    lm = latest()
    online = sum(1 for h in hosts if h["status"] in ("ready", "ready-with-warnings", "not-ready"))
    vals = lambda k: [m[k] for m in lm.values() if m.get(k) is not None]
    avg = lambda k: round(sum(vals(k)) / len(vals(k)), 1) if vals(k) else 0
    disk_total_gb = 0
    for h in hosts:
        try:
            disk_total_gb += int(json.loads(h["facts"] or "{}").get("DISK_TOTAL_GB", 0) or 0)
        except ValueError:
            pass
    dbs = [c for c in db.q("SELECT id, state FROM components") if components.COMPONENTS.get(c["id"], {}).get("cat") == "databases"
           and c["state"] not in ("not_installed",)]
    al = db.q("SELECT severity, COUNT(*) n FROM alerts WHERE resolved=0 GROUP BY severity")
    alc = {a["severity"]: a["n"] for a in al}
    cs = cs_counts()
    net = sum(vals("net_rx")) + sum(vals("net_tx"))
    return {"hosts": {"total": len(hosts), "online": online, "offline": len(hosts) - online},
            "vms": {"total": cs["vms"], "running": cs["vms_running"], "stopped": cs["vms_stopped"], "source": "cloudstack" if cs["configured"] else "none"},
            "databases": {"total": len(dbs), "healthy": sum(c["state"] in ("installed", "running") for c in dbs),
                          "warning": sum(c["state"] in ("degraded", "failed") for c in dbs)},
            "storage": {"total_bytes": cs["storage_total"] or disk_total_gb * 1024 ** 3, "used_bytes": cs["storage_used"],
                        "source": "cloudstack" if cs["storage_total"] else "hosts"},
            "alerts": {"critical": alc.get("critical", 0), "warning": alc.get("warning", 0), "total": sum(alc.values())},
            "gauges": {"cpu": avg("cpu"), "mem": avg("mem"), "disk": avg("disk"), "net_bytes_s": net},
            "samples": len(lm)}


# ------------------------------------------------------------------ pages + API
@bp.route("/monitoring")
@auth.login_required
def page():
    return render_template("monitoring.html")


@bp.route("/api/monitor/overview")
@auth.login_required
def api_overview():
    return jsonify(overview())


@bp.route("/api/monitor/hosts")
@auth.login_required
def api_hosts():
    lm = latest()
    out = []
    for h in db.q("SELECT id, ip, hostname, roles, status, os_pretty FROM hosts ORDER BY id"):
        m = lm.get(h["id"], {})
        out.append(dict(h, cpu=m.get("cpu"), mem=m.get("mem"), disk=m.get("disk"), load=m.get("load1"), ts=m.get("ts")))
    return jsonify(hosts=out)


@bp.route("/api/monitor/metrics/<int:hid>")
@auth.login_required
def api_metrics(hid):
    hours = min(24 * 14, max(1, float(request.args.get("hours", 24))))
    return jsonify(series=db.q("SELECT ts,cpu,mem,disk,net_rx,net_tx,load1 FROM metrics WHERE host_id=? AND ts>? ORDER BY ts",
                               (hid, time.time() - hours * 3600)))


@bp.route("/api/monitor/collect", methods=["POST"])
@auth.role_required("operator")
def api_collect():
    jid = jobs.start("monitor", "Collect host metrics", [{"name": "collect metrics", "fn": "collect_metrics"}])
    return jsonify(job_id=jid), 202


@bp.route("/api/alerts")
@auth.login_required
def api_alerts():
    show = request.args.get("show", "open")
    sql = "SELECT * FROM alerts" + (" WHERE resolved=0" if show == "open" else "") + " ORDER BY (severity='critical') DESC, last_seen DESC LIMIT 500"
    return jsonify(alerts=db.q(sql))


@bp.route("/api/alerts/<int:aid>/ack", methods=["POST"])
@auth.role_required("operator")
def api_ack(aid):
    if not db.one("SELECT id FROM alerts WHERE id=?", (aid,)):
        return jsonify(error="no such alert"), 404
    db.ex("UPDATE alerts SET ack=1 WHERE id=?", (aid,))
    auth.audit("alert.ack", aid)
    return jsonify(ok=True)


@bp.route("/api/alerts/<int:aid>/resolve", methods=["POST"])
@auth.role_required("operator")
def api_resolve(aid):
    if not db.one("SELECT id FROM alerts WHERE id=?", (aid,)):
        return jsonify(error="no such alert"), 404
    db.ex("UPDATE alerts SET resolved=1 WHERE id=?", (aid,))
    auth.audit("alert.resolve", aid)
    return jsonify(ok=True)


@bp.route("/api/monitor/topology")
@auth.login_required
def api_topology():
    """Management -> CloudStack (zones/pods/clusters/hosts) and OpenStack (controllers/compute/network/storage)."""
    from . import cloudstack, openstack
    hosts = {h["id"]: h for h in db.q("SELECT * FROM hosts")}
    lm = latest()
    hn = lambda i: {"id": i, "name": hosts[i].get("hostname") or hosts[i]["ip"], "ip": hosts[i]["ip"], "status": hosts[i]["status"],
                    "cpu": (lm.get(i) or {}).get("cpu")} if i in hosts else None
    mgmt = [hn(h["id"]) for h in hosts.values() if "management" in (h["roles"] or "").split(",")]
    cs_tree = []
    res = cloudstack.topology().get_json()
    if res.get("configured"):
        cs_tree = res["zones"]
    else:
        s = cloudstack.state()
        if s["cluster"]["clusters"] and any(c["host_ids"] for c in s["cluster"]["clusters"]):
            cs_tree = [{"name": s["zone"]["name"], "state": "planned", "pods": [{"name": s["zone"]["pod_name"], "clusters": [
                {"name": c["name"], "hosts": [x for x in (hn(i) for i in c["host_ids"]) if x]} for c in s["cluster"]["clusters"]]}]}]
    o = openstack.state()["nodes"]
    os_tree = {k: [x for x in (hn(i) for i in o.get(f"{k}_ids", [])) if x] for k in ("controller", "compute", "network", "storage")}
    storage = [hn(h["id"]) for h in hosts.values() if "storage" in (h["roles"] or "").split(",")]
    return jsonify(management=mgmt, master={"ip": inventory.master_ip()}, cloudstack=cs_tree, openstack=os_tree, storage=storage)


@bp.route("/api/logs")
@auth.login_required
def api_logs():
    """Logs & events: jobs, alerts and audit entries merged on one timeline."""
    lim = min(1000, int(request.args.get("limit", 200)))
    q = request.args.get("q", "")
    ev = []
    for j in db.q("SELECT id, title, state, created, by_user FROM jobs ORDER BY id DESC LIMIT ?", (lim,)):
        ev.append({"ts": j["created"], "type": "job", "level": "error" if j["state"] == "failed" else "info",
                   "message": f"Job #{j['id']} {j['title']} - {j['state']}", "user": j["by_user"], "ref": j["id"]})
    for a in db.q("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (lim,)):
        ev.append({"ts": a["ts"], "type": "alert", "level": a["severity"], "message": a["message"], "user": "", "ref": a["id"]})
    for a in db.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (lim,)):
        ev.append({"ts": a["ts"], "type": "audit", "level": "info" if a["ok"] else "warning",
                   "message": f"{a['action']} {a['target']}".strip(), "user": a["user"], "ref": a["id"]})
    if q:
        ev = [e for e in ev if q.lower() in e["message"].lower()]
    ev.sort(key=lambda e: e["ts"], reverse=True)
    return jsonify(events=ev[:lim])


@bp.route("/api/zabbix/problems")
@auth.login_required
def zabbix_problems():
    url, token = db.get_setting("zabbix_url"), inventory.secret_get("zabbix_token")
    if not url or not token:
        return jsonify(configured=False, problems=[])
    body = json.dumps({"jsonrpc": "2.0", "method": "problem.get", "params": {"output": "extend", "recent": True, "sortfield": ["eventid"],
                       "sortorder": "DESC", "limit": 100}, "id": 1}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api_jsonrpc.php", data=body, headers={"Content-Type": "application/json-rpc",
                                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.load(r)
        if "error" in res:
            return jsonify(configured=True, error=res["error"].get("data") or res["error"].get("message"), problems=[]), 502
        sev = {"0": "not classified", "1": "information", "2": "warning", "3": "average", "4": "high", "5": "disaster"}
        return jsonify(configured=True, problems=[{"id": p["eventid"], "name": p["name"], "severity": sev.get(str(p.get("severity")), p.get("severity")),
                                                   "clock": int(p.get("clock", 0))} for p in res.get("result", [])])
    except Exception as e:  # noqa: BLE001
        return jsonify(configured=True, error=str(e), problems=[]), 502
