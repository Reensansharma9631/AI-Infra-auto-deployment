"""SQLite storage (WAL mode, one connection per thread). Path: $AUTOINFRA_DB or $AUTOINFRA_DATA/autoinfra.db."""
import json, os, sqlite3, threading, time

_local = threading.local()
_init_lock = threading.Lock()
_initialised = set()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT DEFAULT '',
  pw_hash TEXT NOT NULL, role TEXT NOT NULL, created REAL, last_login REAL, disabled INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS hosts(id INTEGER PRIMARY KEY, ip TEXT NOT NULL, hostname TEXT DEFAULT '', ssh_user TEXT DEFAULT 'root',
  ssh_port INTEGER DEFAULT 22, roles TEXT DEFAULT '', grp TEXT DEFAULT 'default', os_id TEXT DEFAULT '', os_version TEXT DEFAULT '',
  os_family TEXT DEFAULT '', os_pretty TEXT DEFAULT '', os_support TEXT DEFAULT '', kernel TEXT DEFAULT '', key_ok INTEGER DEFAULT 0,
  status TEXT DEFAULT 'new', ping TEXT DEFAULT '', ssh TEXT DEFAULT '', root TEXT DEFAULT '', updates INTEGER DEFAULT -1,
  last_check REAL, facts TEXT DEFAULT '{}', created REAL, hvars TEXT DEFAULT '{}', UNIQUE(ip, ssh_port));
CREATE TABLE IF NOT EXISTS check_runs(id INTEGER PRIMARY KEY, started REAL, finished REAL, by_user TEXT, params TEXT, state TEXT, log TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS checks(id INTEGER PRIMARY KEY, run_id INTEGER, host_id INTEGER, name TEXT, status TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS uploads(id INTEGER PRIMARY KEY, name TEXT, repo TEXT, size INTEGER, sha256 TEXT, received INTEGER DEFAULT 0,
  state TEXT, message TEXT DEFAULT '', log TEXT DEFAULT '', created REAL, finished REAL, by_user TEXT, path TEXT, files INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS repo_items(id INTEGER PRIMARY KEY, repo TEXT, kind TEXT, name TEXT, version TEXT, arch TEXT, path TEXT,
  size INTEGER, sha256 TEXT, upload_id INTEGER, created REAL, provides TEXT DEFAULT '', UNIQUE(repo, path));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts REAL, user TEXT, ip TEXT, action TEXT, target TEXT, detail TEXT, ok INTEGER);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, kind TEXT, title TEXT, state TEXT, created REAL, started REAL, finished REAL,
  by_user TEXT, meta TEXT DEFAULT '{}', steps TEXT DEFAULT '[]', log_path TEXT, rc INTEGER, progress INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS components(id TEXT PRIMARY KEY, state TEXT DEFAULT 'not_installed', version TEXT DEFAULT '',
  hosts TEXT DEFAULT '[]', config TEXT DEFAULT '{}', updated REAL, last_job INTEGER);
CREATE TABLE IF NOT EXISTS component_actions(id INTEGER PRIMARY KEY, ts REAL, component TEXT, action TEXT, status TEXT,
  message TEXT, user TEXT, job_id INTEGER);
CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY, ts REAL, host_id INTEGER, cpu REAL, mem REAL, disk REAL,
  net_rx REAL, net_tx REAL, load1 REAL);
CREATE INDEX IF NOT EXISTS metrics_host_ts ON metrics(host_id, ts);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, ts REAL, severity TEXT, source TEXT, message TEXT, host_id INTEGER,
  akey TEXT, ack INTEGER DEFAULT 0, resolved INTEGER DEFAULT 0, last_seen REAL);
CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY, ts REAL, user TEXT, role TEXT, content TEXT);
CREATE TABLE IF NOT EXISTS kb_docs(id INTEGER PRIMARY KEY, name TEXT, content TEXT, created REAL, by_user TEXT);
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, fmt TEXT, name TEXT, path TEXT, size INTEGER, by_user TEXT);
"""


def data_dir():
    d = getattr(_local, "forced_data", None) or os.environ.get("AUTOINFRA_DATA", "/var/lib/autoinfra")
    os.makedirs(d, exist_ok=True)
    return d


def path():
    forced = getattr(_local, "forced", None)
    if forced:
        return forced
    return os.environ.get("AUTOINFRA_DB") or os.path.join(data_dir(), "autoinfra.db")


def use(p, data):
    """Pin this thread (a background job) to one database + data dir, whatever happens to the environment later."""
    _local.forced, _local.forced_data = p, data


def conn():
    p = path()
    c = getattr(_local, "c", None)
    if c is not None and getattr(_local, "p", None) == p:
        return c
    c = sqlite3.connect(p, timeout=30, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA foreign_keys=ON")
    with _init_lock:
        if p not in _initialised:
            c.executescript(SCHEMA)
            for mig in ("ALTER TABLE hosts ADD COLUMN hvars TEXT DEFAULT '{}'", "ALTER TABLE repo_items ADD COLUMN provides TEXT DEFAULT ''"):
                try:
                    c.execute(mig)
                except sqlite3.OperationalError:
                    pass          # column already there
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
            _initialised.add(p)
    _local.c, _local.p = c, p
    return c


def reset_cache():
    """Tests switch databases between apps; forget cached connections."""
    for k in ("c", "p"):
        _local.__dict__.pop(k, None)


def q(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def ex(sql, args=()):
    cur = conn().execute(sql, args)
    return cur.lastrowid


def get_setting(k, default=None):
    r = one("SELECT v FROM settings WHERE k=?", (k,))
    if not r:
        return default
    try:
        return json.loads(r["v"])
    except ValueError:
        return default


def set_setting(k, v):
    ex("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


def now():
    return time.time()
