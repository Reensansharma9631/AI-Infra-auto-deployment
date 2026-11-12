#!/usr/bin/env python3
"""
REAL integration test of the MySQL HA set: 3 real mysqld 8.0 instances (127.0.0.1/2/3:3306) + 3 real dbha agents,
using the real Ansible template for my.cnf and the real init SQL. Needs: root, mysql-server installed (not started),
python3-pymysql, jinja2.   Run:  sudo python3 tests/integration/dbha_cluster_test.py
A bash loop plays the role of systemd (restart on exit, MYSQLD_PARENT_PID set like the Ubuntu unit does).
"""
import json, os, pathlib, signal, subprocess, sys, time, urllib.request
import jinja2
import pymysql

ROOT = pathlib.Path(__file__).resolve().parents[2]
ROLE = ROOT / "ansible/roles/mysql_ha"
BASE = pathlib.Path(os.environ.get("DBHA_TEST_DIR", "/tmp/dbha-test"))
IPS = ["127.0.0.1", "127.0.0.2", "127.0.0.3"]
PW = "TestPassw0rdTestPassw0rd"
HTTP = 8686
results = []


def sh(cmd, check=True, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str), check=check, capture_output=True, text=True, **kw)


def wait_for(fn, timeout, what):
    end, last = time.time() + timeout, None
    while time.time() < end:
        try:
            r = fn()
            if r:
                return r
        except Exception as exc:
            last = exc
        time.sleep(0.5)
    raise AssertionError(f"timeout ({timeout}s) waiting for: {what} {('- last error: %r' % last) if last else ''}")


def step(name):
    print(f"\n=== {name}", flush=True)
    return time.time()


def ok(name, t0):
    results.append(name)
    print(f"    PASS ({time.time()-t0:.0f}s) {name}", flush=True)


class Node:
    def __init__(self, i, ip):
        self.i, self.ip = i, ip
        self.dir = BASE / f"n{i}"
        self.sock = str(self.dir / "mysqld.sock")
        self.agent = None

    # ---- mysqld lifecycle
    def prepare(self):
        d = self.dir
        (d / "data").mkdir(parents=True, exist_ok=True)
        sh(f"chown -R mysql:mysql {d}")
        (d / "min.cnf").write_text(f"[mysqld]\ndatadir={d}/data\nsocket={self.sock}\nmysqlx=OFF\ninnodb_buffer_pool_size=32M\nperformance_schema=OFF\n")
        sh(f"mysqld --defaults-file={d}/min.cnf --initialize-insecure --user=mysql > {d}/init.log 2>&1")  # like the .deb postinst
        tpl = (ROLE / "templates/zz-airgap-ha.cnf.j2").read_text()
        rendered = jinja2.Template(tpl).render(db_server_id=self.i, ansible_host=self.ip)
        (d / "my.cnf").write_text(rendered + f"""
# ---- test-only overrides (paths/ports of this sandbox)
datadir={d}/data
socket={self.sock}
pid-file={d}/mysqld.pid
log-error={d}/error.log
bind-address={self.ip}
port=3306
innodb_buffer_pool_size=32M
performance_schema=OFF
skip_name_resolve=ON
""")
        ppid = "$$" if not os.environ.get("DBHA_TEST_UNMANAGED") else "999999"
        (d / "supervise.sh").write_text(f"""#!/bin/bash
# emulates systemd: Restart=on-failure; MYSQLD_PARENT_PID must equal mysqld's parent pid (systemd: 1, here: this shell)
export MYSQLD_PARENT_PID={ppid}
while true; do
  mysqld --defaults-file={d}/my.cnf --user=mysql >> {d}/stdout.log 2>&1
  rc=$?
  if [ $rc -eq 0 ]; then            # clean exit = `systemctl stop`: stay down until kicked (systemctl start/restart)
    while [ ! -f {d}/kick ]; do sleep 0.3; done
    rm -f {d}/kick
  else
    sleep 1                         # crash / exit 16 -> restart (RestartSec)
  fi
done
""")
        sh(f"chown -R mysql:mysql {d}; chmod +x {d}/supervise.sh")
        sh(f"setsid {d}/supervise.sh > /dev/null 2>&1 &", check=False)
        wait_for(lambda: self.sql("SELECT 1"), 90, f"mysqld {self.ip} up")

    def pid(self):
        return int((self.dir / "mysqld.pid").read_text().strip())

    def sql(self, q, args=None, fetch=True):
        c = pymysql.connect(unix_socket=self.sock, user="root", autocommit=True, connect_timeout=3,
                            read_timeout=60, cursorclass=pymysql.cursors.DictCursor)
        try:
            with c.cursor() as cur:
                cur.execute(q, args)
                return cur.fetchall() if fetch else True
        finally:
            c.close()

    def stop_mysql(self):               # like `systemctl stop mysql`
        try:
            os.kill(self.pid(), signal.SIGTERM)
        except Exception:
            pass
        wait_for(lambda: not self.is_up(), 60, f"{self.ip} down")

    def start_mysql(self):              # like `systemctl start mysql`
        (self.dir / "kick").touch()
        wait_for(self.is_up, 90, f"{self.ip} up")

    def crash(self):                    # kill -9: the supervisor (systemd) restarts it
        os.kill(self.pid(), signal.SIGKILL)

    def freeze(self):
        os.kill(self.pid(), signal.SIGSTOP)

    def thaw(self):
        os.kill(self.pid(), signal.SIGCONT)

    def is_up(self):
        try:
            self.sql("SELECT 1"); return True
        except Exception:
            return False

    # ---- agent
    def start_agent(self):
        cfg = {"self_ip": self.ip, "nodes": [{"ip": ip, "server_id": n} for n, ip in enumerate(IPS, 1)],
               "mysql_port": 3306, "http_port": HTTP, "http_bind": self.ip, "user": "dbha", "password": PW,
               "interval": 1, "fail_grace": 6, "fence_after": 3, "probe_timeout": 2, "min_clone_interval": 8,
               "state_dir": str(self.dir / "state"), "socket": self.sock, "restart_cmd": ["touch", str(self.dir / "kick")], "failed_cmd": ["false"],
               "log_level": "INFO", "allow_single_survivor": False}
        (self.dir / "agent.json").write_text(json.dumps(cfg))
        log = open(self.dir / "agent.log", "a")
        self.agent = subprocess.Popen([sys.executable, str(ROLE / "files/dbha_agent.py"), str(self.dir / "agent.json")],
                                      stdout=log, stderr=log)

    def stop_agent(self):
        if self.agent:
            self.agent.terminate(); self.agent.wait(10); self.agent = None

    def status(self):
        with urllib.request.urlopen(f"http://{self.ip}:{HTTP}/status", timeout=2) as r:
            return json.load(r)

    def agent_log(self):
        return (self.dir / "agent.log").read_text()


def cleanup():
    sh("pkill -f 'dbha-test/n./supervise.sh'", check=False)
    sh("pkill -9 -f 'dbha-test/n./my.cnf'", check=False)
    sh("pkill -f 'roles/mysql_ha/files/dbha_agent.py'", check=False)
    time.sleep(2)


def roles(nodes):
    out = {}
    for n in nodes:
        try:
            out[n.ip] = n.status()
        except Exception:
            out[n.ip] = None
    return out


def primaries(nodes):
    return [ip for ip, s in roles(nodes).items() if s and s["role"] == "primary"]


def stable(nodes, want_primary=None, replicas_ok=True, skip=()):
    r = roles(nodes)
    prim = [ip for ip, s in r.items() if s and s["role"] == "primary"]
    if len(prim) != 1 or (want_primary and prim[0] != want_primary):
        return None
    for ip, s in r.items():
        if ip == prim[0] or ip in skip:
            continue
        if not s or s["role"] != "replica" or (replicas_ok and not s["replication_ok"]):
            return None
    return prim[0]


def rowcount(n, tbl="t.rows"):
    return n.sql(f"SELECT COUNT(*) c FROM {tbl}")[0]["c"]


def main():
    cleanup()
    sh(f"rm -rf {BASE}"); BASE.mkdir(parents=True)
    nodes = [Node(i, ip) for i, ip in enumerate(IPS, 1)]
    n1, n2, n3 = nodes
    try:
        t = step("0. start 3 real mysqld (config rendered from the Ansible template) and run the real init SQL")
        for n in nodes:
            n.prepare()
        init_sql = jinja2.Template((ROLE / "templates/init-node.sql.j2").read_text()).render(
            db_ips=IPS, db_dbha_password=PW)
        for n in nodes:
            r = sh(f"mysql -uroot -S {n.sock}", input=init_sql) if False else subprocess.run(
                ["mysql", "-uroot", "-S", n.sock], input=init_sql, text=True, capture_output=True)
            assert r.returncode == 0, r.stderr
            v = n.sql("SELECT @@read_only ro, @@super_read_only sro, @@gtid_mode g, @@rpl_semi_sync_replica_enabled sr")[0]
            assert v["ro"] == 1 and v["sro"] == 1 and v["g"] == "ON", v
            assert n.sql("SELECT PLUGIN_NAME FROM information_schema.PLUGINS WHERE PLUGIN_NAME IN ('clone','rpl_semi_sync_source','rpl_semi_sync_replica')")
        ok("all nodes: read-only, GTID ON, clone + semi-sync plugins loaded", t)

        t = step("1. bootstrap: agents elect exactly one primary (lowest server_id), others stream")
        for n in nodes:
            n.start_agent()
        prim = wait_for(lambda: stable(nodes), 60, "one primary + 2 streaming replicas")
        assert prim == n1.ip, f"expected {n1.ip} to win the bootstrap election, got {prim}"
        ok(f"primary={prim}, 2 replicas replicating (epoch {n1.status()['epoch']})", t)

        t = step("2. writes on the primary reach every replica; replicas refuse writes")
        n1.sql("CREATE DATABASE t; CREATE TABLE t.rows (id INT AUTO_INCREMENT PRIMARY KEY, v VARCHAR(64))", fetch=False) if False else None
        n1.sql("CREATE DATABASE t", fetch=False)
        n1.sql("CREATE TABLE t.rows (id INT AUTO_INCREMENT PRIMARY KEY, v VARCHAR(64))", fetch=False)
        for _ in range(20):
            n1.sql("INSERT INTO t.rows (v) SELECT UUID() FROM information_schema.COLUMNS LIMIT 100", fetch=False)
        total = rowcount(n1)
        wait_for(lambda: rowcount(n2) == total and rowcount(n3) == total, 30, "replicas caught up")
        try:
            n2.sql("INSERT INTO t.rows (v) VALUES ('x')", fetch=False); raise SystemExit("replica accepted a write!")
        except pymysql.err.OperationalError as exc:
            assert exc.args[0] == 1290, exc
        assert n1.sql("SELECT @@rpl_semi_sync_source_enabled s")[0]["s"] == 1
        ok(f"{total} rows on all 3 nodes; replica rejects writes (error 1290); semi-sync source ON", t)

        t = step("3. replica down for a while -> catches up by GTID (no clone, no dump)")
        n3.stop_mysql()
        for _ in range(10):
            n1.sql("INSERT INTO t.rows (v) SELECT UUID() FROM information_schema.COLUMNS LIMIT 100", fetch=False)
        total = rowcount(n1)
        n3.start_mysql()
        wait_for(lambda: rowcount(n3) == total, 60, "n3 caught up")
        wait_for(lambda: stable(nodes), 30, "cluster stable")
        assert "CLONE from" not in n3.agent_log(), "should not have cloned for a simple outage"
        ok(f"n3 rejoined, {total} rows, no CLONE used", t)

        t = step("4. replication manually broken on a replica -> re-attached automatically")
        n2.sql("STOP REPLICA", fetch=False); n2.sql("RESET REPLICA ALL", fetch=False)
        wait_for(lambda: n2.status()["replication_ok"], 30, "n2 re-attached")
        ok("n2 re-attached after RESET REPLICA ALL", t)

        t = step("5. errant transaction on a replica -> automatic CLONE (physical copy, no dump)")
        n2.sql("SET GLOBAL super_read_only=OFF", fetch=False)
        n2.sql("CREATE DATABASE errant_db", fetch=False)
        n2.sql("SET GLOBAL super_read_only=ON", fetch=False)
        wait_for(lambda: "CLONE from" in n2.agent_log(), 40, "agent starts a clone")
        wait_for(lambda: n2.is_up() and not [d for d in n2.sql("SHOW DATABASES") if d["Database"] == "errant_db"], 120,
                 "errant database gone after clone")
        total = rowcount(n1)
        wait_for(lambda: rowcount(n2) == total and stable(nodes), 90, "n2 rebuilt and replicating")
        ok("n2 cloned from primary, errant_db removed, replicating again", t)

        t = step("6. binlogs purged while a replica is away -> automatic CLONE")
        n3.stop_mysql()
        for _ in range(5):
            n1.sql("INSERT INTO t.rows (v) SELECT UUID() FROM information_schema.COLUMNS LIMIT 100", fetch=False)
        n1.sql("FLUSH BINARY LOGS", fetch=False)
        n1.sql("SET GLOBAL binlog_expire_logs_seconds=1", fetch=False); time.sleep(2)
        n1.sql("FLUSH BINARY LOGS", fetch=False); n1.sql("PURGE BINARY LOGS BEFORE NOW()", fetch=False)
        n1.sql("SET GLOBAL binlog_expire_logs_seconds=604800", fetch=False)
        before = n3.agent_log().count("CLONE from")
        time.sleep(9)                                  # respect min_clone_interval
        n3.start_mysql()
        wait_for(lambda: n3.agent_log().count("CLONE from") > before, 90, "agent clones n3")
        total = rowcount(n1)
        wait_for(lambda: n3.is_up() and rowcount(n3) == total and stable(nodes), 120, "n3 rebuilt")
        ok("n3 rebuilt by CLONE after its binlog position was purged", t)

        t = step("7. primary dies -> most up-to-date replica promoted, writes continue, old primary rejoins as replica")
        old_epoch = n1.status()["epoch"]
        n1.stop_mysql()
        newp = wait_for(lambda: stable([n2, n3]), 60, "new primary elected among n2/n3")
        assert newp in (n2.ip, n3.ip)
        newn = n2 if newp == n2.ip else n3
        assert newn.status()["epoch"] > old_epoch, "epoch must increase"
        newn.sql("INSERT INTO t.rows (v) VALUES ('after-failover')", fetch=False)
        n1.start_mysql()
        wait_for(lambda: stable(nodes) == newp, 60, "old primary is a streaming replica")
        wait_for(lambda: rowcount(n1) == rowcount(newn), 30, "old primary has the new rows")
        assert "after-failover" in [r["v"] for r in n1.sql("SELECT v FROM t.rows ORDER BY id DESC LIMIT 1")]
        ok(f"failover to {newp} (epoch {old_epoch}->{newn.status()['epoch']}); n1 back as replica with all data", t)

        t = step("8. kill -9 of the primary (crash, supervisor restarts it) -> still exactly one primary, no data loss")
        cur = {n.ip: n for n in nodes}[stable(nodes)]
        total_before = rowcount(cur)
        cur.crash()
        time.sleep(1)
        wait_for(lambda: stable(nodes), 90, "cluster healed after crash")
        p2 = {n.ip: n for n in nodes}[stable(nodes)]
        assert rowcount(p2) >= total_before
        for n in nodes:
            wait_for(lambda n=n: rowcount(n) == rowcount(p2), 30, f"{n.ip} converged")
        ok(f"after crash primary={p2.ip}, all nodes converged on {rowcount(p2)} rows", t)

        t = step("9. frozen primary (like a network stall) -> replacement elected; when it wakes it is demoted, not split-brain")
        p = {n.ip: n for n in nodes}[stable(nodes)]
        others = [n for n in nodes if n is not p]
        p.freeze()
        newp = wait_for(lambda: stable(others), 60, "new primary among the other two")
        assert newp != p.ip
        p.thaw()
        wait_for(lambda: stable(nodes) == newp, 60, "unfrozen old primary demoted and following")
        w = [n.ip for n in nodes if n.sql("SELECT @@super_read_only s")[0]["s"] == 0]
        assert w == [newp], f"writable nodes: {w}"
        ok(f"{p.ip} frozen -> {newp} took over; old primary demoted, exactly 1 writable node", t)

        t = step("10. primary without quorum fences itself (super_read_only) before anyone else could elect")
        p = {n.ip: n for n in nodes}[stable(nodes)]
        others = [n for n in nodes if n is not p]
        for o in others:
            o.freeze()
        wait_for(lambda: p.sql("SELECT @@super_read_only s")[0]["s"] == 1, 30, "primary fenced")
        assert p.status()["role"] in ("fenced", "replica")
        for o in others:
            o.thaw()
        wait_for(lambda: stable(nodes), 90, "cluster re-forms after the 'partition' heals")
        ok("isolated primary fenced itself; cluster re-formed with exactly one primary", t)

        t = step("11. final consistency check on all nodes")
        prim = {n.ip: n for n in nodes}[stable(nodes)]
        chk = prim.sql("CHECKSUM TABLE t.rows")[0]["Checksum"]
        for n in nodes:
            wait_for(lambda n=n: n.sql("CHECKSUM TABLE t.rows")[0]["Checksum"] == chk, 30, f"{n.ip} checksum")
        gt = {n.ip: n.sql("SELECT @@gtid_executed g")[0]["g"].replace("\n", "") for n in nodes}
        assert len(set(gt.values())) == 1, gt
        ok("identical table checksum and identical gtid_executed on all 3 nodes", t)
        print(f"\nALL {len(results)} SCENARIOS PASSED")
        return 0
    except BaseException as exc:
        import traceback; traceback.print_exc()
        print(f"\nFAILED after {len(results)} passing scenarios: {exc}")
        for n in nodes:
            print(f"--- tail agent.log {n.ip}"); print("\n".join(n.agent_log().splitlines()[-25:]) if (n.dir / 'agent.log').exists() else '')
        return 1
    finally:
        for n in nodes:
            n.stop_agent()
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
