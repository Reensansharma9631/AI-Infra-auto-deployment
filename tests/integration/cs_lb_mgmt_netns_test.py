#!/usr/bin/env python3
"""
Phase-2 test: roles `cs_mgmt` and `cs_lb` on 4 light Ubuntu chroots (2 LB + 2 management servers), each in its own
network namespace on a bridge. REAL: ssh, ansible, haproxy, keepalived/VRRP, the VIP, health checks, failover.
FAKE (clearly): the CloudStack package - a stand-in `cloudstack-management` whose setup commands record their
arguments and whose service answers HTTP :8080 and TCP :8250 with its own IP. It proves OUR flow, not CloudStack itself.
Needs root, /tmp/chroot-db-base (with haproxy, keepalived, mysql-client), dpkg-deb.
"""
import json, os, pathlib, shutil, socket, subprocess, sys, time, urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
T = pathlib.Path("/tmp/csn"); BASE = "/tmp/chroot-db-base"
NODES = {"lb1": "10.78.0.21", "lb2": "10.78.0.22", "m1": "10.78.0.31", "m2": "10.78.0.32"}
VIP = "10.78.0.20"; KEYS = "/tmp/csn-keys"
PW1, PW2 = "AcsAdminPassw0rd1234567", "CloudDbPassw0rd1234567"
n_pass = 0


def sh(c, **k): return subprocess.run(c, shell=True, text=True, capture_output=True, **k)


def ok(name, cond, extra=""):
    global n_pass
    print(("    PASS " if cond else "    FAIL ") + name, flush=True)
    if not cond: raise SystemExit(f"FAILED: {name} {extra}")
    n_pass += 1


def wait_for(fn, timeout, what, step=1.0):
    end = time.time() + timeout; last = None
    while time.time() < end:
        try:
            r = fn()
            if r: return r
        except Exception as e: last = e
        time.sleep(step)
    print("--- VIP holders:", vip_holders())
    raise SystemExit(f"timeout ({timeout}s) waiting for {what} {last!r}")


def ns(n): return f"csn-{n}"
def root(n): return T / n
def nsh(n, cmd): return sh(f"ip netns exec {ns(n)} chroot {root(n)} /bin/bash -c {json.dumps(cmd)}")
def vip_holders(): return [n for n in ("lb1", "lb2") if f"{VIP}/" in sh(f"ip netns exec {ns(n)} ip -4 addr show eth0").stdout]


def http(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r: return r.read().decode().strip()


def agent_id():
    s = socket.create_connection((VIP, 8250), timeout=3); s.settimeout(3)
    try: return s.recv(100).decode().strip()
    finally: s.close()


def who(n=12):
    out = []
    for _ in range(n):
        try: out.append(http(f"http://{VIP}:8080/client/"))
        except Exception: out.append("ERR")
    return out


def teardown():
    sh(f"for p in $(ls /proc | grep -E '^[0-9]+$'); do r=$(readlink /proc/$p/root 2>/dev/null); case \"$r\" in {T}/*) kill -9 $p;; esac; done")
    time.sleep(2)
    sh(f"for m in $(findmnt -rn -o TARGET | grep '^{T}/' | sort -r); do umount -l $m; done")
    for n in NODES: sh(f"ip netns del {ns(n)} 2>/dev/null; ip link del v-{n} 2>/dev/null")
    sh("ip link del br-cs 2>/dev/null")


def fake_deb():
    d = pathlib.Path("/tmp/fake-cs-deb"); shutil.rmtree(d, ignore_errors=True)
    for p in ("DEBIAN", "usr/local/bin", "usr/local/lib/fake-cs", "etc/systemd/system"): (d / p).mkdir(parents=True)
    (d / "DEBIAN/control").write_text("Package: cloudstack-management\nVersion: 4.22.0-faketest\nArchitecture: all\nMaintainer: t <t@t>\nDescription: FAKE for tests\n")
    (d / "DEBIAN/postinst").write_text("#!/bin/sh\ngetent group cloud >/dev/null || groupadd cloud\nexit 0\n"); (d / "DEBIAN/postinst").chmod(0o755)
    f = d / "usr/local/bin"
    (f / "cloudstack-setup-databases").write_text("""#!/bin/bash
LOG=/var/log/fake-cs-calls.log; D=/etc/cloudstack/management; mkdir -p $D
echo "setup-databases $(echo "$*" | sed -E 's#(cloud:)[^@]*#\\1***#; s#(--deploy-as=[^:]*:)[^ ]*#\\1***#')" >> $LOG
conn=$1; shift; deploy=0; ip=""
while [ $# -gt 0 ]; do case $1 in --deploy-as=*) deploy=1;; -i) ip=$2; shift;; esac; shift; done
[ $deploy -eq 1 ] || { echo "fake: only the deploying server calls this" >&2; exit 2; }
[ -f $D/key ] || head -c 24 /dev/urandom | base64 > $D/key
printf 'db.cloud.host=%s\\ncluster.node.IP=%s\\ndb.cloud.password=ENC(fake)\\n' "${conn#*@}" "$ip" > $D/db.properties
getent group cloud >/dev/null && chgrp cloud $D/key $D/db.properties; chmod 640 $D/key $D/db.properties
touch /mnt/cloud-db
""")
    (f / "cloudstack-setup-management").write_text("""#!/bin/bash
D=/etc/cloudstack/management
[ -f $D/db.properties ] && [ -f $D/key ] || { echo "db.properties/key missing" >&2; exit 1; }
echo "setup-management" >> /var/log/fake-cs-calls.log
""")
    (f / "mysql").write_text("""#!/bin/bash
[ -n "$MYSQL_PWD" ] || { echo "no MYSQL_PWD" >&2; exit 1; }
echo "mysql-check $*" >> /var/log/fake-cs-calls.log
[ -f /mnt/cloud-db ] && echo 1 || echo 0
""")
    (d / "usr/local/lib/fake-cs/server.py").write_text('''import re, socketserver, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
props = open("/etc/cloudstack/management/db.properties").read()
ME = "mgmt@" + re.search(r"^cluster\\.node\\.IP=(.*)$", props, re.M).group(1)
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        b = ME.encode(); self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
class T(socketserver.BaseRequestHandler):
    def handle(self): self.request.sendall((ME + "\\n").encode())
class S(socketserver.ThreadingTCPServer): allow_reuse_address = True
threading.Thread(target=S(("0.0.0.0", 8250), T).serve_forever, daemon=True).start()
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
''')
    (d / "etc/systemd/system/cloudstack-management.service").write_text("[Service]\nExecStart=/usr/bin/python3 /usr/local/lib/fake-cs/server.py\n")
    for x in ("cloudstack-setup-databases", "cloudstack-setup-management", "mysql"): (f / x).chmod(0o755)
    sh(f"dpkg-deb --build {d} /tmp/fake-cloudstack-management.deb", check=False)


def make_node(n, ip):
    r = root(n); r.mkdir(parents=True, exist_ok=True)
    sh(f"tar -C {BASE} -cf - etc var root --exclude=var/lib/mysql --exclude=var/lib/apt/lists --exclude=var/cache | tar -C {r} -xf -")
    for l in ("bin", "sbin", "lib", "lib64"): sh(f"cp -a {BASE}/{l} {r}/{l}")
    for d in ("usr", "proc", "dev", "run", "tmp", "mnt", "sys"): (r / d).mkdir(exist_ok=True)
    sh(f"mount --bind {BASE}/usr {r}/usr && mount -o remount,ro,bind {r}/usr")
    ul = T / f"{n}.usrlocal"; sh(f"cp -a {BASE}/usr/local {ul}"); sh(f"mount --bind {ul} {r}/usr/local")
    sh(f"mount -t proc proc {r}/proc; mount --bind /dev {r}/dev; mount -t tmpfs tmpfs {r}/run; mount -t tmpfs tmpfs {r}/tmp; mount --bind {T}/shared {r}/mnt")
    sh(f"mkdir -p {r}/run/sshd {r}/run/systemd/system {r}/etc/systemd/system {r}/root/.ssh; chroot {r} ssh-keygen -A")
    shutil.copy(f"{ROOT}/tests/integration/chroot-systemctl.py", f"{r}/usr/local/bin/systemctl"); os.chmod(f"{r}/usr/local/bin/systemctl", 0o755)
    sh(f"printf '#!/bin/sh\\nexit 101\\n' > {r}/usr/sbin/policy-rc.d 2>/dev/null; true")
    (r / "root/.ssh/authorized_keys").write_text((T / "key.pub").read_text()); os.chmod(f"{r}/root/.ssh", 0o700); os.chmod(f"{r}/root/.ssh/authorized_keys", 0o600)
    sh(f"cp /tmp/fake-cloudstack-management.deb {r}/tmp/ ; ")
    if n.startswith("m"):
        res = sh(f"chroot {r} dpkg -i /tmp/fake-cloudstack-management.deb"); assert res.returncode == 0, res.stderr


def main():
    teardown(); shutil.rmtree(T, ignore_errors=True); shutil.rmtree(KEYS, ignore_errors=True); (T / "shared").mkdir(parents=True)
    sh("pkill -9 sshd; true"); sh(f"ssh-keygen -q -t ed25519 -N '' -f {T}/key")
    fake_deb()
    print("=== 0. network + 4 light chroots (2 load balancers, 2 management servers)")
    sh("ip link add br-cs type bridge && ip addr add 10.78.0.1/24 dev br-cs && ip link set br-cs up")
    for i, (n, ip) in enumerate(NODES.items(), 1):
        sh(f"ip netns add {ns(n)} && ip link add v-{n} type veth peer name e{i} && ip link set v-{n} master br-cs up && ip link set e{i} netns {ns(n)} && "
           f"ip netns exec {ns(n)} ip link set e{i} name eth0 && ip netns exec {ns(n)} ip addr add {ip}/24 dev eth0 && ip netns exec {ns(n)} ip link set eth0 up && ip netns exec {ns(n)} ip link set lo up")
        make_node(n, ip)
        subprocess.Popen(f"ip netns exec {ns(n)} chroot {root(n)} /usr/sbin/sshd -o UsePAM=no -o PermitRootLogin=prohibit-password", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    for n, ip in NODES.items():
        r = sh(f"ssh -i {T}/key -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=5 root@{ip} 'grep -c 24.04 /etc/os-release'")
        ok(f"{n} {ip}: real SSH reachable", r.stdout.strip() not in ("", "0"), r.stderr)
    inv = {"all": {"vars": {"ansible_user": "root", "ansible_ssh_private_key_file": f"{T}/key", "ansible_python_interpreter": "/usr/bin/python3"},
           "hosts": {ip: {"ansible_host": ip, "node_hostname": n} for n, ip in NODES.items()},
           "children": {"cs_lb": {"hosts": {NODES["lb1"]: {}, NODES["lb2"]: {}}}, "cs_mgmt": {"hosts": {NODES["m1"]: {}, NODES["m2"]: {}}}}}}
    (T / "inv.json").write_text(json.dumps(inv))
    (T / "vars.json").write_text(json.dumps({"db_vip": "10.78.0.99", "db_acs_admin_password": PW1, "cs_cloud_db_password": PW2, "cs_key_dir": KEYS,
        "cs_mgmt_vip": VIP, "cs_mgmt_vip_prefix": 24, "cs_mgmt_vrrp_pass": "abcd5678", "cs_mgmt_vrrp_id": 52}))

    def play(pb):
        env = dict(os.environ, ANSIBLE_CONFIG=str(ROOT / "ansible/ansible.cfg"), ANSIBLE_ROLES_PATH=str(ROOT / "ansible/roles"), ANSIBLE_NOCOLOR="1")
        r = subprocess.run(["ansible-playbook", "-i", str(T / "inv.json"), str(ROOT / f"ansible/{pb}"), "-e", f"@{T}/vars.json"], cwd=str(ROOT / "ansible"), env=env, capture_output=True, text=True)
        (T / f"{pb}.log").write_text(r.stdout + r.stderr); return r.returncode, r.stdout + r.stderr

    print("\n=== 1. 20-cloudstack-mgmt.yml")
    rc, log = play("20-cloudstack-mgmt.yml")
    ok("playbook succeeded (failed=0 on both servers)", rc == 0 and log.count("failed=0") == 2, log[-2500:])
    c1 = (root("m1") / "var/log/fake-cs-calls.log").read_text(); c2 = (root("m2") / "var/log/fake-cs-calls.log").read_text()
    ok("FIRST server deployed the schema through the DB VIP with its own IP", "setup-databases cloud:***@10.78.0.99 --deploy-as=acsadmin:*** -i 10.78.0.31" in c1, c1)
    ok("second server did NOT deploy (no setup-databases call)", "setup-databases" not in c2 and "mysql-check" in c2, c2)
    ok("passwords never appear in call logs", PW1 not in c1 + c2 and PW2 not in c1 + c2)
    k1 = (root("m1") / "etc/cloudstack/management/key").read_text(); k2 = (root("m2") / "etc/cloudstack/management/key").read_text()
    ok("both servers have the SAME encryption key", k1 == k2 and len(k1) > 10)
    p1 = (root("m1") / "etc/cloudstack/management/db.properties").read_text(); p2 = (root("m2") / "etc/cloudstack/management/db.properties").read_text()
    ok("each server has its OWN cluster.node.IP", "cluster.node.IP=10.78.0.31" in p1 and "cluster.node.IP=10.78.0.32" in p2)
    ok("key copies on the master are mode 600", oct(os.stat(f"{KEYS}/key").st_mode & 0o777) == "0o600" and oct(os.stat(f"{KEYS}/db.properties").st_mode & 0o777) == "0o600")
    ok("both management services answer on :8080", http("http://10.78.0.31:8080/") == "mgmt@10.78.0.31" and http("http://10.78.0.32:8080/") == "mgmt@10.78.0.32")

    print("\n=== 2. 21-cloudstack-lb.yml (real HAProxy + real VRRP)")
    rc, log = play("21-cloudstack-lb.yml")
    ok("playbook succeeded (failed=0 on both LBs)", rc == 0 and log.count("failed=0") == 2, log[-2500:])
    h = wait_for(lambda: vip_holders() if len(vip_holders()) == 1 else None, 30, "exactly one VIP holder")
    ok(f"VIP {VIP} is on exactly one LB node ({h[0]})", True)
    seen = set(who(14)); ok(f"UI/API through the VIP is balanced over both servers: {sorted(seen)}", seen == {"mgmt@10.78.0.31", "mgmt@10.78.0.32"})
    a1 = {agent_id() for _ in range(5)}; ok(f"agent port :8250 through the VIP works and is sticky per client: {a1}", len(a1) == 1 and list(a1)[0].startswith("mgmt@"))

    print("\n=== 3. failures")
    nsh("m1", "systemctl stop cloudstack-management")
    wait_for(lambda: set(who(8)) == {"mgmt@10.78.0.32"}, 25, "traffic leaves the dead backend")
    ok("management server 1 stopped -> HAProxy health check removes it, users still served by server 2", True)
    nsh("m1", "systemctl start cloudstack-management")
    wait_for(lambda: set(who(14)) == {"mgmt@10.78.0.31", "mgmt@10.78.0.32"}, 30, "backend returns")
    ok("server 1 back -> traffic balanced again", True)
    holder = vip_holders()[0]; other = "lb2" if holder == "lb1" else "lb1"
    nsh(holder, "systemctl stop haproxy")
    wait_for(lambda: vip_holders() == [other], 30, "VIP leaves the LB whose haproxy died", 1)
    wait_for(lambda: "ERR" not in who(4), 15, "VIP serves again"); ok(f"haproxy died on {holder} -> VIP moved to {other}, service continues", True)
    nsh(holder, "systemctl start haproxy"); time.sleep(6)
    cur = vip_holders()[0]; nsh(cur, "systemctl stop keepalived")
    wait_for(lambda: vip_holders() not in ([cur], []) and len(vip_holders()) >= 1 and cur not in vip_holders() or vip_holders() == [holder if cur == other else other], 30, "VIP moves when keepalived stops")
    wait_for(lambda: "ERR" not in who(4), 15, "VIP serves after keepalived stop"); ok(f"keepalived stopped on {cur} -> VIP moved, service continues", True)

    print("\n=== 4. re-run both playbooks")
    rc1, l1 = play("20-cloudstack-mgmt.yml"); rc2, l2 = play("21-cloudstack-lb.yml")
    ok("both playbooks are safe to re-run (failed=0)", rc1 == 0 and rc2 == 0 and l1.count("failed=0") == 2 and l2.count("failed=0") == 2, (l1 + l2)[-2000:])
    c1b = (root("m1") / "var/log/fake-cs-calls.log").read_text()
    ok("re-run did not deploy the schema again", c1b.count("setup-databases") == 1, c1b)
    print(f"\nALL {n_pass} CHECKS PASSED"); return 0


if __name__ == "__main__":
    code = 1
    try: code = main()
    except SystemExit as e: print(e)
    except BaseException:
        import traceback; traceback.print_exc()
    finally: teardown()
    sys.exit(code)
