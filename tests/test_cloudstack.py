"""CloudStack wizard (validation, plan, inventory/vars, full deploy job) and live infrastructure API against the simulator."""
import json

from autoinfra import db

import pytest

from conftest import add_host, ansible_calls, mark_ready, wait_job


@pytest.fixture
def lab(app, operator):
    """mgmt x2, db x3, kvm x3, storage x1 - all 'ready' Ubuntu hosts."""
    ids = {}
    spec = [("mgmt1", "10.0.0.10", ["management"]), ("mgmt2", "10.0.0.11", ["management"]), ("db1", "10.0.0.41", ["database"]),
            ("db2", "10.0.0.42", ["database"]), ("db3", "10.0.0.43", ["database"]), ("kvm1", "10.0.1.21", ["compute"]),
            ("kvm2", "10.0.1.22", ["compute"]), ("kvm3", "10.0.1.23", ["compute"]), ("nfs1", "10.0.0.31", ["storage"])]
    for name, ip, roles in spec:
        ids[name] = add_host(operator, ip, hostname=name, roles=roles)
        mark_ready(app, ids[name])
    return ids


def fill(operator, lab, **over):
    steps = {
        "requirements": {"mode": "offline", "version": "4.22.0", "os": "ubuntu-24.04"},
        "management": {"host_ids": [lab["mgmt1"], lab["mgmt2"]], "lb_host_ids": [lab["db1"], lab["db2"]], "hostname": "cloudstack-mgmt", "vip": "10.0.0.20", "network": "10.0.0.0/24",
                       "netmask": "255.255.255.0", "gateway": "10.0.0.1", "timezone": "Asia/Kolkata", "ssl": True, "usage": True},
        "database": {"strategy": "gtid_agent", "host_ids": [lab["db1"], lab["db2"], lab["db3"]], "vip": "10.0.0.40", "port": 3306},
        "mq": {"enabled": False, "host_ids": []},
        "systemvm": {"template": "", "hypervisor": "KVM"},
        "zone": {"name": "Zone-1", "dns1": "8.8.8.8", "internal_dns1": "10.0.0.5", "vlan_range": "100-200", "guest_cidr": "10.1.1.0/24",
                 "pod_name": "Pod-1", "pod_gateway": "10.0.0.1", "pod_netmask": "255.255.255.0", "pod_start": "10.0.0.100", "pod_end": "10.0.0.130",
                 "public_gateway": "10.0.2.1", "public_netmask": "255.255.255.0", "public_start": "10.0.2.100", "public_end": "10.0.2.150"},
        "cluster": {"clusters": [{"name": "Cluster-1", "host_ids": [lab["kvm1"], lab["kvm2"]]}, {"name": "Cluster-2", "host_ids": [lab["kvm3"]]}],
                    "primary_server": "10.0.0.31", "primary_path": "/export/primary", "secondary_server": "10.0.0.31", "secondary_path": "/export/secondary"},
    }
    for k, v in over.items():
        steps[k].update(v)
    for st, body in steps.items():
        r = operator.post(f"/api/cs/wizard/{st}", json=body)
        assert r.status_code == 200, (st, r.get_json())
    return steps


def test_wizard_defaults(operator):
    w = operator.get("/api/cs/wizard").get_json()
    assert w["steps"][0] == "requirements" and len(w["steps"]) == 8 and w["state"]["zone"]["name"] == "Zone-1"
    assert w["required"] and w["plan"][0]["playbook"] == "00-preflight.yml"


def test_full_wizard_saves(operator, lab):
    fill(operator, lab)
    s = operator.get("/api/cs/wizard").get_json()["state"]
    assert s["database"]["vip"] == "10.0.0.40" and len(s["cluster"]["clusters"]) == 2


BAD = [
    ("requirements", {"mode": "cloud"}, "mode"), ("requirements", {"version": "x"}, "version"), ("requirements", {"os": "windows"}, "os"),
    ("requirements", {"deploy_type": "half"}, "deploy_type"),
    ("management", {"host_ids": []}, "host_ids"), ("management", {"host_ids": [999]}, "host_ids"), ("management", {"host_ids": "1"}, "host_ids"),
    ("management", {"hostname": "bad host"}, "hostname"), ("management", {"netmask": "255.0.255.0"}, "netmask"),
    ("management", {"timezone": "kolkata"}, "timezone"), ("management", {"network": "10.0.0.1/24"}, "network"),
    ("management", {"gateway": "10.9.9.1"}, "gateway"), ("management", {"gateway": "x"}, "gateway"),
    ("management", {"vip": ""}, "vip"), ("management", {"vip": "10.0.0.10"}, "vip"), ("management", {"vip": "bad"}, "vip"),
    ("management", {"ntp": "bad host!"}, "ntp"),
    ("database", {"strategy": "galera2"}, "strategy"), ("database", {"strategy": "pxc", "host_ids": [3, 4]}, "host_ids"),
    ("database", {"strategy": "single"}, "host_ids"), ("database", {"vip": ""}, "vip"), ("database", {"vip": "10.0.0.41"}, "vip"),
    ("database", {"vip": "10.0.0.20"}, "vip"), ("database", {"port": 0}, "port"), ("database", {"host_ids": [12345]}, "host_ids"),
    ("mq", {"enabled": True, "host_ids": []}, "host_ids"),
    ("systemvm", {"hypervisor": "XenServer"}, "hypervisor"), ("systemvm", {"template": "cloudstack/nope.qcow2.bz2"}, "template"),
    ("zone", {"name": ""}, "name"), ("zone", {"dns1": "dns"}, "dns1"), ("zone", {"dns2": "x"}, "dns2"), ("zone", {"internal_dns1": "y"}, "internal_dns1"),
    ("zone", {"vlan_range": "300-100"}, "vlan_range"), ("zone", {"guest_cidr": "10.1.1.1/24"}, "guest_cidr"),
    ("zone", {"pod_name": "<p>"}, "pod_name"), ("zone", {"pod_gateway": "x"}, "pod_gateway"), ("zone", {"pod_netmask": "1"}, "pod_netmask"),
    ("zone", {"pod_start": "10.0.0.130", "pod_end": "10.0.0.100"}, "pod_range"), ("zone", {"pod_start": "10.0.5.100"}, "pod_range"),
    ("zone", {"pod_start": "10.0.0.1"}, "pod_range"), ("zone", {"public_gateway": "?"}, "public_gateway"),
    ("zone", {"public_start": "10.0.2.200"}, "public_range"), ("zone", {"public_vlan": "5000"}, "public_vlan"),
    ("zone", {"public_gateway": "10.0.0.1", "public_start": "10.0.0.110", "public_end": "10.0.0.120"}, "public_range"),
    ("zone", {"label_mgmt": "bad label"}, "label_mgmt"), ("zone", {"label_guest": ""}, "label_guest"), ("zone", {"label_public": "x" * 20}, "label_public"),
    ("cluster", {"clusters": []}, "clusters"), ("cluster", {"clusters": [{"name": "", "host_ids": [1]}]}, "clusters.0.name"),
    ("cluster", {"primary_server": ""}, "primary_server"), ("cluster", {"primary_path": "export"}, "primary_path"),
    ("cluster", {"secondary_server": "bad host"}, "secondary_server"), ("cluster", {"secondary_path": "/a/../b"}, "secondary_path"),
    ("cluster", {"kvm_auth": "token"}, "kvm_auth"),
]


@pytest.mark.parametrize("step,body,field", BAD)
def test_wizard_step_validation(operator, lab, step, body, field):
    fill(operator, lab)
    r = operator.post(f"/api/cs/wizard/{step}", json=body)
    assert r.status_code == 400, r.get_json()
    assert field in r.get_json()["fields"], r.get_json()


def test_cluster_duplicate_and_shared_host(operator, lab):
    fill(operator, lab)
    r = operator.post("/api/cs/wizard/cluster", json={"clusters": [{"name": "A", "host_ids": [lab["kvm1"]]}, {"name": "A", "host_ids": [lab["kvm2"]]}]})
    assert "clusters.1.name" in r.get_json()["fields"]
    r = operator.post("/api/cs/wizard/cluster", json={"clusters": [{"name": "A", "host_ids": [lab["kvm1"]]}, {"name": "B", "host_ids": [lab["kvm1"]]}]})
    assert "one cluster only" in r.get_json()["fields"]["clusters"]
    r = operator.post("/api/cs/wizard/cluster", json={"clusters": [{"name": "A", "host_ids": []}]})
    assert "clusters.0.host_ids" in r.get_json()["fields"]


@pytest.mark.parametrize("strategy,n,ok", [("gtid_agent", 2, True), ("gtid_agent", 3, True), ("gtid_agent", 1, False), ("pxc", 3, True),
                                            ("pxc", 2, False), ("single", 1, True), ("single", 2, False)])
def test_db_node_counts(operator, lab, strategy, n, ok):
    fill(operator, lab)
    ids = [lab["db1"], lab["db2"], lab["db3"]][:n]
    r = operator.post("/api/cs/wizard/database", json={"strategy": strategy, "host_ids": ids, "vip": "10.0.0.40" if n > 1 else ""})
    assert (r.status_code == 200) == ok, r.get_json()


@pytest.mark.parametrize("n,ok", [(1, True), (3, True), (2, False)])
def test_mq_node_counts(operator, lab, n, ok):
    ids = [lab["db1"], lab["db2"], lab["db3"]][:n]
    assert (operator.post("/api/cs/wizard/mq", json={"enabled": True, "host_ids": ids}).status_code == 200) == ok


def test_single_mgmt_needs_no_vip(operator, lab):
    assert operator.post("/api/cs/wizard/management", json={"host_ids": [lab["mgmt1"]], "vip": "", "hostname": "m"}).status_code == 200


def test_unknown_step(operator):
    assert operator.post("/api/cs/wizard/review", json={}).status_code == 404
    assert operator.post("/api/cs/wizard/bogus", json={}).status_code == 404


def test_unknown_fields_ignored(operator, lab):
    fill(operator, lab)
    operator.post("/api/cs/wizard/zone", json={"evil": "x"})
    assert "evil" not in operator.get("/api/cs/wizard").get_json()["state"]["zone"]


@pytest.mark.parametrize("over,present,absent", [
    ({}, ["10-cloudstack-db.yml", "21-cloudstack-lb.yml", "31-nginx.yml", "24-cloudstack-usage.yml", "41-zabbix-agent.yml"], ["30-rabbitmq.yml", "11-cloudstack-db-pxc.yml"]),
    ({"database": {"strategy": "pxc"}}, ["11-cloudstack-db-pxc.yml"], ["10-cloudstack-db.yml"]),
    ({"mq": {"enabled": True}}, ["30-rabbitmq.yml"], []),
    ({"management": {"ssl": False, "usage": False}}, [], ["31-nginx.yml", "24-cloudstack-usage.yml"]),
    ({"requirements": {"monitoring": False}}, [], ["41-zabbix-agent.yml"]),
    ({"requirements": {"database": False}}, [], ["10-cloudstack-db.yml"]),
])
def test_plan(operator, lab, over, present, absent):
    s = fill(operator, lab)
    for k, v in over.items():
        if k == "mq":
            v = dict(v, host_ids=[lab["db1"]])
        if k == "database" and v.get("strategy") == "pxc":
            v = dict(v)
        r = operator.post(f"/api/cs/wizard/{k}", json=v)
        assert r.status_code == 200, r.get_json()
    plan = [p["playbook"] for p in operator.get("/api/cs/wizard").get_json()["plan"]]
    assert plan[:2] == ["00-preflight.yml", "01-common.yml"] and plan[-1] in ("23-cloudstack-kvm.yml", "25-systemvm-template.yml", "41-zabbix-agent.yml")
    for p in present:
        assert p in plan
    for p in absent:
        assert p not in plan
    assert plan.index("20-cloudstack-mgmt.yml") > plan.index("01-common.yml")
    assert plan.index("23-cloudstack-kvm.yml") > plan.index("22-nfs.yml")


def test_deploy_requires_ready_hosts(app, operator, lab):
    fill(operator, lab)
    from autoinfra import db
    with app.app_context():
        db.ex("UPDATE hosts SET status='unreachable' WHERE id=?", (lab["kvm3"],))
    r = operator.post("/api/cs/wizard/deploy", json={})
    assert r.status_code == 400 and "10.0.1.23" in json.dumps(r.get_json())


def test_deploy_requires_same_os_family(app, operator, lab):
    fill(operator, lab)
    mark_ready(app, lab["kvm3"], "rhel", "almalinux", "9.3")
    r = operator.post("/api/cs/wizard/deploy", json={})
    assert r.status_code == 400 and "different OS family" in json.dumps(r.get_json())


def test_deploy_requires_packages(operator, lab):
    fill(operator, lab)
    r = operator.post("/api/cs/wizard/deploy", json={})
    assert r.status_code == 409 and "missing from the master repository" in r.get_json()["error"]


def test_full_deploy_job(app, operator, lab, cs_mock):
    fill(operator, lab)
    from autoinfra import db
    with app.app_context():
        db.set_setting("cs_api_url", cs_mock.url)
        db.set_setting("cs_poll_interval", 0.01)
    r = operator.post("/api/cs/wizard/deploy", json={"force": True, "admin_password": "password"})
    assert r.status_code == 202
    j = wait_job(operator, r.get_json()["job_id"], 120)
    log = operator.get(f"/api/jobs/{j['id']}/log.txt").data.decode()
    assert j["state"] == "success", log
    calls = ansible_calls(app)
    assert [c["playbook"] for c in calls] == [p for p, _ in __import__("autoinfra.cloudstack", fromlist=["x"]).plan(_state(app))] + ["27-cloudstack-mgmt-restart.yml"]
    inv, v = calls[0]["inventory"], calls[0]["vars"]
    assert "[cs_mgmt]\nmgmt1\nmgmt2" in inv and "[cs_db]\ndb1\ndb2\ndb3" in inv and "[cs_kvm]\nkvm1\nkvm2\nkvm3" in inv
    assert "[cs_lb]\ndb1\ndb2" in inv and "[cs_nfs]\nnfs1" in inv
    assert v["db_vip"] == "10.0.0.40" and v["cs_mgmt_vip"] == "10.0.0.20" and v["db_client_ips"] == ["10.0.0.10", "10.0.0.11"]
    assert len(v["db_acs_admin_password"]) >= 16 and v["cs_series"] == "4.22" and v["nginx_hosts"] == "cs_mgmt"
    # zone objects created in the simulator, through the management VIP setting
    assert [z["name"] for z in cs_mock.zones.values()] == ["Zone-1"] and len(cs_mock.clusters) == 2 and len(cs_mock.hosts) == 3
    assert cs_mock.config["host"] == "10.0.0.20" and list(cs_mock.zones.values())[0]["allocationstate"] == "Enabled"
    assert cs_mock.host_passwords == [None, None, None]          # key-based addHost
    with app.app_context():
        assert db.get_setting("cs_api_key") == cs_mock.api_key   # keys registered for the infra pages


def _state(app):
    from autoinfra import cloudstack
    with app.app_context():
        return cloudstack.state()


def test_deploy_kvm_password_mode(app, operator, lab, cs_mock):
    fill(operator, lab, cluster={"kvm_auth": "password"})
    from autoinfra import db
    with app.app_context():
        db.set_setting("cs_api_url", cs_mock.url)
        db.set_setting("cs_poll_interval", 0.01)
    r = operator.post("/api/cs/wizard/deploy", json={"force": True, "admin_password": "password", "kvm_password": "Kvm-Root-Pw-99"})
    j = wait_job(operator, r.get_json()["job_id"], 120)
    assert j["state"] == "success"
    assert cs_mock.host_passwords == ["Kvm-Root-Pw-99"] * 3
    assert "Kvm-Root-Pw-99" not in operator.get(f"/api/jobs/{j['id']}/log.txt").data.decode()
    with app.app_context():
        assert "Kvm-Root-Pw-99" not in "\n".join(map(str, db.conn().iterdump()))


def test_failed_playbook_stops_deploy(app, operator, lab, cs_mock, monkeypatch):
    fill(operator, lab)
    monkeypatch.setenv("FAKE_RC_20_cloudstack_mgmt_yml", "2")
    r = operator.post("/api/cs/wizard/deploy", json={"force": True})
    j = wait_job(operator, r.get_json()["job_id"], 120)
    assert j["state"] == "failed"
    states = [s["state"] for s in j["steps"]]
    assert "failed" in states and states[-1] == "skipped" and len(cs_mock.zones) == 0


def test_zone_bootstrap_wrong_admin_password(app, operator, lab, cs_mock):
    fill(operator, lab)
    from autoinfra import db
    with app.app_context():
        db.set_setting("cs_api_url", cs_mock.url)
    r = operator.post("/api/cs/wizard/deploy", json={"force": True, "admin_password": "wrong"})
    j = wait_job(operator, r.get_json()["job_id"], 120)
    assert j["state"] == "failed" and "login failed" in operator.get(f"/api/jobs/{j['id']}/log.txt").data.decode()


def test_zone_bootstrap_idempotent(app, cs_mock):
    from autoinfra import csapi
    spec = {"zone": {"name": "Z", "dns1": "8.8.8.8", "vlan_range": "100-200", "guest_cidr": "10.1.1.0/24"},
            "pod": {"name": "P", "start": "10.0.0.100", "end": "10.0.0.130", "netmask": "255.255.255.0", "gateway": "10.0.0.1"},
            "public": {"start": "10.0.2.100", "end": "10.0.2.150", "netmask": "255.255.255.0", "gateway": "10.0.2.1"},
            "clusters": [{"name": "C1", "hosts": [{"ip": "10.0.1.21"}], "primary_nfs": "10.0.0.31/export/c1"}],
            "secondary_nfs": "10.0.0.31/export/sec", "management_vip": "10.0.0.20"}
    cs = csapi.CS(cs_mock.url, poll_interval=0.01)
    cs.login("admin", "password")
    logs = []
    csapi.bootstrap_zone(cs, spec, logs.append)
    n = len(cs_mock.calls)
    csapi.bootstrap_zone(cs, spec, logs.append)
    creates = [c for c in cs_mock.calls[n:] if c.startswith(("create", "add"))]
    assert creates == [] and len(cs_mock.pools) == 1 and len(cs_mock.stores) == 1
    assert any("exists" in l for l in logs)


def test_client_signature_rejected_with_wrong_secret(cs_mock):
    from autoinfra import csapi
    cs = csapi.CS(cs_mock.url, cs_mock.api_key, "wrong-secret")
    with pytest.raises(csapi.CSError):
        cs.run("listZones")
    assert cs_mock.bad_signatures == 1


def test_client_unreachable():
    from autoinfra import csapi
    with pytest.raises(csapi.CSError, match="cannot reach"):
        csapi.CS("http://127.0.0.1:1", "k", "s", timeout=2).run("listZones")


# ------------------------------------------------------------------ infrastructure API
def test_not_configured(operator):
    assert operator.get("/api/cs/summary").get_json()["configured"] is False
    r = operator.get("/api/cs/zones")
    assert r.status_code == 502 and "not configured" in r.get_json()["error"]


def test_unknown_resource(operator):
    assert operator.get("/api/cs/bananas").status_code == 404
    assert operator.post("/api/cs/bananas", json={}).status_code == 404


@pytest.fixture
def zone(operator, cs_configured):
    z = operator.post("/api/cs/zones", json={"name": "Z1", "dns1": "8.8.8.8", "internaldns1": "10.0.0.5"}).get_json()["result"]["zone"]
    p = operator.post("/api/cs/pods", json={"zoneid": z["id"], "name": "P1", "gateway": "10.0.0.1", "netmask": "255.255.255.0",
                                           "startip": "10.0.0.100", "endip": "10.0.0.120"}).get_json()["result"]["pod"]
    c = operator.post("/api/cs/clusters", json={"zoneid": z["id"], "podid": p["id"], "clustername": "C1"}).get_json()["result"]["cluster"][0]
    return {"zone": z, "pod": p, "cluster": c, "mock": cs_configured}


@pytest.mark.parametrize("res", ["zones", "pods", "clusters", "hosts", "storagepools", "imagestores", "networks", "physicalnetworks",
                                 "vlanipranges", "systemvms", "templates", "serviceofferings", "diskofferings", "virtualmachines",
                                 "volumes", "capacity", "alerts", "routers"])
def test_list_every_resource(operator, cs_configured, res):
    r = operator.get(f"/api/cs/{res}")
    assert r.status_code == 200 and isinstance(r.get_json()["items"], list)


def test_create_chain_and_summary(operator, zone):
    z, p, c = zone["zone"], zone["pod"], zone["cluster"]
    assert operator.post("/api/cs/hosts", json={"zoneid": z["id"], "podid": p["id"], "clusterid": c["id"], "url": "http://10.0.1.21"}).status_code == 201
    # like real CloudStack, a pool needs an Up host: before the agent connected the API answers with an error
    r = operator.post("/api/cs/storagepools", json={"zoneid": z["id"], "podid": p["id"], "clusterid": c["id"], "name": "pri", "url": "nfs://10.0.0.31/export/p"})
    assert r.status_code == 502 and "No host up" in r.get_json()["error"]
    __import__("time").sleep(0.6)
    assert operator.post("/api/cs/storagepools", json={"zoneid": z["id"], "podid": p["id"], "clusterid": c["id"], "name": "pri", "url": "nfs://10.0.0.31/export/p"}).status_code == 201
    assert operator.post("/api/cs/imagestores", json={"zoneid": z["id"], "name": "sec", "url": "nfs://10.0.0.31/export/s"}).status_code == 201
    assert operator.post("/api/cs/serviceofferings", json={"name": "small", "displaytext": "1x1", "cpunumber": 1, "cpuspeed": 1000, "memory": 1024}).status_code == 201
    s = operator.get("/api/cs/summary").get_json()
    assert (s["configured"], s["zones"], s["pods"], s["clusters"], s["hosts"], s["storagepools"], s["imagestores"]) == (True, 1, 1, 1, 1, 1, 1)
    t = operator.get("/api/cs/topology").get_json()
    assert t["zones"][0]["pods"][0]["clusters"][0]["hosts"][0]["name"] == "10.0.1.21"
    assert operator.get(f"/api/cs/pods?zoneid={z['id']}").get_json()["items"][0]["name"] == "P1"


CREATE_BAD = [
    ("zones", {"name": "", "dns1": "8.8.8.8", "internaldns1": "1.1.1.1"}, "name"), ("zones", {"name": "Z", "dns1": "x", "internaldns1": "1.1.1.1"}, "dns1"),
    ("zones", {"name": "Z", "dns1": "8.8.8.8"}, "internaldns1"),
    ("pods", {"zoneid": "z", "name": "P", "gateway": "10.0.0.1", "netmask": "255.255.255.0", "startip": "10.0.0.130", "endip": "10.0.0.100"}, "startip"),
    ("pods", {"zoneid": "z", "name": "P", "gateway": "x", "netmask": "255.255.255.0", "startip": "10.0.0.1", "endip": "10.0.0.2"}, "gateway"),
    ("pods", {"name": "P"}, "zoneid"), ("clusters", {"zoneid": "z", "podid": "p"}, "clustername"),
    ("hosts", {"zoneid": "z", "podid": "p", "clusterid": "c", "url": "10.0.1.21"}, "url"),
    ("storagepools", {"zoneid": "z", "podid": "p", "clusterid": "c", "name": "x", "url": "10.0.0.1:/x"}, "url"),
    ("imagestores", {"zoneid": "z", "name": "s", "url": "smb://x/y"}, "url"),
    ("vlanipranges", {"zoneid": "z", "gateway": "10.0.2.1", "netmask": "255.255.255.0", "startip": "10.0.3.1", "endip": "10.0.3.5"}, "startip"),
    ("serviceofferings", {"name": "x", "displaytext": "y", "cpunumber": 0, "cpuspeed": 1000, "memory": 512}, "cpunumber"),
    ("serviceofferings", {"name": "x", "displaytext": "y", "cpunumber": 1, "cpuspeed": 50, "memory": 512}, "cpuspeed"),
    ("serviceofferings", {"name": "x", "displaytext": "y", "cpunumber": 1, "cpuspeed": 1000, "memory": 64}, "memory"),
]


@pytest.mark.parametrize("res,body,field", CREATE_BAD)
def test_create_validation(operator, cs_configured, res, body, field):
    r = operator.post(f"/api/cs/{res}", json=body)
    assert r.status_code == 400 and field in r.get_json()["fields"], r.get_json()


def test_create_api_error_is_502(operator, cs_configured):
    r = operator.post("/api/cs/clusters", json={"zoneid": "nope", "podid": "nope", "clustername": "C"})
    assert r.status_code == 502 and "pod does not belong" in r.get_json()["error"]


@pytest.mark.parametrize("res,action,field,value", [
    ("zones", "disable", "allocationstate", "Disabled"), ("zones", "enable", "allocationstate", "Enabled"),
    ("pods", "disable", "allocationstate", "Disabled"), ("clusters", "disable", "allocationstate", "Disabled"),
    ("clusters", "enable", "allocationstate", "Enabled"),
])
def test_state_actions(operator, zone, res, action, field, value):
    store = {"zones": "zones", "pods": "pods", "clusters": "clusters"}[res]
    obj = {"zones": zone["zone"], "pods": zone["pod"], "clusters": zone["cluster"]}[res]
    r = operator.post(f"/api/cs/{res}/{obj['id']}/{action}")
    assert r.status_code == 200, r.get_json()
    assert getattr(zone["mock"], store)[obj["id"]][field] == value


@pytest.mark.parametrize("action,expect", [("maintenance", "Maintenance"), ("cancel-maintenance", "Enabled")])
def test_host_maintenance(operator, zone, action, expect):
    z, p, c = zone["zone"], zone["pod"], zone["cluster"]
    h = operator.post("/api/cs/hosts", json={"zoneid": z["id"], "podid": p["id"], "clusterid": c["id"], "url": "http://10.0.1.21"}).get_json()["result"]["host"][0]
    assert operator.post(f"/api/cs/hosts/{h['id']}/{action}").status_code == 200
    assert zone["mock"].hosts[h["id"]]["resourcestate"] == expect


@pytest.mark.parametrize("action,state", [("start", "Running"), ("stop", "Stopped"), ("reboot", "Running")])
def test_systemvm_actions(operator, cs_configured, action, state):
    cs_configured.systemvms["s1"] = {"id": "s1", "name": "s-1-VM", "state": "Running", "zoneid": "z"}
    assert operator.post(f"/api/cs/systemvms/s1/{action}").status_code == 200
    assert cs_configured.systemvms["s1"]["state"] == state


def test_delete_needs_admin(operator, admin, zone):
    zid = zone["zone"]["id"]
    assert operator.post(f"/api/cs/zones/{zid}/delete").status_code == 403
    assert admin.post(f"/api/cs/zones/{zid}/delete").status_code == 200
    assert zid not in zone["mock"].zones


@pytest.mark.parametrize("res,action", [("zones", "explode"), ("imagestores", "start"), ("templates", "enable"), ("nope", "delete")])
def test_unknown_actions(operator, cs_configured, res, action):
    assert operator.post(f"/api/cs/{res}/x/{action}").status_code == 404


def test_action_on_missing_entity(operator, cs_configured):
    r = operator.post("/api/cs/hosts/doesnotexist/maintenance")
    assert r.status_code == 502


def test_every_action_maps_to_a_mock_command(cs_configured):
    """Each UI action calls a CloudStack command the simulator knows (catches typos in command names)."""
    import mock_cloudstack_api as mock
    from autoinfra import csapi
    known = set(mock.ACTIONS_MOCK) | {k[2:] for k in dir(type("x", (), {})) if k.startswith("c_")} | {"updatezone"}
    for (res, act), (cmd, _, _) in csapi.ACTIONS.items():
        assert cmd.lower() in known, (res, act, cmd)


def test_systemvm_seed_only_when_template_chosen(app, operator, lab, tmp_path):
    fill(operator, lab)
    plan = [p["playbook"] for p in operator.get("/api/cs/wizard").get_json()["plan"]]
    assert "25-systemvm-template.yml" not in plan
    from autoinfra import db
    with app.app_context():
        db.ex("INSERT INTO repo_items(repo,kind,name,version,arch,path,size,sha256,created) VALUES('cloudstack','file','t','','',"
              "'systemvm/systemvmtemplate-4.22.0-x86_64-kvm.qcow2.bz2',1,'',0)")
    assert operator.post("/api/cs/wizard/systemvm", json={"template": "systemvm/systemvmtemplate-4.22.0-x86_64-kvm.qcow2.bz2"}).status_code == 200
    plan = [p["playbook"] for p in operator.get("/api/cs/wizard").get_json()["plan"]]
    assert "25-systemvm-template.yml" in plan


# ---------------------------------------------------------------- production audit fixes
@pytest.mark.parametrize("lb,msg", [([], "1-2 load balancer"), (["mgmt1"], "must not be management"), (["db1", "db2", "db3"], "1-2 load balancer"),
                                     ([99999], "unknown")])
def test_vip_needs_separate_lb_hosts(operator, lab, lb, msg):
    ids = [lab.get(x, x) for x in lb]
    r = operator.post("/api/cs/wizard/management", json={"host_ids": [lab["mgmt1"], lab["mgmt2"]], "lb_host_ids": ids, "vip": "10.0.0.20", "hostname": "m"})
    assert r.status_code == 400 and "lb_host_ids" in r.get_json()["fields"], r.get_json()


def test_single_mgmt_with_vip_also_needs_lb(operator, lab):
    r = operator.post("/api/cs/wizard/management", json={"host_ids": [lab["mgmt1"]], "vip": "10.0.0.20", "hostname": "m"})
    assert r.status_code == 400 and "lb_host_ids" in r.get_json()["fields"]
    r = operator.post("/api/cs/wizard/management", json={"host_ids": [lab["mgmt1"]], "lb_host_ids": [lab["db1"]], "vip": "10.0.0.20", "hostname": "m"})
    assert r.status_code == 200


def test_deploy_only_touches_wizard_hosts_and_sets_vip_before_hosts(app, operator, lab, cs_mock):
    from conftest import add_host
    add_host(operator, "10.0.9.99", hostname="unrelated", roles=["app", "management"])
    fill(operator, lab)
    with app.app_context():
        db.set_setting("cs_api_url", cs_mock.url)
        db.set_setting("cs_poll_interval", 0.01)
    r = operator.post("/api/cs/wizard/deploy", json={"force": True, "admin_password": "password"})
    assert r.status_code == 202
    j = wait_job(operator, r.get_json()["job_id"], 120)
    log = operator.get(f"/api/jobs/{j['id']}/log.txt").data.decode()
    assert j["state"] == "success", log
    calls = ansible_calls(app)
    assert "unrelated" not in calls[0]["inventory"] and "10.0.9.99" not in calls[0]["inventory"]
    assert "[cs_mgmt]\nmgmt1\nmgmt2\n" in calls[0]["inventory"]                      # host roles do not leak into wizard groups
    assert calls[-1]["playbook"] == "27-cloudstack-mgmt-restart.yml"
    assert calls[0]["vars"]["cs_key_dir"].endswith("cs-keys") and not calls[0]["vars"]["cs_key_dir"].startswith("/var/lib/airgap-ui")
    assert log.index("global setting host = 10.0.0.20 (management servers are restarted next)") < log.index("27-cloudstack-mgmt-restart.yml") < log.index("[zone] created zone")


def test_no_restart_step_without_vip(app, operator, lab, cs_mock):
    fill(operator, lab, management={"host_ids": [lab["mgmt1"]], "lb_host_ids": [], "vip": ""},
         database={"strategy": "single", "host_ids": [lab["db1"]], "vip": ""})
    with app.app_context():
        db.set_setting("cs_api_url", cs_mock.url)
        db.set_setting("cs_poll_interval", 0.01)
    r = operator.post("/api/cs/wizard/deploy", json={"force": True, "admin_password": "password"})
    j = wait_job(operator, r.get_json()["job_id"], 120)
    assert j["state"] == "success"
    pbs = [c["playbook"] for c in ansible_calls(app)]
    assert "27-cloudstack-mgmt-restart.yml" not in pbs and "21-cloudstack-lb.yml" not in pbs


def test_api_login_waits_for_a_starting_management_server(cs_mock):
    """A fresh management server refuses connections for minutes: login must retry, a wrong password must not."""
    import socket, threading, time as _t
    from autoinfra import csapi
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    cs = csapi.CS(f"http://127.0.0.1:{port}")
    t0 = _t.time()
    with pytest.raises(csapi.CSError):
        cs.login_retry("admin", "password", wait=0.5)
    assert _t.time() - t0 >= 0.4
    cs = csapi.CS(cs_mock.url)
    t0 = _t.time()
    with pytest.raises(csapi.CSError):
        cs.login_retry("admin", "wrong", wait=30)
    assert _t.time() - t0 < 5
    assert cs.login_retry("admin", "password", wait=5)[0]
    assert cs.wait_ready(5)


@pytest.mark.parametrize("auto", [True, False])
def test_wizard_passes_automatic_bridge_setting_to_the_kvm_role(app, operator, lab, cs_mock, auto):
    fill(operator, lab, cluster={"auto_bridge": auto}, zone={"label_guest": "cloudbr1"})
    with app.app_context():
        db.set_setting("cs_api_url", cs_mock.url)
        db.set_setting("cs_poll_interval", 0.01)
    r = operator.post("/api/cs/wizard/deploy", json={"force": True, "admin_password": "password"})
    wait_job(operator, r.get_json()["job_id"], 120)
    v = ansible_calls(app)[0]["vars"]
    assert v["kvm_manage_network"] is auto and v["kvm_bridge_name"] == "cloudbr0" and v["kvm_required_bridges"] == ["cloudbr0", "cloudbr1"]


def test_automatic_bridge_is_the_wizard_default(operator):
    st = operator.get("/api/cs/wizard").get_json()["state"]
    assert st["cluster"]["auto_bridge"] is True and st["zone"]["label_guest"] == "cloudbr0"


# ---------------------------------------------------------------- internal pod/public network (LAN without spare IPs)
INTERNAL = {"internal_net": True, "label_mgmt": "cloudbr1", "label_guest": "cloudbr1", "label_public": "cloudbr1",
            "pod_gateway": "10.200.0.1", "pod_netmask": "255.255.255.0", "pod_start": "10.200.0.10", "pod_end": "10.200.0.29",
            "public_gateway": "10.201.0.1", "public_netmask": "255.255.255.0", "public_start": "10.201.0.10", "public_end": "10.201.0.29"}
SINGLE = {"management": {"host_ids": [], "vip": "", "lb_host_ids": []}, "database": {"strategy": "single", "host_ids": [], "vip": ""}}


def _single(lab, kvm=("kvm1",)):
    return {"management": {"host_ids": [lab["mgmt1"]], "vip": "", "lb_host_ids": []},
            "database": {"strategy": "single", "host_ids": [lab["mgmt1"]], "vip": ""},
            "zone": dict(INTERNAL), "cluster": {"clusters": [{"name": "Cluster-1", "host_ids": [lab[k] for k in kvm]}]}}


@pytest.mark.parametrize("over,field", [
    ({"label_guest": "cloudbr0"}, "label_mgmt"),                                                  # labels must name one bridge
    ({"label_mgmt": "cloudbr0", "label_guest": "cloudbr0", "label_public": "cloudbr0"}, "label_mgmt"),   # not the host bridge
    ({"public_gateway": "10.200.0.1", "public_start": "10.200.0.100", "public_end": "10.200.0.120"}, "public_gateway"),  # same subnet
    ({"pod_gateway": "10.0.1.1", "pod_start": "10.0.1.100", "pod_end": "10.0.1.120"}, "pod_gateway"),  # contains a host IP (kvm1)
])
def test_internal_net_zone_validation(operator, lab, over, field):
    r = operator.post("/api/cs/wizard/zone", json=dict(INTERNAL, **over))
    assert r.status_code == 400 and field in r.get_json()["fields"], r.get_json()


def test_internal_net_needs_exactly_one_kvm_host(operator, lab):
    o = _single(lab)
    fill(operator, lab, **o)
    r = operator.post("/api/cs/wizard/cluster", json={"clusters": [{"name": "Cluster-1", "host_ids": [lab["kvm1"], lab["kvm2"]]}]})
    assert r.status_code == 400 and "exactly one KVM host" in r.get_json()["fields"]["clusters"]


def test_internal_net_vars_and_zone_spec(app, operator, lab):
    fill(operator, lab, **_single(lab))
    from autoinfra import cloudstack
    with app.app_context():
        s = cloudstack.state()
        v, spec = cloudstack.extra_vars(s), cloudstack.zone_spec(s)
    assert v["kvm_bridge_name"] == "cloudbr0" and v["kvm_required_bridges"] == ["cloudbr1"]
    assert v["kvm_internal_net"] == {"bridge": "cloudbr1", "addresses": ["10.200.0.1/24", "10.201.0.1/24"],
                                     "nat_cidrs": ["10.200.0.0/24", "10.201.0.0/24"]}
    assert v["autoinfra_static_routes"] == [{"to": "10.200.0.0/24", "via": "10.0.1.21"}, {"to": "10.201.0.0/24", "via": "10.0.1.21"}]
    assert v["nfs_extra_cidrs"] == ["10.200.0.0/24"]
    assert spec["kvm_labels"] == {"management": "cloudbr1", "guest": "cloudbr1", "public": "cloudbr1"}
    assert spec["pod"]["gateway"] == "10.200.0.1" and spec["public"]["gateway"] == "10.201.0.1"


def test_without_internal_net_no_extra_vars(app, operator, lab):
    fill(operator, lab)
    from autoinfra import cloudstack
    with app.app_context():
        v = cloudstack.extra_vars(cloudstack.state())
    assert "kvm_internal_net" not in v and "autoinfra_static_routes" not in v and v["kvm_bridge_name"] == "cloudbr0"


# ---------------------------------------------------------------- resume a failed deployment
def test_resume_runs_only_the_failed_step_onward(app, operator, lab, cs_mock):
    fill(operator, lab, **_single(lab))
    from autoinfra import db
    with app.app_context():
        db.set_setting("cs_api_url", cs_mock.url)
        db.set_setting("cs_poll_interval", 0.01)
    r = operator.post("/api/cs/wizard/deploy", json={"force": True, "admin_password": "wrong"})   # zone step fails: login refused
    j1 = wait_job(operator, r.get_json()["job_id"], 120)
    assert j1["state"] == "failed" and j1["steps"][-1]["state"] == "failed" and all(s["state"] == "ok" for s in j1["steps"][:-1])
    n_playbooks = len(ansible_calls(app))
    # a resume that fails again can itself be resumed (offsets add up)
    r = operator.post("/api/cs/wizard/deploy", json={"force": True, "admin_password": "still-wrong", "resume": True})
    jr = wait_job(operator, r.get_json()["job_id"], 120)
    assert jr["state"] == "failed" and len(jr["steps"]) == 1
    r = operator.post("/api/cs/wizard/deploy", json={"force": True, "admin_password": "password", "resume": True})
    assert r.status_code == 202, r.get_json()
    j2 = wait_job(operator, r.get_json()["job_id"], 120)
    log = operator.get(f"/api/jobs/{j2['id']}/log.txt").data.decode()
    assert j2["state"] == "success", log
    assert len(j2["steps"]) == 1 and "resumed at step" in j2["title"] and len(ansible_calls(app)) == n_playbooks
    assert [z["name"] for z in cs_mock.zones.values()] == ["Zone-1"]
    # nothing left to resume
    r = operator.post("/api/cs/wizard/deploy", json={"force": True, "resume": True})
    assert r.status_code == 409 and "did not fail" in r.get_json()["error"]


def test_resume_refused_when_the_plan_changed(app, operator, lab, cs_mock):
    fill(operator, lab, **_single(lab))
    from autoinfra import db
    with app.app_context():
        db.set_setting("cs_api_url", cs_mock.url)
        db.set_setting("cs_poll_interval", 0.01)
    j1 = wait_job(operator, operator.post("/api/cs/wizard/deploy", json={"force": True, "admin_password": "wrong"}).get_json()["job_id"], 120)
    assert j1["state"] == "failed"
    assert operator.post("/api/cs/wizard/management", json={"usage": False}).status_code == 200    # removes a playbook
    r = operator.post("/api/cs/wizard/deploy", json={"force": True, "admin_password": "password", "resume": True})
    assert r.status_code == 409 and "wizard changed" in r.get_json()["error"]


def test_rerun_reuses_saved_keys_and_recovers_from_regenerated_keys(app, operator, lab, cs_mock):
    """Real CloudStack issues a NEW key pair on every registerUserKeys: a re-run must reuse the saved pair, and when the saved
    pair was regenerated elsewhere it logs in again (password given) or says what to do (no password)."""
    fill(operator, lab, **_single(lab))
    from autoinfra import db
    with app.app_context():
        db.set_setting("cs_api_url", cs_mock.url)
        db.set_setting("cs_poll_interval", 0.01)
    go = lambda body: wait_job(operator, operator.post("/api/cs/wizard/deploy", json=dict(body, force=True)).get_json()["job_id"], 120)
    assert go({"admin_password": "password"})["state"] == "success" and cs_mock.key_rotations == 1
    assert go({"admin_password": "password"})["state"] == "success" and cs_mock.key_rotations == 1      # saved keys reused
    cs_mock.rotate()                                                                                    # regenerated in CloudStack
    j = go({})
    log = operator.get(f"/api/jobs/{j['id']}/log.txt").data.decode()
    assert j["state"] == "failed" and "saved CloudStack API keys were rejected" in log
    j = go({"admin_password": "password"})
    log = operator.get(f"/api/jobs/{j['id']}/log.txt").data.decode()
    assert j["state"] == "success" and "logging in again" in log


def test_slow_sync_commands_get_a_long_read_timeout(monkeypatch):
    """addHost on real CloudStack answers only after setting up the agent (minutes); the client must not give up after 30 s."""
    from autoinfra import csapi
    seen = {}

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, *a): return b"{}"

    def fake_open(req, timeout):
        seen[urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)["command"][0]] = timeout
        return R()
    import json as _j, urllib.parse
    monkeypatch.setattr(_j, "load", lambda r: {})
    cs = csapi.CS("http://x", "k", "s")
    monkeypatch.setattr(cs._op, "open", fake_open)
    for c in ("listZones", "addHost", "createStoragePool", "addImageStore"):
        cs._call({"command": c})
    assert seen["listZones"] == 30 and seen["addHost"] >= 1800 and seen["createStoragePool"] >= 900 and seen["addImageStore"] >= 900
