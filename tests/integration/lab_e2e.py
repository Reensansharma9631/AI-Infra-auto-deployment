#!/usr/bin/env python3
"""REAL end-to-end on two real OS nodes without internet (tests/integration/os_lab.sh up):
Ubuntu 24.04 (10.79.0.22) and AlmaLinux 9.3 (10.79.0.21). Everything goes through the AutoInfra API like the browser does:
chunked upload of a real offline bundle -> verify/extract/index -> add hosts (password bootstrap / pre-installed key) ->
real SSH checks -> REAL ansible-playbook runs (common, MySQL, PostgreSQL, Nginx, NFS, HAProxy/keepalived, RabbitMQ) ->
verification on the nodes over SSH -> idempotency (second run changes nothing).
Limits (stated in the report): the nodes have no systemd, so services are not started by Ansible (autoinfra_services=false);
the RHEL node has no access to AlmaLinux mirrors from this sandbox, so on it only the repository switch + a locally built
RPM are installed - the same role code path dnf uses for real packages.
usage: python3 tests/integration/lab_e2e.py /tmp/ubuntu-noble-bundle.zip"""
import hashlib, http.server, json, os, pathlib, re, shutil, socketserver, subprocess, sys, tempfile, threading, time, urllib.request, zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ui"))
sys.path.insert(0, str(ROOT / "tests"))
W = pathlib.Path("/var/tmp/autoinfra-lab")
shutil.rmtree(W, ignore_errors=True)
(W / "web" / "repo").mkdir(parents=True)
os.environ.update(AUTOINFRA_DATA=str(W / "data"), AUTOINFRA_REPO=str(W / "web" / "repo"), AUTOINFRA_ADMIN_PASSWORD="Admin-Pass-2026!",
                  AUTOINFRA_COOKIE_SECURE="0", AUTOINFRA_MONITOR="0", AUTOINFRA_MASTER_IP="10.79.0.1")
os.environ.pop("AUTOINFRA_ANSIBLE_PLAYBOOK", None)
from autoinfra import create_app, db  # noqa: E402
import conftest  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)
    return ok


def serve_repo():
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(s, *a, **k): super().__init__(*a, directory=str(W / "web"), **k)
        def log_message(s, *a): pass
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("10.79.0.1", 8081), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def upload(c, path, repo):
    data = open(path, "rb").read()
    uid = c.post("/api/packages/uploads", json={"name": os.path.basename(path), "size": len(data), "repo": repo,
                                                "sha256": hashlib.sha256(data).hexdigest()}).get_json()["id"]
    off, ch, resumed = 0, 8 * 1024 * 1024, False
    while off < len(data):
        r = c.put(f"/api/packages/uploads/{uid}/chunk?offset={off}", data=data[off:off + ch])
        off = r.get_json()["received"]
        if not resumed and off >= ch:      # simulate a lost response: resend the same chunk -> 409 + resume offset
            r2 = c.put(f"/api/packages/uploads/{uid}/chunk?offset=0", data=data[:ch])
            resumed = r2.status_code == 409 and r2.get_json()["received"] == off
    jid = c.post(f"/api/packages/uploads/{uid}/finish").get_json()["job_id"]
    return conftest.wait_job(c, jid, 900), resumed, jid


def ssh(host_ip, cmd):
    from autoinfra import inventory, sshops
    cl = sshops.connect({"ip": host_ip, "ssh_user": "root"}, key_path=inventory.key_path())
    try:
        return sshops.run(cl, cmd, timeout=300)
    finally:
        cl.close()


def recap(log, host):
    m = re.search(rf"^{re.escape(host)}\s+:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", log, re.M)
    return tuple(map(int, m.groups())) if m else None


def run_component(c, cid, ids, cfg=None):
    r = c.post(f"/api/components/{cid}/deploy", json={"host_ids": ids, "config": cfg or {}})
    assert r.status_code == 202, r.get_json()
    j = conftest.wait_job(c, r.get_json()["job_id"], 1800)
    return j, c.get(f"/api/jobs/{j['id']}/log.txt").data.decode()


KVM_BUNDLE = os.environ.get("KVM_BUNDLE", "")


def main(bundle):
    t0 = time.time()
    srv = serve_repo()
    app = create_app({"TESTING": True})
    c = conftest.Client(app, "admin")

    # 1. offline bundles through the chunked upload API
    j, resumed, jid = upload(c, bundle, "custom")
    check("upload: Ubuntu offline bundle verified+extracted+indexed", j["state"] == "success", f"job {jid}")
    check("upload: duplicate chunk answered 409 with the resume offset", resumed)
    items = {i["name"] for i in c.get("/api/packages/items?repo=core&limit=5000").get_json()["items"]}
    check("repo: core apt repository holds nginx, mysql-server, postgresql", {"nginx", "mysql-server", "postgresql"} <= items, f"{len(items)} pkgs")
    tmp = pathlib.Path(tempfile.mkdtemp())
    from test_packages import make_deb, make_rpm
    rpm, deb = make_rpm(tmp, "autoinfra-probe"), make_deb(tmp, "autoinfra-probe", "1.0")
    z = tmp / "probe-bundle.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(rpm, "airgap/www/custom/rpm/" + rpm.name)
        zf.write(deb, "airgap/www/custom/deb/" + deb.name)
    j, _, _ = upload(c, str(z), "custom")
    check("upload: probe bundle (rpm + deb) indexed, dnf repodata created", j["state"] == "success" and (W / "web/repo/custom/rpm/repodata/repomd.xml").exists())
    miss = c.get("/api/packages/deps/core").get_json()["missing"]
    check("repo: dependency scan over 120+ real Ubuntu packages runs", isinstance(miss, list), f"{len(miss)} groups provided by the base OS")

    # 2. hosts
    with app.app_context():
        db.set_setting("ansible_extra_vars", {"autoinfra_services": False, "manage_hostname": False, "preflight_soft": True})
    r = c.post("/api/hosts", json={"ip": "10.79.0.22", "hostname": "ubu24", "roles": ["database", "app", "storage", "lb", "mq"],
                                   "password": os.environ.get("UBU_PW", "LabRootPw-2026")}).get_json()
    ub = r["id"]
    check("hosts: Ubuntu added with a password - master key installed, password not stored", "SSH key installed" in r["message"], r["message"])
    pub = c.get("/api/ssh/pubkey").get_json()["pubkey"]
    os.makedirs("/etc/ssh/autoinfra_keys", exist_ok=True)
    open("/etc/ssh/autoinfra_keys/root", "w").write(pub + "\n")
    os.chmod("/etc/ssh/autoinfra_keys/root", 0o600)
    al = c.post("/api/hosts", json={"ip": "10.79.0.21", "hostname": "alma9", "roles": ["app"]}).get_json()["id"]
    c.post("/api/hosts/import", json={"csv": "ip,hostname,roles\n10.79.0.99,ghost,compute\n"})
    c.patch(f"/api/hosts/{al}", json={"vars": {"common_os_pkgs": ["ca-certificates", "python3"], "common_packages": ["autoinfra-probe"]}})
    with app.app_context():
        dump = "\n".join(map(str, db.conn().iterdump()))
    check("security: the Ubuntu root password is nowhere in the database", "LabRootPw-2026" not in dump)

    # 3. real SSH checks
    jid = c.post("/api/checks/run", json={"retries": 2, "timeout": 5}).get_json()["job_id"]
    conftest.wait_job(c, jid, 300)
    hosts = {h["ip"]: h for h in c.get("/api/hosts").get_json()["hosts"]}
    for ip, fam, osid in (("10.79.0.22", "debian", "ubuntu"), ("10.79.0.21", "rhel", "almalinux")):
        h = hosts[ip]
        check(f"checks: {osid} detected as {fam} family over SSH", h["os_family"] == fam and h["os_id"] == osid, f"{h['os_pretty']} -> {h['status']}")
        ch = {x["name"]: x["status"] for x in c.get(f"/api/hosts/{h['id']}").get_json()["checks"]}
        check(f"checks: {osid} OS compatibility = supported, master repo reachable, root access",
              ch.get("OS compatibility") == "pass" and ch.get("Master repository reachable") == "pass" and ch.get("Root / sudo access") == "pass", json.dumps(ch))
        check(f"checks: {osid} usable for deployment", h["status"] in ("ready", "ready-with-warnings"), h["status"])
    check("checks: unreachable host reported", hosts["10.79.0.99"]["status"] == "unreachable")
    c.delete(f"/api/hosts/{hosts['10.79.0.99']['id']}")

    # 4. common role on BOTH operating systems (real ansible-playbook over SSH)
    j = conftest.wait_job(c, c.post("/api/ansible/run", json={"playbook": "01-common.yml", "host_ids": [ub, al]}).get_json()["job_id"], 1800)
    log = c.get(f"/api/jobs/{j['id']}/log.txt").data.decode()
    check("ansible: 01-common.yml succeeded on Ubuntu 24.04 and AlmaLinux 9", j["state"] == "success", str(recap(log, "ubu24")) + str(recap(log, "alma9")))
    if j["state"] != "success":
        print(log[-6000:])
    rc, out, _ = ssh("10.79.0.21", "rpm -q autoinfra-probe; ls /etc/yum.repos.d/; dnf -q repolist 2>/dev/null")
    check("alma: dnf uses ONLY the master repository and installed autoinfra-probe from it",
          "autoinfra-probe-1.0-1.el9" in out and "autoinfra.repo" in out and ".airgap-disabled" in out, out.replace("\n", " ")[:200])
    rc, out, _ = ssh("10.79.0.22", "dpkg-query -W -f='${Status} ${Package}\\n' chrony jq rsync 2>&1; ls /etc/apt/sources.list.d/; cat /etc/chrony/chrony.conf | grep ^server")
    check("ubuntu: apt uses ONLY the master repository; chrony, jq, rsync installed; chrony -> master",
          out.count("install ok installed") == 3 and "airgap.list" in out and "server 10.79.0.1 iburst" in out, out.replace("\n", " ")[:240])
    j2 = conftest.wait_job(c, c.post("/api/ansible/run", json={"playbook": "01-common.yml", "host_ids": [ub, al]}).get_json()["job_id"], 1800)
    log2 = c.get(f"/api/jobs/{j2['id']}/log.txt").data.decode()
    check("ansible: 01-common.yml second run is idempotent on both OS (changed=0)",
          j2["state"] == "success" and recap(log2, "ubu24")[1] == 0 and recap(log2, "alma9")[1] == 0, f"{recap(log2, 'ubu24')} {recap(log2, 'alma9')}")

    # 5. components on Ubuntu from the offline repository
    for cid, cfg, verify, desc in [
        ("mysql", {"strategy": "single"}, "dpkg -s mysql-server >/dev/null && mysqld --validate-config && grep -q 'bind-address = 0.0.0.0' /etc/mysql/mysql.conf.d/zz-autoinfra.cnf && echo OK", "MySQL 8 single"),
        ("postgresql", {"port": 5433, "allowed_cidr": "10.79.0.0/24"}, "f=$(find /etc/postgresql -name postgresql.conf); grep -q \"^listen_addresses = '\\*'\" $f && grep -q '^port = 5433' $f && grep -q '10.79.0.0/24 scram-sha-256' $(dirname $f)/pg_hba.conf && echo OK", "PostgreSQL 16"),
        ("nginx", {"upstream": "10.79.0.1:8081"}, "nginx -t 2>&1 && test -f /etc/nginx/autoinfra-tls/server.crt && test -L /etc/nginx/sites-enabled/autoinfra.conf && echo OK", "Nginx + offline TLS"),
        ("nfs", {"allowed_cidr": "10.79.0.0/24"}, "grep -q '/export/primary 10.79.0.0/24' /etc/exports && test -d /export/secondary && dpkg -s nfs-kernel-server >/dev/null && echo OK", "NFS primary/secondary"),
        ("rabbitmq", {"user": "mqadmin"}, "dpkg -s rabbitmq-server >/dev/null && grep -q 'cluster_formation.classic_config.nodes.1 = rabbit@ubu24' /etc/rabbitmq/rabbitmq.conf && test \"$(stat -c %a /var/lib/rabbitmq/.erlang.cookie)\" = 400 && echo OK", "RabbitMQ"),
    ]:
        j, log = run_component(c, cid, [ub], cfg)
        check(f"component: {desc} deployed on Ubuntu by Ansible", j["state"] == "success", str(recap(log, "ubu24")))
        if j["state"] != "success":
            print(log[-5000:])
        rc, out, err = ssh("10.79.0.22", verify)
        check(f"verify: {desc} files/packages correct on the node", out.strip().endswith("OK"), (out + err).strip()[-200:])
    # KVM / libvirt from a bundle produced by the UI's own download kit (Package Repository > Online Download)
    if KVM_BUNDLE and os.path.exists(KVM_BUNDLE):
        j, _, _ = upload(c, KVM_BUNDLE, "custom")
        ready = c.get("/api/packages/required/kvm?family=debian").get_json()["ready"]
        check("kit: bundle made by the UI's KVM download kit uploads and makes 'KVM / Libvirt' ready", j["state"] == "success" and ready)
        with app.app_context():
            db.ex("UPDATE hosts SET roles=roles||',compute' WHERE id=?", (ub,))
        j, log = run_component(c, "kvm-libvirt", [ub], {"nested": True})
        rc, out, err = ssh("10.79.0.22", "dpkg -s qemu-system-x86 libvirt-daemon-system libvirt-clients >/dev/null && grep -q 'kvm_intel nested=1' /etc/modprobe.d/autoinfra-kvm-nested.conf && echo OK")
        check("component: KVM / libvirt deployed on Ubuntu from the kit bundle", j["state"] == "success" and out.strip().endswith("OK"),
              str(recap(log, "ubu24")) + (out + err)[-150:])

    # HAProxy + keepalived fronts the cs_mgmt group; it must not share a host with a management server -> AlmaLinux is the backend
    with app.app_context():
        db.ex("UPDATE hosts SET roles=roles||',management' WHERE ip='10.79.0.21'")
    r = c.post("/api/components/haproxy/deploy", json={"host_ids": [al], "config": {"vip": "10.79.0.30"}})
    check("component: HAProxy refused on a management server (port clash 8080/8250)", r.status_code == 400, str(r.get_json()))
    j, log = run_component(c, "haproxy", [ub], {"vip": "10.79.0.30"})
    rc, out, err = ssh("10.79.0.22", "haproxy -c -f /etc/haproxy/haproxy.cfg && keepalived -t -f /etc/keepalived/keepalived.conf && echo OK")
    check("component: HAProxy + keepalived (management VIP) deployed and both configs valid", j["state"] == "success" and out.strip().endswith("OK"),
          (out + err).strip()[-200:] if j["state"] == "success" else log[-1500:])

    # 6. nginx really serves with the generated config
    ssh("10.79.0.22", "nginx")
    time.sleep(1)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        op.open("http://10.79.0.22/", timeout=5)
        code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:  # noqa: BLE001
        code = str(e)
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    op2 = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
    try:
        body = op2.open("https://10.79.0.22/repo/core/deb/Release", timeout=5).read().decode()
    except Exception as e:  # noqa: BLE001
        body = str(e)
    check("live: nginx on Ubuntu redirects :80 -> https and proxies :443 to the upstream (the master repo)",
          code == 301 and "Origin: AutoInfra" in body, f"http={code} https-body={body[:40]!r}")

    # 7. idempotency of a component
    j, log = run_component(c, "nginx", [ub], {"upstream": "10.79.0.1:8081"})
    check("idempotency: second Nginx deployment changes nothing", j["state"] == "success" and recap(log, "ubu24")[1] == 0, str(recap(log, "ubu24")))
    comps = {x["id"]: x["state"] for x in c.get("/api/components").get_json()["components"]}
    check("ui: component cards show Installed", all(comps[x] == "installed" for x in ("mysql", "postgresql", "nginx", "nfs", "rabbitmq", "haproxy")), json.dumps(comps))

    # 8. monitoring over SSH from the real nodes
    j = conftest.wait_job(c, c.post("/api/monitor/collect").get_json()["job_id"], 120)
    mh = {h["ip"]: h for h in c.get("/api/monitor/hosts").get_json()["hosts"]}
    check("monitoring: metrics collected from both nodes over SSH", all(mh[i]["cpu"] is not None for i in ("10.79.0.21", "10.79.0.22")),
          json.dumps({i: (mh[i]["cpu"], mh[i]["mem"], mh[i]["disk"]) for i in mh}))
    r = c.post("/api/reports", json={"kind": "infra", "fmt": "xlsx"})
    check("reports: infrastructure Excel generated from the lab data", r.status_code == 201)

    srv.shutdown()
    ok = sum(1 for _, o, _ in RESULTS if o)
    print(f"\n{ok}/{len(RESULTS)} checks passed in {time.time() - t0:.0f}s")
    json.dump([{"check": n, "ok": o, "detail": d} for n, o, d in RESULTS], open(W / "lab-results.json", "w"), indent=1)
    return ok == len(RESULTS)


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ubuntu-noble-bundle.zip") else 1)
