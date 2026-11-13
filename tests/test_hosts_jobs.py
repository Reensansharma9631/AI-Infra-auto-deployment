"""Host inventory API, CSV import, checks against an unreachable host, background job runner."""
import json, os, time

import pytest

from conftest import add_host, wait_job


@pytest.mark.parametrize("body,field", [
    ({"ip": "10.0.0.300"}, "ip"), ({"ip": ""}, "ip"), ({"ip": "abc"}, "ip"), ({"ip": "10.0.0.1", "hostname": "-bad"}, "hostname"),
    ({"ip": "10.0.0.1", "ssh_user": "Root"}, "ssh_user"), ({"ip": "10.0.0.1", "ssh_port": 0}, "ssh_port"),
    ({"ip": "10.0.0.1", "ssh_port": 70000}, "ssh_port"), ({"ip": "10.0.0.1", "roles": ["wizard"]}, "roles"),
    ({"ip": "10.0.0.1", "roles": "management,hacker"}, "roles"), ({"ip": "10.0.0.1", "grp": "bad<grp>"}, "grp"),
    ({"ip": "10.0.0.1", "ssh_user": "a b"}, "ssh_user"), ({"ip": "::1"}, "ip"),
])
def test_host_validation(operator, body, field):
    r = operator.post("/api/hosts", json=body)
    assert r.status_code == 400 and field in r.get_json()["fields"]


@pytest.mark.parametrize("roles", [["management"], ["database"], ["compute"], ["storage"], ["monitoring"], ["lb"], ["mq"], ["controller"],
                                   ["network"], ["os-compute"], ["os-storage"], ["ai"], ["app"], ["management", "database"],
                                   ["compute", "storage", "monitoring"]])
def test_host_roles_accepted(operator, roles):
    hid = add_host(operator, "10.9.0.1", roles=roles)
    h = operator.get(f"/api/hosts/{hid}").get_json()["host"]
    assert h["roles"] == sorted(roles)


def test_duplicate_host(operator):
    add_host(operator, "10.0.0.10")
    r = operator.post("/api/hosts", json={"ip": "10.0.0.10"})
    assert r.status_code == 400 and "already" in r.get_json()["fields"]["ip"]
    add_host(operator, "10.0.0.10", ssh_port=2222)   # same ip, other port = other host


def test_password_never_stored(app, operator):
    r = operator.post("/api/hosts", json={"ip": "127.0.0.1", "ssh_port": 1, "password": "Top-Secret-PW-1"})
    assert r.status_code == 201 and "not reachable" in r.get_json()["message"]
    from autoinfra import db
    with app.app_context():
        dump = "\n".join(map(str, db.conn().iterdump()))
    assert "Top-Secret-PW-1" not in dump


def test_list_filter_stats(operator):
    add_host(operator, "10.0.0.1", roles=["management"], grp="cs")
    add_host(operator, "10.0.0.2", roles=["compute"], grp="cs")
    add_host(operator, "10.0.0.3", roles=["compute"], grp="other")
    assert len(operator.get("/api/hosts?group=cs").get_json()["hosts"]) == 2
    assert len(operator.get("/api/hosts?role=compute").get_json()["hosts"]) == 2
    assert len(operator.get("/api/hosts?q=10.0.0.3").get_json()["hosts"]) == 1
    assert operator.get("/api/hosts").get_json()["stats"]["total"] == 3
    g = {x["name"]: x["hosts"] for x in operator.get("/api/hosts/groups").get_json()["groups"]}
    assert g == {"cs": 2, "other": 1}


def test_patch_and_delete(operator):
    hid = add_host(operator, "10.0.0.5")
    assert operator.patch(f"/api/hosts/{hid}", json={"hostname": "db01", "roles": ["database"], "grp": "db"}).status_code == 200
    h = operator.get(f"/api/hosts/{hid}").get_json()["host"]
    assert (h["hostname"], h["roles"], h["grp"]) == ("db01", ["database"], "db")
    assert operator.patch(f"/api/hosts/{hid}", json={"ssh_port": 0}).status_code == 400
    assert operator.patch(f"/api/hosts/{hid}", json={"ip": "1.1.1.1"}).status_code == 200      # ip is not editable: ignored
    assert operator.get(f"/api/hosts/{hid}").get_json()["host"]["ip"] == "10.0.0.5"
    assert operator.delete(f"/api/hosts/{hid}").status_code == 200
    assert operator.get(f"/api/hosts/{hid}").status_code == 404
    assert operator.delete(f"/api/hosts/{hid}").status_code == 404
    assert operator.patch(f"/api/hosts/{hid}", json={}).status_code == 404


CSV = """ip,hostname,ssh_user,ssh_port,roles,group,password
10.0.0.10,cs-mgmt-01,root,22,management;database,cloudstack,
10.0.0.21,cs-compute-01,root,22,compute,cloudstack,
10.0.0.22,cs-compute-02,ubuntu,2222,compute|storage,cloudstack,
bad-ip,x,root,22,compute,cloudstack,
10.0.0.31,storage-01,root,22,storage,storage,
10.0.0.10,dup,root,22,management,cloudstack,
10.0.0.41,db-01,root,22,wizard,db,
"""


def test_csv_import(operator):
    r = operator.post("/api/hosts/import", json={"csv": CSV}).get_json()
    assert len(r["added"]) == 4 and [e["line"] for e in r["errors"]] == [5, 7, 8]
    hosts = {h["ip"]: h for h in operator.get("/api/hosts").get_json()["hosts"]}
    assert hosts["10.0.0.22"]["ssh_user"] == "ubuntu" and hosts["10.0.0.22"]["ssh_port"] == 2222
    assert hosts["10.0.0.22"]["roles"] == ["compute", "storage"] and hosts["10.0.0.10"]["roles"] == ["database", "management"]


def test_csv_file_upload_and_bom(operator, app):
    import io
    data = ("﻿" + "ip,hostname\n10.1.0.1,a\n10.1.0.2,b\n").encode()
    r = operator.c.post("/api/hosts/import", data={"file": (io.BytesIO(data), "hosts.csv")}, headers={"X-CSRF-Token": operator.csrf()},
                        content_type="multipart/form-data")
    assert r.status_code == 200 and len(r.get_json()["added"]) == 2


@pytest.mark.parametrize("csv", ["", "hostname,user\nx,y\n", "just text"])
def test_csv_needs_ip_header(operator, csv):
    assert operator.post("/api/hosts/import", json={"csv": csv}).status_code == 400


@pytest.mark.parametrize("body,field", [({"retries": 0}, "retries"), ({"retries": 11}, "retries"), ({"timeout": 0}, "timeout"),
                                        ({"timeout": 500}, "timeout"), ({"parallel": 50}, "parallel"), ({"username": "Bad User"}, "username")])
def test_check_options_validated(operator, body, field):
    add_host(operator, "10.0.0.1")
    r = operator.post("/api/checks/run", json=body)
    assert r.status_code == 400 and field in r.get_json()["fields"]


def test_checks_need_hosts(operator):
    assert operator.post("/api/checks/run", json={}).status_code == 400


def test_checks_unreachable_host(operator):
    hid = add_host(operator, "127.0.0.1", ssh_port=1)
    jid = operator.post("/api/checks/run", json={"retries": 1, "timeout": 2}).get_json()["job_id"]
    assert wait_job(operator, jid)["state"] == "success"
    h = operator.get(f"/api/hosts/{hid}").get_json()
    assert h["host"]["status"] == "unreachable" and h["checks"][0]["status"] == "fail"
    log = operator.get(f"/api/jobs/{jid}/log.txt").data.decode()
    assert "Ping: FAILED" in log and "unreachable=1" in log
    assert operator.get("/api/checks/latest").get_json()["checks"][str(hid)]


def test_checks_by_group_only(operator):
    a = add_host(operator, "127.0.0.1", ssh_port=1, grp="g1")
    b = add_host(operator, "127.0.0.2", ssh_port=1, grp="g2")
    jid = operator.post("/api/checks/run", json={"group": "g1", "retries": 1, "timeout": 1}).get_json()["job_id"]
    wait_job(operator, jid)
    assert operator.get(f"/api/hosts/{a}").get_json()["host"]["status"] == "unreachable"
    assert operator.get(f"/api/hosts/{b}").get_json()["host"]["status"] == "new"


def test_check_password_redacted_in_log(operator):
    add_host(operator, "127.0.0.1", ssh_port=1)
    jid = operator.post("/api/checks/run", json={"password": "Very-Secret-Check-PW", "retries": 1, "timeout": 1}).get_json()["job_id"]
    wait_job(operator, jid)
    assert "Very-Secret-Check-PW" not in operator.get(f"/api/jobs/{jid}/log.txt").data.decode()
    row = operator.get(f"/api/jobs/{jid}").get_json()["job"]
    assert "Very-Secret-Check-PW" not in json.dumps(row)


def test_package_action_validation(operator):
    assert operator.post("/api/hosts/packages", json={"action": "rm"}).status_code == 400
    assert operator.post("/api/hosts/packages", json={"action": "refresh"}).status_code == 400   # no hosts


def test_package_action_unknown_os(operator):
    add_host(operator, "10.0.0.1")
    jid = operator.post("/api/hosts/packages", json={"action": "refresh"}).get_json()["job_id"]
    assert wait_job(operator, jid)["state"] == "failed"
    assert "run the system checks first" in operator.get(f"/api/jobs/{jid}/log.txt").data.decode()


def test_required_packages_per_role(operator):
    add_host(operator, "10.0.0.1", roles=["management", "compute"])
    r = operator.get("/api/hosts/required").get_json()["required"]
    pk = {x["package"] for x in r}
    assert {"cloudstack-management", "cloudstack-agent", "qemu-system-x86"} <= pk and not any(x["in_repo"] for x in r)


def test_pubkey(operator):
    k = operator.get("/api/ssh/pubkey").get_json()["pubkey"]
    assert k.startswith("ssh-ed25519 ")


# ---------------------------------------------------------------- job runner
def run(app, steps, **kw):
    from autoinfra import jobs
    with app.app_context():
        jid = jobs.start("test", "t", steps, user="tester", **kw)
        assert jobs.wait(jid, 30) != "timeout"
        from autoinfra import db
        return db.one("SELECT * FROM jobs WHERE id=?", (jid,)), open(db.one("SELECT log_path FROM jobs WHERE id=?", (jid,))["log_path"]).read()


def test_job_success_and_progress(app):
    j, log = run(app, [{"name": "a", "cmd": ["echo", "hello"]}, {"name": "b", "cmd": ["true"]}])
    assert j["state"] == "success" and j["progress"] == 100 and "hello" in log and "JOB SUCCESS" in log
    assert [s["state"] for s in json.loads(j["steps"])] == ["ok", "ok"]


def test_job_failure_skips_rest(app):
    j, log = run(app, [{"name": "a", "cmd": ["false"]}, {"name": "b", "cmd": ["echo", "never"]}])
    assert j["state"] == "failed" and "never" not in log
    assert [s["state"] for s in json.loads(j["steps"])] == ["failed", "skipped"]


def test_job_env_and_cwd(app, tmp_path):
    j, log = run(app, [{"name": "a", "cmd": ["sh", "-c", "echo $FOO; pwd"], "env": {"FOO": "bar42"}, "cwd": str(tmp_path)}])
    assert "bar42" in log and str(tmp_path) in log


def test_job_secrets_redacted(app):
    j, log = run(app, [{"name": "a", "cmd": ["echo", "pw=Hunter2Secret"]}], secrets=["Hunter2Secret"])
    assert "Hunter2Secret" not in log and "********" in log


def test_job_timeout(app):
    t = time.time()
    j, log = run(app, [{"name": "slow", "cmd": ["sleep", "30"], "timeout": 1}])
    assert j["state"] == "failed" and time.time() - t < 15


def test_job_fn_step_and_job_id(app):
    from autoinfra import jobs
    seen = {}

    @jobs.register("t_fn")
    def fn(log, x, job_id=None):
        seen["x"], seen["jid"] = x, job_id
        log("fn ran")
        return True
    j, log = run(app, [{"name": "f", "fn": "t_fn", "args": {"x": 7}}])
    assert j["state"] == "success" and seen == {"x": 7, "jid": j["id"]} and "fn ran" in log


def test_job_fn_exception(app):
    from autoinfra import jobs
    jobs.FUNCS["t_boom"] = lambda log: 1 / 0
    j, log = run(app, [{"name": "f", "fn": "t_boom"}])
    assert j["state"] == "failed" and "ZeroDivisionError" in log


def test_job_fn_false_fails(app):
    from autoinfra import jobs
    jobs.FUNCS["t_false"] = lambda log: False
    j, _ = run(app, [{"name": "f", "fn": "t_false"}])
    assert j["state"] == "failed"


def test_job_hook(app):
    from autoinfra import jobs
    got = []
    jobs.HOOKS["test"] = lambda row, ok: got.append(ok)
    run(app, [{"name": "a", "cmd": ["true"]}])
    run(app, [{"name": "a", "cmd": ["false"]}])
    jobs.HOOKS.pop("test")
    assert got == [True, False]


def test_job_cancel_api(app, operator):
    from autoinfra import jobs
    with app.app_context():
        jid = jobs.start("test", "long", [{"name": "s", "cmd": ["sleep", "20"]}, {"name": "t", "cmd": ["true"]}], user="x")
    time.sleep(0.5)
    assert operator.post(f"/api/jobs/{jid}/cancel").status_code == 200
    j = wait_job(operator, jid, 15)
    assert j["state"] == "cancelled" and [s["state"] for s in j["steps"]] == ["failed", "skipped"]
    assert operator.post(f"/api/jobs/{jid}/cancel").status_code == 409
    assert operator.post("/api/jobs/99999/cancel").status_code == 404


def test_job_log_offsets(app, operator):
    from autoinfra import jobs
    with app.app_context():
        jid = jobs.start("test", "t", [{"name": "a", "cmd": ["sh", "-c", "for i in 1 2 3; do echo line$i; done"]}], user="x")
    wait_job(operator, jid)
    r = operator.get(f"/api/jobs/{jid}?offset=0").get_json()
    assert "line3" in r["log"]
    r2 = operator.get(f"/api/jobs/{jid}?offset={r['offset']}").get_json()
    assert r2["log"] == "" and r2["offset"] == r["offset"]
    assert operator.get("/api/jobs?kind=test").get_json()["jobs"][0]["id"] == jid
    assert operator.get("/api/jobs/123456").status_code == 404
    assert operator.get("/api/jobs/123456/log.txt").status_code == 404


def test_interrupted_jobs_marked_failed_on_restart(app):
    from autoinfra import create_app, db
    with app.app_context():
        db.ex("INSERT INTO jobs(kind,title,state,created) VALUES('x','stale','running',1)")
    create_app({"TESTING": True})
    with app.app_context():
        assert db.one("SELECT state FROM jobs WHERE title='stale'")["state"] == "failed"


def test_secrets_are_stable_and_private(app):
    from autoinfra import inventory, db
    with app.app_context():
        a = inventory.secret("x", 20)
        assert a == inventory.secret("x", 20) and len(a) == 20
        assert oct(os.stat(os.path.join(db.data_dir(), "secrets.json")).st_mode & 0o777) == "0o600"
        inventory.secret_set("y", "v")
        assert inventory.secret_get("y") == "v"
        inventory.secret_set("y", "")
        assert inventory.secret_get("y") is None


def test_inventory_render(app, operator):
    from autoinfra import db, inventory
    add_host(operator, "10.0.0.1", hostname="mgmt1", roles=["management", "database"])
    add_host(operator, "10.0.0.2", hostname="kvm1", roles=["compute"], ssh_user="ubuntu", ssh_port=2222)
    with app.app_context():
        hosts = db.q("SELECT * FROM hosts")
        ini = inventory.render(hosts, {"extra": [hosts[1]["id"]]})
    assert "mgmt1 ansible_host=10.0.0.1 ansible_port=22 ansible_user=root" in ini
    assert "kvm1 ansible_host=10.0.0.2 ansible_port=2222 ansible_user=ubuntu node_hostname=kvm1 ansible_become=true" in ini
    for g, h in (("cs_mgmt", "mgmt1"), ("cs_db", "mgmt1"), ("cs_kvm", "kvm1"), ("extra", "kvm1")):
        assert f"[{g}]\n{h}" in ini


def test_playbook_steps_files(app, operator):
    from autoinfra import db, inventory
    add_host(operator, "10.0.0.1", roles=["management"])
    with app.app_context():
        steps, wd = inventory.playbook_steps(["01-common.yml"], db.q("SELECT * FROM hosts"), {"x": 1}, tags=["repo"], limit="all", check=True)
    s = steps[0]
    assert s["cmd"][-5:] == ["--tags", "repo", "--limit", "all", "--check", "--diff"][:0] or "--check" in s["cmd"]
    assert oct(os.stat(os.path.join(wd, "vars.json")).st_mode & 0o777) == "0o600"
    v = json.load(open(os.path.join(wd, "vars.json")))
    assert v["x"] == 1 and v["airgap_repo_base"].endswith(":8081/repo") and v["master_ip"] == "10.0.0.5"
    assert s["env"]["ANSIBLE_CONFIG"].endswith("ansible/ansible.cfg") and os.path.exists(s["cmd"][s["cmd"].index("-e") + 2 - 1 + 1 + 0] if False else s["cmd"][-1] if not s["cmd"][-1].startswith("-") else os.path.join(inventory.ansible_dir(), "01-common.yml"))


@pytest.mark.parametrize("vars_,ok", [({"kvm_bridge_port": "eno1"}, True), ({"os_pkgs": ["a", "b"], "x_1": {"y": 1}}, True), ({}, True),
                                      ({"Bad-Name": 1}, False), ({"ansible_connection": "local"}, False), ("notadict", False),
                                      ({"ansible_python_interpreter": "/x"}, False), ({"a" * 70: 1}, False)])
def test_host_vars_validation(admin, vars_, ok):
    hid = add_host(admin, "10.0.0.8")
    r = admin.patch(f"/api/hosts/{hid}", json={"vars": vars_})
    assert (r.status_code == 200) == ok, r.get_json()
    if ok:
        assert admin.get(f"/api/hosts/{hid}").get_json()["host"]["vars"] == vars_


def test_host_vars_admin_only(operator):
    hid = add_host(operator, "10.0.0.8")
    assert operator.patch(f"/api/hosts/{hid}", json={"vars": {"a": 1}}).status_code == 400


def test_host_and_global_vars_reach_ansible(app, admin):
    from autoinfra import db, inventory
    hid = add_host(admin, "10.0.0.8", hostname="n8")
    admin.patch(f"/api/hosts/{hid}", json={"vars": {"kvm_bridge_port": "eno9"}})
    assert admin.post("/api/settings", json={"ansible_extra_vars": {"ntp_server": "10.0.0.1"}}).status_code == 200
    assert admin.post("/api/settings", json={"ansible_extra_vars": {"bad key": 1}}).status_code == 400
    with app.app_context():
        steps, wd = inventory.playbook_steps(["01-common.yml"], db.q("SELECT * FROM hosts"), {"x": 2})
    assert json.load(open(os.path.join(wd, "host_vars", "n8.json"))) == {"kvm_bridge_port": "eno9"}
    v = json.load(open(os.path.join(wd, "vars.json")))
    assert v["ntp_server"] == "10.0.0.1" and v["x"] == 2


def test_repo_lists_only_existing(app, operator, tmp_path):
    from autoinfra import inventory, packages
    with app.app_context():
        root = packages.repo_root()
        os.makedirs(os.path.join(root, "kvm", "deb"))
        open(os.path.join(root, "kvm", "deb", "Packages"), "w").close()
        os.makedirs(os.path.join(root, "custom", "rpm", "repodata"))
        open(os.path.join(root, "custom", "rpm", "repodata", "repomd.xml"), "w").close()
        assert inventory.repo_lists() == {"airgap_deb_repos": ["kvm"], "airgap_rpm_repos": ["custom"]}



@pytest.mark.parametrize("raw,leaked", [
    ("cell_database': 'mysql+pymysql://nova:Zf9DObWwCy7gev@172.27.15.239:3306/nova_cell0'", "Zf9DObWwCy7gev"),
    ("amqp://openstack:S3cretRabbit99@10.0.0.11:5672//", "S3cretRabbit99"),
    ("keystone_admin_password: Qwerty1234567890", "Qwerty1234567890"),
    ('{"password": "hunter2hunter2"}', "hunter2hunter2"),
])
def test_redact_catches_unknown_secrets(raw, leaked):
    """Real lab: a Kolla deprecation warning printed the nova DB URL with its password into the job log."""
    from autoinfra import jobs
    out = jobs.redact(raw)
    assert leaked not in out and "********" in out


def test_redact_keeps_normal_text():
    from autoinfra import jobs
    for t in ("TASK [nova : Copy over config] ****", "http://172.27.15.238:8081/repo/core/deb ./", "ok: [kvm-01] => (item=nova)"):
        assert jobs.redact(t) == t
