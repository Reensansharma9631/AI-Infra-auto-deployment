#!/usr/bin/env python3
"""
dbha-agent - self-healing, single-writer MySQL 8 GTID replication manager.

One agent runs next to every mysqld of a 2-3 node set. It never needs a dump:

  * replica stopped / lagging / reconnecting      -> START REPLICA, auto-position by GTID
  * replica broken (purged binlog, dup key, ...)  -> CLONE INSTANCE FROM the primary (physical copy)
  * primary dies                                  -> most up-to-date node (quorum + grace) is promoted
  * old primary returns / split brain             -> demoted, errant GTIDs => re-cloned from the winner
  * primary isolated without quorum               -> fenced (super_read_only) BEFORE the others elect

HTTP (port from config):  /status (JSON)   /primary (200 only on the writable primary, used by keepalived)
"""
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

try:
    import pymysql
    import pymysql.cursors
except ImportError:  # unit tests only need the pure functions
    pymysql = None

log = logging.getLogger("dbha")

DEFAULTS = {
    "mysql_port": 3306, "http_port": 8686, "user": "dbha", "interval": 5,
    "fail_grace": 25, "fence_after": 12, "probe_timeout": 3, "allow_single_survivor": False,
    "min_clone_interval": 600, "max_start_retries": 3, "state_dir": "/var/lib/dbha",
    "socket": "/var/run/mysqld/mysqld.sock", "http_bind": "0.0.0.0",
    "restart_cmd": ["systemctl", "restart", "mysql"],
    "failed_cmd": ["systemctl", "is-failed", "--quiet", "mysql"], "post_clone_restart_window": 300,
}
# connection-level problems: mysqld keeps retrying by itself, never re-clone for these
TRANSIENT_ERRNOS = {0, 2002, 2003, 2006, 2013, 1158, 1159, 1129, 1130, 2026, 1053}


# --------------------------------------------------------------------------- pure helpers
def gtid_count(gtid_set):
    """Number of transactions in a GTID set string 'uuid:1-5:7,uuid2:1-3'."""
    total = 0
    for part in (gtid_set or "").replace("\n", "").split(","):
        part = part.strip()
        if not part:
            continue
        for seg in part.split(":")[1:]:
            lo, _, hi = seg.partition("-")
            total += (int(hi) - int(lo) + 1) if hi else 1
    return total


def decide(me, view, n_nodes, cfg, st, now):
    """
    Pure decision function (unit tested).
    view: ip -> {up, writable, gtid, server_id, epoch, primary_seen}
    st:   mutable dict {no_primary_since, no_quorum_since}
    returns (action, target_ip):  keep_primary | follow | demote_follow | fence | promote | wait
    """
    live = {ip: v for ip, v in view.items() if v.get("up")}
    quorum = len(live) > n_nodes // 2 or (cfg.get("allow_single_survivor") and len(live) >= 1)
    writers = [ip for ip, v in live.items() if v.get("writable")]

    if len(writers) >= 2:  # split brain: newest leadership epoch wins, then most data, then lowest id
        st["no_primary_since"] = None
        win = max(writers, key=lambda ip: (live[ip].get("epoch", 0), gtid_count(live[ip].get("gtid")),
                                           -live[ip].get("server_id", 0)))
        if me == win:
            return "keep_primary", me
        return ("demote_follow" if me in writers else "follow"), win

    if len(writers) == 1:
        st["no_primary_since"] = None
        w = writers[0]
        if w != me:
            st["no_quorum_since"] = None
            return "follow", w
        if quorum:
            st["no_quorum_since"] = None
            return "keep_primary", me
        if st.get("no_quorum_since") is None:
            st["no_quorum_since"] = now
        if now - st["no_quorum_since"] >= cfg["fence_after"]:
            return "fence", me
        return "keep_primary", me

    # nobody is writable
    st["no_quorum_since"] = None
    if me not in live or not quorum:
        st["no_primary_since"] = None
        return "wait", None
    if any(v.get("primary_seen") for ip, v in live.items() if ip != me):
        st["no_primary_since"] = None  # somebody else still sees a primary; it is just me who lost it
        return "wait", None
    if st.get("no_primary_since") is None:
        st["no_primary_since"] = now
        return "wait", None
    if now - st["no_primary_since"] < cfg["fail_grace"]:
        return "wait", None
    cand = max(live, key=lambda ip: (gtid_count(live[ip].get("gtid")), -live[ip].get("server_id", 0)))
    return ("promote", me) if cand == me else ("wait", cand)


# --------------------------------------------------------------------------- agent
class Agent:
    def __init__(self, cfg):
        self.cfg = {**DEFAULTS, **cfg}
        self.me = self.cfg["self_ip"]
        self.nodes = {n["ip"]: n for n in self.cfg["nodes"]}
        self.peers = [ip for ip in self.nodes if ip != self.me]
        self.state = {"no_primary_since": None, "no_quorum_since": None}
        self.retries = 0
        self.lock = threading.Lock()
        self.snapshot = {"role": "starting"}
        self.stop = threading.Event()
        self.last_service_kick = 0
        os.makedirs(self.cfg["state_dir"], exist_ok=True)

    # ---- small persistent state
    def _read(self, name, default=0):
        try:
            return type(default)(open(os.path.join(self.cfg["state_dir"], name)).read().strip())
        except Exception:
            return default

    def _write(self, name, value):
        tmp = os.path.join(self.cfg["state_dir"], name + ".tmp")
        with open(tmp, "w") as f:
            f.write(str(value))
        os.replace(tmp, os.path.join(self.cfg["state_dir"], name))

    # ---- connections
    def local(self):
        return pymysql.connect(unix_socket=self.cfg["socket"], user="root", autocommit=True,
                               connect_timeout=self.cfg["probe_timeout"], read_timeout=60,
                               cursorclass=pymysql.cursors.DictCursor)

    def remote(self, ip):
        return pymysql.connect(host=ip, port=self.cfg["mysql_port"], user=self.cfg["user"],
                               password=self.cfg["password"], autocommit=True,
                               connect_timeout=self.cfg["probe_timeout"],
                               read_timeout=self.cfg["probe_timeout"] * 2,
                               cursorclass=pymysql.cursors.DictCursor)

    @staticmethod
    def _probe_conn(conn):
        with conn.cursor() as c:
            c.execute("SELECT @@global.read_only ro, @@global.super_read_only sro, "
                      "@@global.gtid_executed g, @@global.server_id sid")
            r = c.fetchone()
            c.execute("SHOW REPLICA STATUS")
            rs = c.fetchone()
            try:
                c.execute("SELECT epoch FROM dbha.leader WHERE id=1")
                row = c.fetchone()
                table_epoch = int(row["epoch"]) if row else 0
            except Exception:
                table_epoch = 0
        repl = None
        if rs:
            repl = {"source_host": rs.get("Source_Host"), "io": rs.get("Replica_IO_Running"),
                    "sql": rs.get("Replica_SQL_Running"), "io_errno": int(rs.get("Last_IO_Errno") or 0),
                    "sql_errno": int(rs.get("Last_SQL_Errno") or 0),
                    "lag": rs.get("Seconds_Behind_Source"), "error": rs.get("Last_SQL_Error") or rs.get("Last_IO_Error")}
        return {"up": True, "writable": not (r["ro"] or r["sro"]), "gtid": (r["g"] or "").replace("\n", ""),
                "server_id": int(r["sid"]), "repl": repl, "table_epoch": table_epoch}

    def probe(self, ip):
        v = {"up": False, "writable": False, "gtid": "", "server_id": self.nodes[ip]["server_id"],
             "repl": None, "epoch": 0, "primary_seen": None}
        try:
            conn = self.local() if ip == self.me else self.remote(ip)
            try:
                v.update(self._probe_conn(conn))
            finally:
                conn.close()
        except Exception as exc:
            log.debug("probe %s failed: %s", ip, exc)
            return v
        if ip == self.me:
            v["epoch"] = max(self._read("epoch", 0), v.get("table_epoch", 0))
        else:
            try:
                with urlopen(f"http://{ip}:{self.cfg['http_port']}/status", timeout=self.cfg["probe_timeout"]) as r:
                    j = json.load(r)
                v["epoch"], v["primary_seen"] = max(int(j.get("epoch", 0)), v.get("table_epoch", 0)), j.get("primary_seen")
            except Exception:
                v["epoch"] = v.get("table_epoch", 0)  # agent not answering: use the replicated table value
        return v

    # ---- actions
    def sql(self, *stmts, ignore_errors=False):
        conn = self.local()
        try:
            with conn.cursor() as c:
                for s in stmts:
                    try:
                        c.execute(s)
                    except Exception as exc:
                        if not ignore_errors:
                            raise
                        log.debug("ignored error on %r: %s", s, exc)
        finally:
            conn.close()

    def gtid_subset(self, a, b):
        conn = self.local()
        try:
            with conn.cursor() as c:
                c.execute("SELECT GTID_SUBSET(%s, %s) ok", (a or "", b or ""))
                return bool(c.fetchone()["ok"])
        finally:
            conn.close()

    def promote(self, view):
        log.warning("PROMOTING this node to primary")
        conn = self.local()
        try:
            with conn.cursor() as c:
                c.execute("SHOW REPLICA STATUS")
                rs = c.fetchone()
                if rs:
                    c.execute("STOP REPLICA IO_THREAD")
                    retrieved = (rs.get("Retrieved_Gtid_Set") or "").replace("\n", "")
                    if retrieved:
                        c.execute("SELECT WAIT_FOR_EXECUTED_GTID_SET(%s, 30)", (retrieved,))
                try:
                    c.execute("SELECT epoch FROM dbha.leader WHERE id=1")
                    row = c.fetchone()
                    table_epoch = int(row["epoch"]) if row else 0
                except Exception:
                    table_epoch = 0
                epoch = max([v.get("epoch", 0) for v in view.values()] + [self._read("epoch", 0), table_epoch]) + 1
                if rs:
                    c.execute("STOP REPLICA")
                    c.execute("RESET REPLICA ALL")
                c.execute("SET GLOBAL rpl_semi_sync_source_enabled=1")
                c.execute("SET GLOBAL read_only=OFF")
                c.execute("SET GLOBAL super_read_only=OFF")
                # the epoch is stored in a replicated table so every node learns it, even ones that were down
                c.execute("CREATE DATABASE IF NOT EXISTS dbha")
                c.execute("CREATE TABLE IF NOT EXISTS dbha.leader (id TINYINT PRIMARY KEY, epoch BIGINT NOT NULL, "
                          "ip VARCHAR(45) NOT NULL, ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)")
                c.execute("REPLACE INTO dbha.leader (id, epoch, ip) VALUES (1, %s, %s)", (epoch, self.me))
        finally:
            conn.close()
        self._write("epoch", epoch)
        self.state["no_primary_since"] = None
        log.warning("promoted, leadership epoch=%s", epoch)

    def demote(self):
        log.warning("demoting: super_read_only=ON")
        self.sql("SET GLOBAL super_read_only=ON", "SET GLOBAL rpl_semi_sync_source_enabled=0")

    def ensure_primary(self, me):
        if me.get("repl"):  # stale replica config left over after a promotion
            self.sql("STOP REPLICA", "RESET REPLICA ALL", ignore_errors=True)
        self.sql("SET GLOBAL rpl_semi_sync_source_enabled=1", ignore_errors=True)

    def clone_from(self, ip, reason):
        last = self._read("last_clone", 0.0)
        if time.time() - last < self.cfg["min_clone_interval"]:
            log.error("clone from %s wanted (%s) but last clone was %ds ago - waiting", ip, reason, time.time() - last)
            return
        log.warning("CLONE from %s because: %s", ip, reason)
        self._write("last_clone", time.time())
        self.sql("STOP REPLICA", "RESET REPLICA ALL", "SET GLOBAL rpl_semi_sync_source_enabled=0", ignore_errors=True)
        try:
            # CLONE refuses to run on a read-only recipient (error 1290), so lift read-only in the SAME session,
            # right before the clone. The node is wiped anyway and comes back read-only from my.cnf after the restart.
            self.sql("SET GLOBAL super_read_only=OFF", "SET GLOBAL read_only=OFF",
                     f"SET GLOBAL clone_valid_donor_list='{ip}:{self.cfg['mysql_port']}'",
                     f"CLONE INSTANCE FROM '{self.cfg['user']}'@'{ip}':{self.cfg['mysql_port']} "
                     f"IDENTIFIED BY '{self.cfg['password']}'")
        except Exception as exc:
            code = exc.args[0] if getattr(exc, "args", None) and isinstance(exc.args[0], int) else None
            if code in (2006, 2013, 2055):  # connection lost: mysqld restarted after the clone - expected
                log.warning("clone finished, mysqld is restarting (%s) - replication is re-attached next loop", exc)
            else:
                log.error("clone FAILED: %s - restoring read-only", exc)
                try:
                    self.sql("SET GLOBAL super_read_only=ON", ignore_errors=True)
                except Exception as exc2:  # mysqld may be restarting; it boots read-only from my.cnf anyway
                    log.warning("could not restore read-only now (%s); my.cnf keeps it read-only on restart", exc2)
        self.retries = 0

    def ensure_replica(self, me, tgt_ip, tgt):
        if me.get("writable"):
            self.demote()
        r = me.get("repl")
        if r is None or r["source_host"] != tgt_ip:
            if not self.gtid_subset(me["gtid"], tgt["gtid"]):
                return self.clone_from(tgt_ip, "local GTIDs are not a subset of the primary (errant transactions)")
            log.info("attaching to primary %s", tgt_ip)
            self.sql("STOP REPLICA", ignore_errors=True)
            self.sql(f"CHANGE REPLICATION SOURCE TO SOURCE_HOST='{tgt_ip}', SOURCE_PORT={self.cfg['mysql_port']}, "
                     f"SOURCE_USER='{self.cfg['user']}', SOURCE_PASSWORD='{self.cfg['password']}', "
                     f"SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1, SOURCE_CONNECT_RETRY=2, "
                     f"SOURCE_RETRY_COUNT=86400, SOURCE_HEARTBEAT_PERIOD=2",
                     "SET GLOBAL rpl_semi_sync_source_enabled=0", "START REPLICA")
            return
        if r["io"] == "Yes" and r["sql"] == "Yes":
            self.retries = 0
            return
        errno = r["sql_errno"] or r["io_errno"]
        if r["io"] == "Connecting" and errno in TRANSIENT_ERRNOS:
            return  # mysqld retries by itself
        if errno in TRANSIENT_ERRNOS and self.retries < self.cfg["max_start_retries"]:
            self.retries += 1
            log.info("replica threads stopped (errno %s) - START REPLICA (%d)", errno, self.retries)
            return self.sql("START REPLICA", ignore_errors=True)
        if errno and errno not in TRANSIENT_ERRNOS and self.retries < 1:
            self.retries += 1
            return self.sql("START REPLICA", ignore_errors=True)  # one cheap retry, then clone
        self.clone_from(tgt_ip, f"replication broken: errno={errno} {r.get('error')}")

    def kick_service(self):
        """mysqld unreachable. A deliberate `systemctl stop` must be respected; restart only if the unit is
        'failed' (crash loop) or if we just cloned and mysqld did not come back by itself."""
        recently_cloned = time.time() - self._read("last_clone", 0.0) < self.cfg["post_clone_restart_window"]
        failed = subprocess.run(self.cfg["failed_cmd"], timeout=30, check=False).returncode == 0
        if not (failed or recently_cloned):
            return
        if time.time() - self.last_service_kick < 30:
            return
        self.last_service_kick = time.time()
        log.error("local mysqld unreachable (failed=%s recently_cloned=%s) - running %s",
                  failed, recently_cloned, self.cfg["restart_cmd"])
        subprocess.run(self.cfg["restart_cmd"], timeout=120, check=False)

    # ---- main loop
    def tick(self):
        view = {ip: self.probe(ip) for ip in self.nodes}
        me = view[self.me]
        if not me["up"]:
            self.set_snapshot("down", view, None)
            return self.kick_service()
        action, target = decide(self.me, view, len(self.nodes), self.cfg, self.state, time.time())
        writers = [ip for ip, v in view.items() if v["up"] and v["writable"]]
        role = "replica"
        if action == "keep_primary":
            self.ensure_primary(me); role = "primary"
        elif action == "follow":
            self.ensure_replica(me, target, view[target])
        elif action == "demote_follow":
            self.demote(); self.ensure_replica({**me, "writable": False}, target, view[target])
        elif action == "fence":
            self.demote(); role = "fenced"
        elif action == "promote":
            self.promote(view); role = "primary"
        self.set_snapshot(role, view, writers[0] if len(writers) == 1 else None, action)

    def set_snapshot(self, role, view, primary_seen, action=""):
        me = view[self.me]
        r = me.get("repl") or {}
        with self.lock:
            self.snapshot = {
                "ip": self.me, "role": role, "action": action, "epoch": self._read("epoch", 0),
                "mysql_up": me["up"], "writable": me["writable"], "primary_seen": primary_seen,
                "gtid_count": gtid_count(me["gtid"]), "server_id": me["server_id"],
                "replication_ok": r.get("io") == "Yes" and r.get("sql") == "Yes",
                "replication": r, "peers_up": [ip for ip, v in view.items() if v["up"] and ip != self.me],
                "ts": int(time.time()),
            }

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            self.stop.wait(self.cfg["interval"])


def make_handler(agent):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            with agent.lock:
                snap = dict(agent.snapshot)
            fresh = time.time() - snap.get("ts", 0) < agent.cfg["interval"] * 4
            if self.path == "/primary":
                ok = snap.get("role") == "primary" and snap.get("writable") and fresh
                return self._send(200 if ok else 503, "primary\n" if ok else "not-primary\n", "text/plain")
            if self.path == "/health":
                return self._send(200 if fresh else 503, "ok\n" if fresh else "stale\n", "text/plain")
            if self.path == "/status":
                return self._send(200, json.dumps(snap))
            self._send(404, "not found\n", "text/plain")
    return H


def main():
    cfg = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "/etc/dbha/config.json"))
    logging.basicConfig(level=cfg.get("log_level", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    agent = Agent(cfg)
    srv = ThreadingHTTPServer((agent.cfg["http_bind"], agent.cfg["http_port"]), make_handler(agent))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: agent.stop.set())
    log.info("dbha-agent started on %s, peers=%s", agent.me, agent.peers)
    agent.run()


if __name__ == "__main__":
    main()
