"""Monitoring (alerts, overview, topology, logs, Zabbix), Ollama assistant, PDF/Excel reports, billing, settings, users, search."""
import io, json, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import add_host, mark_ready, wait_job


def put_metric(app, hid, cpu, mem, disk, ts=None):
    from autoinfra import db, monitoring
    with app.app_context():
        db.ex("INSERT INTO metrics(ts,host_id,cpu,mem,disk,net_rx,net_tx,load1) VALUES(?,?,?,?,?,?,?,?)", (ts or time.time(), hid, cpu, mem, disk, 1000, 500, 0.5))
        h = db.one("SELECT * FROM hosts WHERE id=?", (hid,))
        monitoring.evaluate_metrics(h, {"cpu": cpu, "mem": mem, "disk": disk})


@pytest.mark.parametrize("cpu,mem,disk,expect", [
    (10, 10, 10, {}), (85, 10, 10, {"cpu": "warning"}), (96, 10, 10, {"cpu": "critical"}), (10, 90, 10, {"mem": "warning"}),
    (10, 99, 10, {"mem": "critical"}), (10, 10, 85, {"disk": "warning"}), (10, 10, 95, {"disk": "critical"}),
    (96, 99, 95, {"cpu": "critical", "mem": "critical", "disk": "critical"}), (80, 85, 80, {"cpu": "warning", "mem": "warning", "disk": "warning"}),
    (79.9, 84.9, 79.9, {}),
])
def test_threshold_alerts(app, operator, cpu, mem, disk, expect):
    hid = add_host(operator, "10.0.0.1", hostname="kvm-01")
    put_metric(app, hid, cpu, mem, disk)
    al = operator.get("/api/alerts").get_json()["alerts"]
    got = {a["akey"].split(":")[0]: a["severity"] for a in al}
    assert got == expect
    for a in al:
        assert "kvm-01" in a["message"]


def test_alert_dedup_and_resolve(app, operator):
    hid = add_host(operator, "10.0.0.1")
    for _ in range(3):
        put_metric(app, hid, 97, 10, 10)
    assert len(operator.get("/api/alerts").get_json()["alerts"]) == 1
    put_metric(app, hid, 10, 10, 10)
    assert operator.get("/api/alerts").get_json()["alerts"] == []
    assert len(operator.get("/api/alerts?show=all").get_json()["alerts"]) == 1


def test_custom_alert_rules(app, admin):
    hid = add_host(admin, "10.0.0.1")
    assert admin.post("/api/settings", json={"alert_rules": {"cpu_warn": 50}}).status_code == 200
    put_metric(app, hid, 55, 10, 10)
    assert admin.get("/api/alerts").get_json()["alerts"][0]["severity"] == "warning"


def test_alert_ack_resolve_api(app, operator):
    hid = add_host(operator, "10.0.0.1")
    put_metric(app, hid, 99, 10, 10)
    aid = operator.get("/api/alerts").get_json()["alerts"][0]["id"]
    assert operator.post(f"/api/alerts/{aid}/ack").status_code == 200
    assert operator.get("/api/notifications").get_json()["count"] == 0
    assert operator.post(f"/api/alerts/{aid}/resolve").status_code == 200
    assert operator.post("/api/alerts/999/ack").status_code == 404
    assert operator.post("/api/alerts/999/resolve").status_code == 404


def test_platform_alerts(app, operator):
    from autoinfra import db, monitoring
    with app.app_context():
        db.ex("INSERT INTO components(id,state) VALUES('mysql','failed')")
        db.ex("INSERT INTO jobs(kind,title,state,created,finished) VALUES('x','Deploy X','failed',?,?)", (time.time(), time.time()))
        monitoring.evaluate_platform()
    msgs = [a["message"] for a in operator.get("/api/alerts").get_json()["alerts"]]
    assert any("MySQL Database deployment failed" in m for m in msgs) and any("Deploy X" in m for m in msgs)


def test_collect_unreachable_host_alert(app, operator):
    hid = add_host(operator, "127.0.0.1", ssh_port=1, hostname="dead")
    from autoinfra import db
    with app.app_context():
        db.ex("UPDATE hosts SET key_ok=1 WHERE id=?", (hid,))
    j = wait_job(operator, operator.post("/api/monitor/collect").get_json()["job_id"])
    assert j["state"] == "success"
    al = operator.get("/api/alerts").get_json()["alerts"]
    assert al[0]["severity"] == "critical" and "not reachable" in al[0]["message"]


def test_overview_and_gauges(app, operator):
    a = add_host(operator, "10.0.0.1")
    b = add_host(operator, "10.0.0.2")
    mark_ready(app, a)
    put_metric(app, a, 40, 60, 50)
    put_metric(app, b, 60, 80, 70)
    o = operator.get("/api/monitor/overview").get_json()
    assert o["hosts"] == {"total": 2, "online": 1, "offline": 1}
    assert o["gauges"]["cpu"] == 50 and o["gauges"]["mem"] == 70 and o["gauges"]["disk"] == 60 and o["samples"] == 2
    assert o["vms"]["source"] == "none"


def test_overview_with_cloudstack(app, operator, cs_configured):
    cs_configured.vms.update({"v1": {"id": "v1", "state": "Running", "zoneid": "z"}, "v2": {"id": "v2", "state": "Stopped", "zoneid": "z"}})
    cs_configured.pools["p"] = {"id": "p", "name": "p", "zoneid": "z", "disksizetotal": 10 * 1024 ** 4, "disksizeused": 2 * 1024 ** 4}
    o = operator.get("/api/monitor/overview").get_json()
    assert o["vms"] == {"total": 2, "running": 1, "stopped": 1, "source": "cloudstack"}
    assert o["storage"]["total_bytes"] == 10 * 1024 ** 4 and o["storage"]["source"] == "cloudstack"


def test_metrics_series_and_hosts(app, operator):
    hid = add_host(operator, "10.0.0.1")
    put_metric(app, hid, 10, 20, 30, ts=time.time() - 7200)
    put_metric(app, hid, 11, 21, 31)
    assert len(operator.get(f"/api/monitor/metrics/{hid}?hours=1").get_json()["series"]) == 1
    assert len(operator.get(f"/api/monitor/metrics/{hid}?hours=3").get_json()["series"]) == 2
    h = operator.get("/api/monitor/hosts").get_json()["hosts"][0]
    assert h["cpu"] == 11 and h["disk"] == 31


def test_topology_planned_from_wizard(app, operator):
    m = add_host(operator, "10.0.0.10", roles=["management"])
    k = add_host(operator, "10.0.1.21", roles=["compute"])
    s = add_host(operator, "10.0.0.31", roles=["storage"])
    from autoinfra import db
    with app.app_context():
        db.set_setting("cs_wizard", {"cluster": {"clusters": [{"name": "C1", "host_ids": [k]}]}})
        db.set_setting("os_wizard", {"nodes": {"controller_ids": [m], "compute_ids": [k]}})
    t = operator.get("/api/monitor/topology").get_json()
    assert t["management"][0]["ip"] == "10.0.0.10" and t["cloudstack"][0]["state"] == "planned"
    assert t["cloudstack"][0]["pods"][0]["clusters"][0]["hosts"][0]["ip"] == "10.0.1.21"
    assert t["openstack"]["controller"][0]["ip"] == "10.0.0.10" and t["storage"][0]["ip"] == "10.0.0.31"


def test_topology_live_cloudstack(app, operator, cs_configured):
    from autoinfra import csapi
    cs = csapi.CS(cs_configured.url, cs_configured.api_key, cs_configured.secret, poll_interval=0.01)
    z = cs.run("createZone", name="Z", networktype="Advanced", dns1="8.8.8.8", internaldns1="8.8.8.8")["zone"]
    p = cs.run("createPod", zoneid=z["id"], name="P", startip="10.0.0.2", endip="10.0.0.9", netmask="255.255.255.0", gateway="10.0.0.1")["pod"]
    c = cs.run("addCluster", zoneid=z["id"], podid=p["id"], clustername="C", clustertype="CloudManaged", hypervisor="KVM")["cluster"][0]
    cs.run("addHost", zoneid=z["id"], podid=p["id"], clusterid=c["id"], hypervisor="KVM", username="root", url="http://10.0.1.5")
    t = operator.get("/api/monitor/topology").get_json()
    assert t["cloudstack"][0]["name"] == "Z" and t["cloudstack"][0]["pods"][0]["clusters"][0]["hosts"][0]["name"] == "10.0.1.5"


def test_logs_timeline(app, operator):
    add_host(operator, "10.0.0.1")
    ev = operator.get("/api/logs").get_json()["events"]
    assert ev and ev[0]["type"] in ("audit", "job", "alert") and all(ev[i]["ts"] >= ev[i + 1]["ts"] for i in range(len(ev) - 1))
    assert all("host.add" in e["message"] for e in operator.get("/api/logs?q=host.add").get_json()["events"])


def zabbix_server(response):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            H.seen = (body["method"], self.headers.get("Authorization"))
            b = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, H


def test_zabbix_not_configured(operator):
    assert operator.get("/api/zabbix/problems").get_json()["configured"] is False


def test_zabbix_problems(app, admin):
    srv, H = zabbix_server({"jsonrpc": "2.0", "result": [{"eventid": "7", "name": "High CPU on kvm-01", "severity": "4", "clock": "1700000000"}], "id": 1})
    admin.post("/api/settings", json={"zabbix_url": f"http://127.0.0.1:{srv.server_address[1]}", "zabbix_token": "tok123"})
    r = admin.get("/api/zabbix/problems").get_json()
    srv.shutdown()
    assert r["problems"] == [{"id": "7", "name": "High CPU on kvm-01", "severity": "high", "clock": 1700000000}]
    assert H.seen == ("problem.get", "Bearer tok123")


def test_zabbix_error(app, admin):
    srv, H = zabbix_server({"jsonrpc": "2.0", "error": {"message": "x", "data": "Not authorised."}, "id": 1})
    admin.post("/api/settings", json={"zabbix_url": f"http://127.0.0.1:{srv.server_address[1]}", "zabbix_token": "bad"})
    r = admin.get("/api/zabbix/problems")
    srv.shutdown()
    assert r.status_code == 502 and "Not authorised" in r.get_json()["error"]


# ------------------------------------------------------------------ AI
def test_ai_models(operator, ollama):
    r = operator.get("/api/ai/models").get_json()
    assert r["ok"] and r["models"] == ["llama3.1:8b", "mistral:7b"]


def test_ai_models_unreachable(app, operator):
    from autoinfra import db
    with app.app_context():
        db.set_setting("ollama_url", "http://127.0.0.1:1")
    r = operator.get("/api/ai/models").get_json()
    assert r["ok"] is False and "not reachable" in r["error"]


def test_ai_chat_uses_live_context(app, operator, ollama):
    hid = add_host(operator, "10.0.0.7", hostname="kvm-07", roles=["compute"])
    put_metric(app, hid, 97, 40, 40)
    r = operator.post("/api/ai/chat", json={"message": "Explain high CPU usage on kvm-07"}).get_json()
    assert r["ok"] and r["answer"] == ollama.answer
    sent = ollama.calls[-1]
    assert sent["stream"] is False and sent["model"] == "llama3.1:8b"
    sysmsg = sent["messages"][0]["content"]
    assert "kvm-07 (10.0.0.7)" in sysmsg and "cpu=97%" in sysmsg and "[critical] CPU usage 97%" in sysmsg
    assert sent["messages"][-1] == {"role": "user", "content": "Explain high CPU usage on kvm-07"}


def test_ai_history_and_clear(operator, ollama):
    operator.post("/api/ai/chat", json={"message": "hello"})
    operator.post("/api/ai/chat", json={"message": "again"})
    m = operator.get("/api/ai/history").get_json()["messages"]
    assert [x["role"] for x in m] == ["user", "assistant", "user", "assistant"]
    assert len(ollama.calls[-1]["messages"]) == 1 + 3      # system + previous turn + new question
    operator.delete("/api/ai/history")
    assert operator.get("/api/ai/history").get_json()["messages"] == []


def test_ai_history_is_per_user(operator, viewer, ollama):
    operator.post("/api/ai/chat", json={"message": "private"})
    assert viewer.get("/api/ai/history").get_json()["messages"] == []


@pytest.mark.parametrize("msg,code", [("", 400), ("   ", 400), ("x" * 4001, 400)])
def test_ai_bad_messages(operator, ollama, msg, code):
    assert operator.post("/api/ai/chat", json={"message": msg}).status_code == code


def test_ai_ollama_http_error(operator, ollama):
    ollama.status = 404
    r = operator.post("/api/ai/chat", json={"message": "hi"}).get_json()
    assert r["ok"] is False and "HTTP 404" in r["answer"]


def test_ai_ollama_down(app, operator):
    from autoinfra import db
    with app.app_context():
        db.set_setting("ollama_url", "http://127.0.0.1:1")
    r = operator.post("/api/ai/chat", json={"message": "hi"}).get_json()
    assert r["ok"] is False and "not reachable" in r["answer"]


@pytest.mark.parametrize("msg,fmt", [("Generate a PDF report of all components", "pdf"), ("export an excel report", "xlsx"), ("generate report", "pdf")])
def test_ai_report_intent(operator, ollama, msg, fmt):
    r = operator.post("/api/ai/chat", json={"message": msg}).get_json()
    assert r["report"]["fmt"] == fmt and operator.get(r["report"]["url"]).status_code == 200


def test_ai_knowledge_base(operator, ollama):
    r = operator.post("/api/ai/kb", json={"name": "runbook.md", "content": "To fix libvirtd restart loops run virsh capabilities first."})
    did = r.get_json()["id"]
    operator.post("/api/ai/chat", json={"message": "libvirtd keeps restarting"})
    assert "virsh capabilities" in ollama.calls[-1]["messages"][0]["content"]
    operator.post("/api/ai/chat", json={"message": "what is the weather"})
    assert "virsh capabilities" not in ollama.calls[-1]["messages"][0]["content"]
    assert operator.get("/api/ai/kb").get_json()["docs"][0]["name"] == "runbook.md"
    assert operator.delete(f"/api/ai/kb/{did}").status_code == 200 and operator.delete(f"/api/ai/kb/{did}").status_code == 404


def test_ai_kb_file_and_limits(operator):
    r = operator.c.post("/api/ai/kb", data={"file": (io.BytesIO(b"hello sop"), "sop.txt")}, headers={"X-CSRF-Token": operator.csrf()},
                        content_type="multipart/form-data")
    assert r.status_code == 201
    assert operator.post("/api/ai/kb", json={"name": "", "content": "x"}).status_code == 400
    assert operator.post("/api/ai/kb", json={"name": "big", "content": "x" * 200_001}).status_code == 413


# ------------------------------------------------------------------ reports
@pytest.mark.parametrize("kind", ["infra", "hosts", "cost", "audit"])
@pytest.mark.parametrize("fmt", ["pdf", "xlsx"])
def test_reports(admin, operator, kind, fmt):
    add_host(operator, "10.0.0.1", hostname="mgmt-01", roles=["management"])
    r = admin.post("/api/reports", json={"kind": kind, "fmt": fmt})
    assert r.status_code == 201
    d = admin.get(r.get_json()["url"])
    assert d.status_code == 200
    if fmt == "pdf":
        assert d.data[:5] == b"%PDF-"
        from pypdf import PdfReader
        text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(d.data)).pages)
        assert "AutoInfra" in text
        if kind in ("infra", "hosts"):
            assert "mgmt-01" in text
    else:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(d.data))
        assert wb.sheetnames
        if kind in ("infra", "hosts"):
            vals = [c.value for row in wb["Hosts"].iter_rows() for c in row]
            assert "mgmt-01" in vals and "10.0.0.1" in vals


def test_report_validation_and_permissions(operator, viewer):
    assert operator.post("/api/reports", json={"kind": "x"}).status_code == 400
    assert operator.post("/api/reports", json={"fmt": "docx"}).status_code == 400
    assert operator.post("/api/reports", json={"kind": "audit"}).status_code == 403
    rid = viewer.post("/api/reports", json={"kind": "infra", "fmt": "xlsx"}).get_json()["id"]
    assert viewer.delete(f"/api/reports/{rid}").status_code == 403
    assert operator.delete(f"/api/reports/{rid}").status_code == 200
    assert operator.get(f"/api/reports/{rid}/download").status_code == 404
    assert operator.delete(f"/api/reports/{rid}").status_code == 404


def test_report_list(operator):
    operator.post("/api/reports", json={"kind": "infra", "fmt": "pdf"})
    r = operator.get("/api/reports").get_json()
    assert len(r["reports"]) == 1 and r["reports"][0]["name"].startswith("infra-report-") and "infra" in r["kinds"]


# ------------------------------------------------------------------ billing
def test_billing_without_cloudstack(app, operator):
    add_host(operator, "10.0.0.1")
    add_host(operator, "10.0.0.2")
    b = operator.get("/api/billing/summary").get_json()
    assert b["source"] == "none" and b["totals"]["infra_month"] == 24000.0 and b["currency"] == "INR"


@pytest.mark.parametrize("vms,expect", [
    ([{"account": "a", "state": "Running", "cpunumber": 2, "memory": 4096}], 730 * (2 * 1.5 + 4 * 0.75)),
    ([{"account": "a", "state": "Stopped", "cpunumber": 2, "memory": 4096}], 0.0),
    ([{"account": "a", "state": "Running", "cpunumber": 1, "memory": 1024}] * 3, 3 * 730 * (1.5 + 0.75)),
    ([], 0.0),
])
def test_billing_vm_cost(app, vms, expect):
    from autoinfra import billing
    with app.app_context():
        b = billing.compute(vms=vms, volumes=[], ips=[], host_count=0)
    assert abs(b["totals"]["vm_month"] - round(expect, 2)) < 0.02


def test_billing_storage_ip_accounts(app):
    from autoinfra import billing
    with app.app_context():
        b = billing.compute(vms=[{"account": "acme", "state": "Running", "cpunumber": 4, "memory": 8192}],
                            volumes=[{"account": "acme", "size": 100 * 1024 ** 3}, {"account": "beta", "size": 50 * 1024 ** 3}],
                            ips=[{"account": "acme"}, {"account": "acme"}], host_count=1)
    acc = {a["account"]: a for a in b["accounts"]}
    assert acc["acme"]["storage_gb"] == 100 and acc["acme"]["ips"] == 2 and acc["beta"]["month"] == 200.0
    assert b["totals"]["ip_month"] == 500.0 and b["totals"]["storage_month"] == 600.0
    assert b["totals"]["total_month"] == round(b["totals"]["vm_month"] + 600 + 500 + 12000, 2)
    assert b["accounts"][0]["account"] == "acme"


def test_billing_with_cloudstack(app, operator, cs_configured):
    cs_configured.vms["v"] = {"id": "v", "zoneid": "z", "account": "admin", "state": "Running", "cpunumber": 2, "memory": 2048}
    b = operator.get("/api/billing/summary").get_json()
    assert b["source"] == "cloudstack" and b["accounts"][0]["vms"] == 1


@pytest.mark.parametrize("body,ok", [({"currency": "USD"}, True), ({"currency": "dollars"}, False), ({"vcpu_hour": 2.5}, True),
                                     ({"vcpu_hour": -1}, False), ({"ram_gb_hour": "abc"}, False), ({"host_month": 50000}, True),
                                     ({"stopped_vm_storage_only": False}, True), ({"nope": 1}, False), ({"currency": "EU1"}, False)])
def test_billing_rates(admin, body, ok):
    r = admin.post("/api/billing/rates", json=body)
    assert (r.status_code == 200) == ok
    if ok:
        rates = admin.get("/api/billing/summary").get_json()["rates"]
        k, v = next(iter(body.items()))
        assert rates[k] == (v.upper() if isinstance(v, str) else v)


def test_billing_csv(admin):
    r = admin.get("/api/billing/export.csv")
    assert r.status_code == 200 and r.data.decode().startswith("account,vms,running")


# ------------------------------------------------------------------ settings / users / search / dashboard
@pytest.mark.parametrize("body,field", [({"master_ip": "x"}, "master_ip"), ({"repo_base_url": "ftp://x"}, "repo_base_url"),
                                        ({"online_mode": "yes"}, "online_mode"), ({"cs_api_url": "x"}, "cs_api_url"),
                                        ({"ollama_url": "localhost:11434"}, "ollama_url"), ({"ollama_timeout": 1}, "ollama_timeout"),
                                        ({"monitor_interval": -1}, "monitor_interval"), ({"metrics_retention_days": 0}, "metrics_retention_days"),
                                        ({"kolla_ansible_bin": "kolla"}, "kolla_ansible_bin"), ({"max_extract_gb": 0}, "max_extract_gb"),
                                        ({"unknown": 1}, "unknown"), ({"thresholds": {"min_cpu": 0}}, "thresholds.min_cpu"),
                                        ({"alert_rules": {"cpu_warn": 101}}, "alert_rules.cpu_warn"), ({"zabbix_url": "zbx"}, "zabbix_url")])
def test_settings_validation(admin, body, field):
    r = admin.post("/api/settings", json=body)
    assert r.status_code == 400 and field in r.get_json()["fields"]


def test_settings_roundtrip_and_secret_masking(admin):
    assert admin.post("/api/settings", json={"master_ip": "10.0.0.5", "cs_secret_key": "S3cr3t", "thresholds": {"min_cpu": 4}}).status_code == 200
    s = admin.get("/api/settings").get_json()["settings"]
    assert s["master_ip"] == "10.0.0.5" and s["cs_secret_key"] == "********" and s["thresholds"]["min_cpu"] == 4
    assert admin.post("/api/settings", json={"cs_secret_key": "********"}).status_code == 200       # unchanged mask keeps the value
    from autoinfra import inventory
    with admin.app.app_context():
        assert inventory.secret_get("cs_secret_key") == "S3cr3t"


def test_settings_operator_limits(operator):
    assert operator.post("/api/settings", json={"master_ip": "10.0.0.6"}).status_code == 400
    assert operator.post("/api/settings", json={"ollama_model": "mistral:7b", "thresholds": {"min_cpu": 2}}).status_code == 200


def test_threshold_setting_used_by_checks(app, admin):
    admin.post("/api/settings", json={"thresholds": {"min_cpu": 64}})
    from autoinfra import hosts
    with app.app_context():
        assert hosts.thresholds()["min_cpu"] == 64


@pytest.mark.parametrize("body,field", [({"username": "Bad", "password": "Good-Password-1", "role": "viewer"}, "username"),
                                        ({"username": "ok", "password": "short", "role": "viewer"}, "password"),
                                        ({"username": "ok", "password": "Good-Password-1", "role": "god"}, "role"),
                                        ({"username": "admin", "password": "Good-Password-1", "role": "viewer"}, "username")])
def test_user_validation(admin, body, field):
    r = admin.post("/api/users", json=body)
    assert r.status_code == 400 and field in r.get_json()["fields"]


def test_user_lifecycle(app, admin):
    from conftest import Client, PW
    uid = admin.post("/api/users", json={"username": "neo", "password": "Matrix-Reloaded-1", "role": "operator", "full_name": "Neo A"}).get_json()["id"]
    PW["neo"] = "Matrix-Reloaded-1"
    c = Client(app, "neo")
    assert c.get("/api/hosts").status_code == 200 and c.post("/api/hosts", json={"ip": "10.0.0.1"}).status_code == 201
    assert admin.patch(f"/api/users/{uid}", json={"role": "viewer"}).status_code == 200
    assert c.post("/api/hosts", json={"ip": "10.0.0.2"}).status_code == 403
    assert admin.patch(f"/api/users/{uid}", json={"password": "short"}).status_code == 400
    assert admin.patch(f"/api/users/{uid}", json={"role": "x"}).status_code == 400
    assert admin.patch("/api/users/999", json={}).status_code == 404
    me = [u for u in admin.get("/api/users").get_json()["users"] if u["username"] == "admin"][0]["id"]
    assert admin.patch(f"/api/users/{me}", json={"role": "viewer"}).status_code == 400
    assert admin.patch(f"/api/users/{me}", json={"disabled": True}).status_code == 400
    del PW["neo"]


def test_change_own_password(app, operator):
    assert operator.post("/api/me/password", json={"current": "wrong", "new": "Another-Pass-2027"}).status_code == 400
    assert operator.post("/api/me/password", json={"current": "Operator-Pass-2026!", "new": "weak"}).status_code == 400
    assert operator.post("/api/me/password", json={"current": "Operator-Pass-2026!", "new": "Another-Pass-2027"}).status_code == 200
    from conftest import Client
    c = Client(app)
    assert c.c.post("/login", data={"username": "operator", "password": "Another-Pass-2027", "csrf_token": c.csrf()}).status_code == 302


def test_search(app, operator):
    add_host(operator, "10.0.0.77", hostname="searchme")
    r = operator.get("/api/search?q=searchme").get_json()["results"]
    assert r[0]["type"] == "host" and "10.0.0.77" in r[0]["label"]
    assert any(x["type"] == "component" for x in operator.get("/api/search?q=mysql").get_json()["results"])
    assert operator.get("/api/search?q=a").get_json()["results"] == []


def test_dashboard(app, operator):
    hid = add_host(operator, "10.0.0.1")
    mark_ready(app, hid)
    d = operator.get("/api/dashboard").get_json()
    assert d["hosts"]["total"] == 1 and d["hosts"]["ready"] == 1 and d["components"]["total"] == 16 and d["packages"]["items"] == 0


def test_audit_search_and_csv(admin):
    add_host(admin, "10.0.0.99")
    rows = admin.get("/api/audit?q=10.0.0.99").get_json()["audit"]
    assert rows and rows[0]["action"] == "host.add"
    csv = admin.get("/api/audit?format=csv").data.decode()
    assert csv.startswith("time,user,ip,action") and "host.add" in csv


def test_metrics_retention(app):
    from autoinfra import db, monitoring
    with app.app_context():
        db.ex("INSERT INTO metrics(ts,host_id,cpu) VALUES(?,?,?)", (time.time() - 30 * 86400, 1, 1))
        monitoring.collect_all(lambda *_: None)
        assert db.one("SELECT COUNT(*) n FROM metrics")["n"] == 0
