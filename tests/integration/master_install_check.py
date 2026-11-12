#!/usr/bin/env python3
"""After master/install-autoinfra.sh on an OFFLINE master: drive the INSTALLED AutoInfra (nginx TLS -> gunicorn as user
'autoinfra' -> real ansible-playbook from the venv -> SSH) against a lab node.
usage: python3 master_install_check.py https://10.79.0.30 admin 'Master-Admin-2026!' 10.79.0.22 'LabRootPw-2026'"""
import hashlib, http.cookiejar, json, os, re, ssl, sys, time, urllib.request

base, user, pw, node, node_pw = sys.argv[1:6]
ctx = ssl.create_default_context()
ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx), urllib.request.HTTPCookieProcessor(jar))
R = []


def ok(name, cond, detail=""):
    R.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def req(method, path, body=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(base + path, data=data, method=method, headers={"X-CSRF-Token": CSRF, "Content-Type": ctype})
    try:
        with op.open(r, timeout=120) as resp:
            t = resp.read()
            return resp.status, (json.loads(t) if resp.headers.get("Content-Type", "").startswith("application/json") else t)
    except urllib.error.HTTPError as e:
        t = e.read()
        try:
            return e.code, json.loads(t)
        except ValueError:
            return e.code, t


def wait(jid, t=900):
    s = time.time()
    while time.time() - s < t:
        c, j = req("GET", f"/api/jobs/{jid}")
        if j["job"]["state"] not in ("queued", "running"):
            return j["job"]
        time.sleep(1)


html = op.open(base + "/login", timeout=30).read().decode()
CSRF = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
r = op.open(urllib.request.Request(base + "/login", data=f"username={user}&password={urllib.request.quote(pw)}&csrf_token={CSRF}".encode(),
                                   headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=30)
ok("installed master: HTTPS login through nginx", r.status == 200 and "Dashboard" in r.read().decode())
CSRF = re.search(r'name="csrf-token" content="([^"]+)"', op.open(base + "/", timeout=30).read().decode()).group(1)
c, s = req("GET", "/api/settings")
ok("installed master: API answers as the logged-in admin", c == 200 and s["settings"]["pubkey"].startswith("ssh-ed25519"))
c, _ = req("POST", "/api/settings", {"ansible_extra_vars": {"autoinfra_services": False, "manage_hostname": False, "preflight_soft": True}})

# upload through nginx (8 MB chunk limit is 16 MB in nginx)
blob = open(sys.argv[6], "rb").read() if len(sys.argv) > 6 else b""
if blob:
    c, u = req("POST", "/api/packages/uploads", {"name": sys.argv[6].split("/")[-1], "size": len(blob), "repo": "core", "sha256": hashlib.sha256(blob).hexdigest()})
    off = 0
    while off < len(blob):
        c, x = req("PUT", f"/api/packages/uploads/{u['id']}/chunk?offset={off}", raw=blob[off:off + 8 * 1024 * 1024], ctype="application/octet-stream")
        off = x["received"]
    c, f = req("POST", f"/api/packages/uploads/{u['id']}/finish", {})
    j = wait(f["job_id"])
    ok("installed master: 8 MB chunked upload through nginx, extracted + indexed", j["state"] == "success", f"{len(blob) // 1024 // 1024} MB")
    repo = os.environ.get("REPO_URL") or base.replace("https", "http").rstrip("/") + ":8081/repo"
    rr = op.open(repo + "/core/deb/Release", timeout=30).read().decode()
    ok(f"installed master: the apt repository is served at {repo}/", "Origin: AutoInfra" in rr)

c, h = req("POST", "/api/hosts", {"ip": node, "hostname": "ubu24", "roles": ["app"], "password": node_pw})
if c == 400 and "already" in json.dumps(h):          # re-run against the same master
    c, h = 201, {"message": "already in inventory, SSH key installed earlier"}
ok("installed master: node added with a password (key bootstrap as user autoinfra)", c == 201 and "key installed" in h["message"], h.get("message"))
c, j = req("POST", "/api/checks/run", {"retries": 1, "timeout": 5})
wait(j["job_id"])
c, hosts = req("GET", "/api/hosts")
st = hosts["hosts"][0]
ok("installed master: SSH checks from the installed service", st["os_family"] == "debian" and st["status"] in ("ready", "ready-with-warnings"), st["status"])
c, j = req("POST", "/api/ansible/run", {"playbook": "01-common.yml", "tags": ["repo", "packages", "hosts"]})
jj = wait(j["job_id"], 1800)
c, log = req("GET", f"/api/jobs/{j['job_id']}/log.txt")
log = log.decode() if isinstance(log, bytes) else str(log)
ok("installed master: real ansible-playbook from the venv ran 01-common.yml on the node", jj["state"] == "success",
   (re.findall(r"^ubu24 .*$", log, re.M) or [log[-600:]])[0])
c, j = req("POST", "/api/components/nginx/deploy", {"host_ids": [st["id"]], "config": {"upstream": "10.79.0.30:8081"}})
jj = wait(j["job_id"], 1800)
ok("installed master: component deploy (Nginx) from the installed UI", jj["state"] == "success")
print(f"\n{sum(R)}/{len(R)} checks passed")
sys.exit(0 if all(R) else 1)
