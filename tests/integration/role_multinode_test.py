#!/usr/bin/env python3
"""
REAL test of the Ansible role `mysql_ha` on THREE separate Ubuntu 24.04 chroots (own filesystem, own sshd on
127.0.0.N:22, real mysqld 8.0 bound to 127.0.0.N:3306, real dbha-agent). Ansible reaches them over real SSH.
systemd is replaced by tests/integration/chroot-systemctl.py (it really starts mysql + the agent). The VIP itself
(keepalived/VRRP) cannot be exercised here; its config is validated with `keepalived -t`.
Needs root + /tmp/chroot-db-base (see make-db-base steps in the README test section).
"""
import json, os, pathlib, shutil, subprocess, sys, time, urllib.request
import pymysql

ROOT = pathlib.Path(__file__).resolve().parents[2]
T = pathlib.Path("/tmp/role-test"); BASE = "/tmp/chroot-db-base"
IPS = ["127.0.0.1", "127.0.0.2", "127.0.0.3"]; CH = {ip: f"/tmp/chroot-db{i}" for i, ip in enumerate(IPS, 1)}
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


def wait_for(fn, timeout, what):
    end = time.time() + timeout; last = None
    while time.time() < end:
        try:
            r = fn()
            if r: return r
        except Exception as e: last = e
        time.sleep(2)
    print("--- cluster view:", json.dumps({k: (v and {x: v[x] for x in ("role", "writable", "mysql_up", "replication_ok", "peers_up", "action")}) for k, v in cluster().items()}))
    for ip in IPS:
        print(f"--- {ip} mysqld procs:", csh(ip, "pgrep -a mysqld | cut -c1-80").stdout.strip().replace("\n", " | "))
    raise SystemExit(f"timeout waiting for {what} {last!r}")


def status(ip):
    with urllib.request.urlopen(f"http://{ip}:8686/status", timeout=3) as r: return json.load(r)


def cluster():
    st = {}
    for ip in IPS:
        try: st[ip] = status(ip)
        except Exception: st[ip] = None
    return st


def one_primary(skip=()):
    st = cluster(); prim = [ip for ip, s in st.items() if s and s["role"] == "primary" and ip not in skip]
    if len(prim) != 1: return None
    for ip, s in st.items():
        if ip == prim[0] or ip in skip: continue
        if not s or s["role"] != "replica" or not s["replication_ok"]: return None
    return prim[0]


def q(ip, sql, user="acsadmin", pw=ACS_PW):
    c = pymysql.connect(host=ip, port=3306, user=user, password=pw, connect_timeout=5, autocommit=True, cursorclass=pymysql.cursors.DictCursor)
    try:
        with c.cursor() as cur: cur.execute(sql); return cur.fetchall()
    finally: c.close()


def csh(ip, cmd): return sh(f"chroot {CH[ip]} /bin/bash -c {json.dumps(cmd)}")


def teardown():
    for ip in IPS:
        d = CH[ip]
        sh(f"for p in $(ls /proc | grep -E '^[0-9]+$'); do r=$(readlink /proc/$p/root 2>/dev/null); [ \"$r\" = {d} ] && kill -9 $p; done")
    time.sleep(2)
    for ip in IPS: sh(f"umount {CH[ip]}/proc {CH[ip]}/dev 2>/dev/null")


def main():
    assert os.path.isdir(BASE + "/etc"), "build the base chroot first"
    teardown(); shutil.rmtree(T, ignore_errors=True); T.mkdir(parents=True)
    sh("pkill -9 sshd; true")
    sh(f"ssh-keygen -q -t ed25519 -N '' -f {T}/key", check=True)
    pub = (T / "key.pub").read_text()
    print("=== 0. prepare 3 chroots (own fs, own IP, sshd + mysql installed)")
    for i, ip in enumerate(IPS, 1):
        d = CH[ip]
        if not os.path.isdir(d + "/etc"): sh(f"cp -a {BASE} {d}", check=True)
        sh(f"mountpoint -q {d}/proc || mount -t proc proc {d}/proc; mountpoint -q {d}/dev || mount --bind /dev {d}/dev")
        sh(f"rm -rf {d}/var/lib/fakesd {d}/etc/dbha {d}/var/lib/dbha {d}/usr/local/lib/dbha {d}/etc/mysql/mysql.conf.d/zz-airgap-ha.cnf {d}/etc/keepalived/keepalived.conf {d}/etc/systemd/system/dbha-agent.service {d}/etc/systemd/system/mysql.service.d; rm -rf {d}/var/lib/mysql/* ")
        os.makedirs(f"{d}/root/.ssh", exist_ok=True); open(f"{d}/root/.ssh/authorized_keys", "w").write(pub)
        os.chmod(f"{d}/root/.ssh", 0o700); os.chmod(f"{d}/root/.ssh/authorized_keys", 0o600)
        sh(f"mkdir -p {d}/run/sshd {d}/run/systemd/system {d}/etc/systemd/system {d}/etc/keepalived; chroot {d} ssh-keygen -A")
        sh(f"cp {ROOT}/tests/integration/chroot-systemctl.py {d}/usr/bin/systemctl; chmod +x {d}/usr/bin/systemctl")
        sh(f"printf '#!/bin/sh\\nexit 101\\n' > {d}/usr/sbin/policy-rc.d; chmod +x {d}/usr/sbin/policy-rc.d")
        open(f"{d}/etc/mysql/mysql.conf.d/zz-test.cnf", "w").write(
            f"[mysqld]\nbind-address={ip}\nmysqlx=OFF\nperformance_schema=OFF\ninnodb_buffer_pool_size=48M\nskip_name_resolve=ON\n")
        subprocess.Popen(f"chroot {d} /usr/sbin/sshd -o ListenAddress={ip} -o UsePAM=no -o PermitRootLogin=prohibit-password",
                         shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    for ip in IPS:
        r = sh(f"ssh -i {T}/key -o StrictHostKeyChecking=no -o BatchMode=yes root@{ip} 'grep -c 24.04 /etc/os-release'")
        ok(f"sshd up on {ip}:22 (Ubuntu 24.04 chroot)", r.stdout.strip() not in ("", "0"), r.stderr)

    inv = {"all": {"vars": {"ansible_user": "root", "ansible_ssh_private_key_file": f"{T}/key", "ansible_python_interpreter": "/usr/bin/python3"},
                   "hosts": {ip: {"ansible_host": ip, "node_hostname": f"db{i}", "db_server_id": i} for i, ip in enumerate(IPS, 1)},
                   "children": {"cs_db": {"hosts": {ip: {} for ip in IPS}}}}}
    (T / "inv.json").write_text(json.dumps(inv))
    (T / "vars.json").write_text(json.dumps({"db_vip": "127.0.0.100", "db_vip_prefix": 32, "db_vrrp_iface": "lo", "db_vrrp_id": 51,
        "db_vrrp_pass": "abcd1234", "db_dbha_password": DBHA_PW, "db_acs_admin_password": ACS_PW, "db_allow_single_survivor": False,
        "db_client_ips": [], "master_ip": "127.0.0.1"}))

    def play():
        env = dict(os.environ, ANSIBLE_CONFIG=str(ROOT / "ansible/ansible.cfg"), ANSIBLE_ROLES_PATH=str(ROOT / "ansible/roles"), ANSIBLE_NOCOLOR="1")
        r = subprocess.run(["ansible-playbook", "-i", str(T / "inv.json"), str(ROOT / "ansible/10-cloudstack-db.yml"), "-e", f"@{T}/vars.json"],
                           cwd=str(ROOT / "ansible"), env=env, capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    print("\n=== 1. run the real playbook 10-cloudstack-db.yml against the 3 nodes")
    t0 = time.time(); rc, log = play()
    open(T / "run1.log", "w").write(log)
    ok(f"playbook run 1 succeeded in {time.time()-t0:.0f}s (failed=0 on all hosts)", rc == 0 and log.count("failed=0") == 3, log[-3000:])
    prim = one_primary()
    ok(f"exactly one primary ({prim}) and two streaming replicas", prim == "127.0.0.1")
    ok("acsadmin account works through TCP on the primary", q(prim, "SELECT 1 x")[0]["x"] == 1)
    q(prim, "CREATE DATABASE role_test"); q(prim, "CREATE TABLE role_test.t (id INT PRIMARY KEY, v VARCHAR(20))"); q(prim, "INSERT INTO role_test.t VALUES (1,'before')")
    for ip in IPS:
        wait_for(lambda ip=ip: csh(ip, "mysql -uroot -N -e 'SELECT v FROM role_test.t'").stdout.strip() == "before", 30, f"row on {ip}")
    ok("data written on the primary is on all 3 nodes", True)
    for ip in IPS:
        k = csh(ip, "keepalived -t -f /etc/keepalived/keepalived.conf 2>&1; echo rc=$?")
        ok(f"keepalived config valid on {ip}", "rc=0" in k.stdout, k.stdout)
        m = csh(ip, "stat -c %a /etc/dbha/config.json /etc/keepalived/keepalived.conf").stdout.split()
        ok(f"secrets files are mode 600 on {ip}", m == ["600", "600"], m)
        ro = csh(ip, "mysql -uroot -N -e 'SELECT @@gtid_mode, @@log_bin, @@binlog_format'").stdout.split()
        ok(f"gtid_mode=ON, log_bin on, ROW format on {ip}", ro == ["ON", "1", "ROW"], ro)

    print("\n=== 2. failover through the deployed agents")
    old = prim; csh(old, "systemctl stop mysql")
    newp = wait_for(lambda: one_primary(skip=(old,)), 120, "new primary")
    ok(f"primary {old} stopped -> {newp} promoted", newp != old)
    q(newp, "INSERT INTO role_test.t VALUES (2,'after-failover')")
    csh(old, "systemctl start mysql")
    wait_for(lambda: one_primary() == newp, 120, "old primary rejoined as replica")
    wait_for(lambda: csh(old, "mysql -uroot -N -e 'SELECT COUNT(*) FROM role_test.t'").stdout.strip() == "2", 60, "old primary has new row")
    ok("old primary rejoined as replica and has the row written during its absence", True)

    print("\n=== 3. re-running the playbook on the live cluster is safe")
    rc, log = play(); open(T / "run2.log", "w").write(log)
    ok("playbook run 2 succeeded (failed=0) and the cluster kept its primary", rc == 0 and log.count("failed=0") == 3 and one_primary() == newp, log[-2500:])
    print(f"\nALL {n_pass} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    code = 1
    try: code = main()
    except SystemExit as e: print(e)
    except BaseException:
        import traceback; traceback.print_exc()
    finally:
        teardown()
    sys.exit(code)
