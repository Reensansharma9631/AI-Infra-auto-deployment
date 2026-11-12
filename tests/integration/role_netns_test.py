#!/usr/bin/env python3
"""
REAL network test of role `mysql_ha`: 3 Ubuntu chroots, each in its OWN network namespace joined by a bridge
(10.77.0.11-13, VIP 10.77.0.10). Real mysqld on 0.0.0.0:3306 per node, real dbha-agent, REAL keepalived/VRRP.
Network failures are real: the veth of a node is taken down. Ansible reaches nodes over real SSH.
Needs root, /tmp/chroot-db-base, ip(8) with netns+bridge support.
"""
import json, os, pathlib, shutil, subprocess, sys, time, urllib.request
import pymysql

ROOT = pathlib.Path(__file__).resolve().parents[2]
T = pathlib.Path("/tmp/netns-test"); BASE = "/tmp/chroot-db-base"
IPS = ["10.77.0.11", "10.77.0.12", "10.77.0.13"]; VIP = "10.77.0.10"
NS = {ip: f"ha{i}" for i, ip in enumerate(IPS, 1)}; CH = {ip: f"/tmp/chroot-db{i}" for i, ip in enumerate(IPS, 1)}
VETH = {ip: f"vh{i}" for i, ip in enumerate(IPS, 1)}
ACS_PW, DBHA_PW = "AcsAdminPassw0rd1234567", "DbhaClusterPassw0rd12345"
n_pass = 0


def sh(c, check=False, **k):
    r = subprocess.run(c, shell=True, text=True, capture_output=True, **k)
    if check and r.returncode: raise RuntimeError(f"{c}\n{r.stdout}\n{r.stderr}")
    return r


def ok(name, cond, extra=""):
    global n_pass
    print(("    PASS " if cond else "    FAIL ") + name, flush=True)
    if not cond: raise SystemExit(f"FAILED: {name} {extra}")
    n_pass += 1


def wait_for(fn, timeout, what, step=1.5):
    end = time.time() + timeout; last = None
    while time.time() < end:
        try:
            r = fn()
            if r: return r
        except Exception as e: last = e
        time.sleep(step)
    dump()
    raise SystemExit(f"timeout ({timeout}s) waiting for {what} {last!r}")


def dump():
    print("--- view:", json.dumps({ip: (lambda s: s and {k: s.get(k) for k in ('role', 'writable', 'replication_ok', 'action')})(status(ip)) for ip in IPS}))
    print("--- VIP holders:", vip_holders())


def csh(ip, cmd): return sh(f"ip netns exec {NS[ip]} chroot {CH[ip]} /bin/bash -c {json.dumps(cmd)}")


def status(ip):
    try:
        with urllib.request.urlopen(f"http://{ip}:8686/status", timeout=2) as r: return json.load(r)
    except Exception: return None


def one_primary(skip=()):
    st = {ip: status(ip) for ip in IPS if ip not in skip}
    prim = [ip for ip, s in st.items() if s and s["role"] == "primary"]
    if len(prim) != 1: return None
    if any(ip != prim[0] and not (s and s["role"] == "replica" and s["replication_ok"]) for ip, s in st.items()): return None
    return prim[0]


def vip_holders(skip=()):
    return [ip for ip in IPS if ip not in skip and f"{VIP}/" in sh(f"ip netns exec {NS[ip]} ip -4 addr show eth0").stdout]


def q(host, sql, user="acsadmin", pw=ACS_PW):
    c = pymysql.connect(host=host, port=3306, user=user, password=pw, connect_timeout=4, read_timeout=8, autocommit=True, cursorclass=pymysql.cursors.DictCursor)
    try:
        with c.cursor() as cur: cur.execute(sql); return cur.fetchall()
    finally: c.close()


def teardown():
    for ip in IPS:
        sh(f"for p in $(ls /proc | grep -E '^[0-9]+$'); do r=$(readlink /proc/$p/root 2>/dev/null); [ \"$r\" = {CH[ip]} ] && kill -9 $p; done")
    time.sleep(2)
    for ip in IPS:
        sh(f"umount {CH[ip]}/proc {CH[ip]}/dev 2>/dev/null; ip netns del {NS[ip]} 2>/dev/null; ip link del {VETH[ip]} 2>/dev/null")
    sh("ip link del br-ha 2>/dev/null")


def main():
    teardown(); shutil.rmtree(T, ignore_errors=True); T.mkdir(parents=True); sh("pkill -9 sshd; true")
    sh(f"ssh-keygen -q -t ed25519 -N '' -f {T}/key", check=True); pub = (T / "key.pub").read_text()
    print("=== 0. real network: bridge + 3 namespaces (10.77.0.11-13), chroot per node, sshd per node")
    sh("ip link add br-ha type bridge && ip addr add 10.77.0.1/24 dev br-ha && ip link set br-ha up", check=True)
    for i, ip in enumerate(IPS, 1):
        d, ns, v = CH[ip], NS[ip], VETH[ip]
        sh(f"ip netns add {ns} && ip link add {v} type veth peer name eth0-{i} && ip link set {v} master br-ha up && "
           f"ip link set eth0-{i} netns {ns} && ip netns exec {ns} ip link set eth0-{i} name eth0 && "
           f"ip netns exec {ns} ip addr add {ip}/24 dev eth0 && ip netns exec {ns} ip link set eth0 up && ip netns exec {ns} ip link set lo up", check=True)
        if not os.path.isdir(d + "/etc"): sh(f"cp -a {BASE} {d}", check=True)
        sh(f"mountpoint -q {d}/proc || mount -t proc proc {d}/proc; mountpoint -q {d}/dev || mount --bind /dev {d}/dev")
        sh(f"rm -rf {d}/var/lib/fakesd {d}/etc/dbha {d}/var/lib/dbha {d}/usr/local/lib/dbha {d}/etc/mysql/mysql.conf.d/zz-airgap-ha.cnf "
           f"{d}/etc/keepalived/keepalived.conf {d}/etc/systemd/system/dbha-agent.service {d}/etc/systemd/system/mysql.service.d {d}/var/lib/mysql/* {d}/var/run/mysqld/*")
        os.makedirs(f"{d}/root/.ssh", exist_ok=True); open(f"{d}/root/.ssh/authorized_keys", "w").write(pub)
        os.chmod(f"{d}/root/.ssh", 0o700); os.chmod(f"{d}/root/.ssh/authorized_keys", 0o600)
        sh(f"mkdir -p {d}/run/sshd {d}/run/systemd/system {d}/etc/systemd/system {d}/etc/keepalived; chroot {d} ssh-keygen -A")
        sh(f"cp {ROOT}/tests/integration/chroot-systemctl.py {d}/usr/bin/systemctl; chmod +x {d}/usr/bin/systemctl; printf '#!/bin/sh\\nexit 101\\n' > {d}/usr/sbin/policy-rc.d; chmod +x {d}/usr/sbin/policy-rc.d")
        open(f"{d}/etc/mysql/mysql.conf.d/zz-test.cnf", "w").write("[mysqld]\nperformance_schema=OFF\ninnodb_buffer_pool_size=48M\nskip_name_resolve=ON\n")
        subprocess.Popen(f"ip netns exec {ns} chroot {d} /usr/sbin/sshd -o UsePAM=no -o PermitRootLogin=prohibit-password", shell=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    for ip in IPS:
        r = sh(f"ssh -i {T}/key -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=5 root@{ip} 'grep -c 24.04 /etc/os-release'")
        ok(f"node {ip}: reachable over real SSH through the bridge", r.stdout.strip() not in ("", "0"), r.stderr)

    inv = {"all": {"vars": {"ansible_user": "root", "ansible_ssh_private_key_file": f"{T}/key", "ansible_python_interpreter": "/usr/bin/python3"},
                   "hosts": {ip: {"ansible_host": ip, "node_hostname": f"db{i}", "db_server_id": i} for i, ip in enumerate(IPS, 1)},
                   "children": {"cs_db": {"hosts": {ip: {} for ip in IPS}}}}}
    (T / "inv.json").write_text(json.dumps(inv))
    (T / "vars.json").write_text(json.dumps({"db_vip": VIP, "db_vip_prefix": 24, "db_vrrp_iface": "", "db_vrrp_id": 51, "db_vrrp_pass": "abcd1234",
        "db_dbha_password": DBHA_PW, "db_acs_admin_password": ACS_PW, "db_allow_single_survivor": False, "db_client_ips": ["10.77.0.1"], "master_ip": "10.77.0.1"}))

    def play():
        env = dict(os.environ, ANSIBLE_CONFIG=str(ROOT / "ansible/ansible.cfg"), ANSIBLE_ROLES_PATH=str(ROOT / "ansible/roles"), ANSIBLE_NOCOLOR="1")
        r = subprocess.run(["ansible-playbook", "-i", str(T / "inv.json"), str(ROOT / "ansible/10-cloudstack-db.yml"), "-e", f"@{T}/vars.json"],
                           cwd=str(ROOT / "ansible"), env=env, capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    print("\n=== 1. deploy with the real playbook (VIP interface auto-detected)")
    t0 = time.time(); rc, log = play(); (T / "run1.log").write_text(log)
    ok(f"playbook succeeded in {time.time()-t0:.0f}s (failed=0 x3)", rc == 0 and log.count("failed=0") == 3, log[-3000:])
    prim = wait_for(one_primary, 60, "one primary + 2 replicas")
    ok(f"primary={prim}, 2 replicas streaming", prim == IPS[0])
    h = wait_for(lambda: vip_holders() == [prim] and vip_holders(), 30, "VIP on the primary")
    ok(f"REAL VRRP: VIP {VIP} is configured on the primary {prim} only", h == [prim])
    ok("write through the VIP lands on the primary", (q(VIP, "SELECT @@report_host h")[0]["h"] == prim))
    q(VIP, "CREATE DATABASE vip_test"); q(VIP, "CREATE TABLE vip_test.t (id INT PRIMARY KEY, v VARCHAR(30))"); q(VIP, "INSERT INTO vip_test.t VALUES (1,'via-vip')")
    for ip in IPS: wait_for(lambda ip=ip: csh(ip, "mysql -uroot -N -e 'SELECT v FROM vip_test.t'").stdout.strip() == "via-vip", 30, f"row on {ip}")
    ok("row written through the VIP is on all 3 nodes", True)

    print("\n=== 2. primary mysqld stops -> new primary AND the VIP follows")
    old = prim; csh(old, "systemctl stop mysql")
    newp = wait_for(lambda: one_primary(skip=(old,)), 120, "new primary", 2)
    wait_for(lambda: vip_holders(skip=()) == [newp], 60, "VIP moved to the new primary", 2)
    ok(f"{old} down -> {newp} promoted and VIP moved to it", True)
    q(VIP, "INSERT INTO vip_test.t VALUES (2,'after-failover')")
    ok("writes through the same VIP keep working after failover", q(VIP, "SELECT @@report_host h")[0]["h"] == newp)
    csh(old, "systemctl start mysql")
    wait_for(lambda: one_primary() == newp, 120, "old primary rejoined", 2)
    wait_for(lambda: csh(old, "mysql -uroot -N -e 'SELECT COUNT(*) FROM vip_test.t'").stdout.strip() == "2", 60, "rows on old primary")
    ok("old primary rejoined as replica with the new data", vip_holders() == [newp])

    print("\n=== 3. REAL network partition: cut the primary's link")
    p = one_primary(); others = [ip for ip in IPS if ip != p]
    sh(f"ip link set {VETH[p]} down")
    wait_for(lambda: (csh(p, "mysql -uroot -N -e 'SELECT @@super_read_only'").stdout.strip() == "1"), 40, "isolated primary fences itself", 2)
    ok(f"isolated primary {p} fenced itself (super_read_only=1) - no split brain", True)
    np_ = wait_for(lambda: one_primary(skip=(p,)), 90, "majority elects a new primary", 2)
    ok(f"majority side elected {np_} while {p} was cut off", np_ in others)
    q(np_, "INSERT INTO vip_test.t VALUES (3,'during-partition')")
    sh(f"ip link set {VETH[p]} up")
    wait_for(lambda: one_primary() == np_, 120, "cut node rejoined as replica", 2)
    wait_for(lambda: csh(p, "mysql -uroot -N -e 'SELECT COUNT(*) FROM vip_test.t'").stdout.strip() == "3", 60, "cut node has the partition-time row")
    h = wait_for(lambda: vip_holders() == [np_] and vip_holders(), 40, "exactly one VIP holder after healing", 2)
    ok(f"link restored: {p} is a replica again with all data; VIP held only by {np_}", h == [np_])

    print("\n=== 4. second playbook run on the live cluster")
    rc, log = play(); (T / "run2.log").write_text(log)
    ok("re-run succeeded (failed=0) and the primary is unchanged", rc == 0 and log.count("failed=0") == 3 and one_primary() == np_, log[-2500:])
    print(f"\nALL {n_pass} CHECKS PASSED"); return 0


if __name__ == "__main__":
    code = 1
    try: code = main()
    except SystemExit as e: print(e)
    except BaseException:
        import traceback; traceback.print_exc()
    finally: teardown()
    sys.exit(code)
