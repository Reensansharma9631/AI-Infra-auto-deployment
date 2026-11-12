"""Background jobs: a job is a list of steps; each step is a command (list) or an in-process Python function.
Output goes to <data>/jobs/<id>/log so the UI can stream it; state lives in the jobs table."""
import inspect, json, os, signal, subprocess, threading, time, traceback

from flask import Blueprint, Response, g, jsonify, request

from . import auth, db

bp = Blueprint("jobs", __name__)
FUNCS = {}            # name -> fn(log, **args) ; registered by feature modules
HOOKS = {}            # kind -> fn(job_row, ok)  called after a job finishes
_procs = {}           # job id -> Popen
_cancel = set()
_lock = threading.Lock()
SECRET_WORDS = ("password", "secret", "token", "apikey", "api_key", "passwd")


def register(name):
    def deco(fn):
        FUNCS[name] = fn
        return fn
    return deco


def job_dir(jid):
    d = os.path.join(db.data_dir(), "jobs", str(jid))
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def redact(text, secrets=()):
    for s in secrets:
        if s and len(str(s)) >= 4:
            text = text.replace(str(s), "********")
    return text


def running(kind):
    """The queued/running job of this kind, if any (a second deployment on the same machines must not start)."""
    return db.one("SELECT id, title FROM jobs WHERE kind=? AND state IN ('queued','running') ORDER BY id DESC LIMIT 1", (kind,))


def start(kind, title, steps, meta=None, user=None, secrets=(), sync=False):
    """steps: [{"name": str, "cmd": [..], "env": {}, "cwd": str, "timeout": s} | {"name", "fn": name, "args": {}}]"""
    meta = meta or {}
    st = [{"name": s["name"], "state": "pending"} for s in steps]
    jid = db.ex("INSERT INTO jobs(kind,title,state,created,by_user,meta,steps) VALUES(?,?,?,?,?,?,?)",
                (kind, title, "queued", time.time(), user or (g.get("user") or {}).get("username", "system"),
                 json.dumps(meta), json.dumps(st)))
    log_path = os.path.join(job_dir(jid), "log")
    open(log_path, "w").close()
    db.ex("UPDATE jobs SET log_path=? WHERE id=?", (log_path, jid))
    t = threading.Thread(target=_run_pinned, args=(db.path(), db.data_dir(), jid, steps, list(secrets)), daemon=True)
    t.start()
    if sync:
        t.join()
    return jid


def _run_pinned(dbpath, data, jid, steps, secrets):
    db.use(dbpath, data)
    _run(jid, steps, secrets)


def _set_step(jid, i, **kw):
    row = db.one("SELECT steps FROM jobs WHERE id=?", (jid,))
    st = json.loads(row["steps"])
    st[i].update(kw)
    done = sum(1 for s in st if s["state"] in ("ok", "failed", "skipped"))
    db.ex("UPDATE jobs SET steps=?, progress=? WHERE id=?", (json.dumps(st), int(100 * done / max(1, len(st))), jid))


def _run(jid, steps, secrets):
    row = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    lp = row["log_path"]
    db.ex("UPDATE jobs SET state='running', started=? WHERE id=?", (time.time(), jid))
    ok, rc = True, 0
    with open(lp, "a", buffering=1) as fh:
        def log(msg):
            fh.write(redact(str(msg), secrets).rstrip("\n") + "\n")
        for i, s in enumerate(steps):
            if jid in _cancel:
                _set_step(jid, i, state="skipped")
                continue
            if not ok:
                _set_step(jid, i, state="skipped")
                continue
            _set_step(jid, i, state="running", started=time.time())
            log(f"===== [{i + 1}/{len(steps)}] {s['name']} =====")
            try:
                if "fn" in s:
                    fn = FUNCS[s["fn"]]
                    kw = dict(s.get("args") or {})
                    if "job_id" in inspect.signature(fn).parameters:
                        kw["job_id"] = jid
                    res = fn(log, **kw)
                    rc = 0 if res is not False else 1
                else:
                    env = dict(os.environ, **{k: str(v) for k, v in (s.get("env") or {}).items()})
                    p = subprocess.Popen(s["cmd"], cwd=s.get("cwd"), env=env, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, start_new_session=True, text=True, errors="replace")
                    with _lock:
                        _procs[jid] = p
                    deadline = time.time() + float(s.get("timeout") or 4 * 3600)
                    timer = threading.Timer(max(1, deadline - time.time()), lambda: _kill(p))
                    timer.start()
                    try:
                        for line in p.stdout:
                            log(line)
                        rc = p.wait()
                    finally:
                        timer.cancel()
                        with _lock:
                            _procs.pop(jid, None)
                    if jid in _cancel:
                        log("cancelled by user")
            except Exception as e:  # noqa: BLE001
                log(f"ERROR: {type(e).__name__}: {e}")
                log(traceback.format_exc(limit=3))
                rc = 1
            step_ok = rc == 0 and jid not in _cancel
            _set_step(jid, i, state="ok" if step_ok else "failed", rc=rc, finished=time.time())
            log(f"----- {s['name']}: {'OK' if step_ok else 'FAILED (rc=%s)' % rc}")
            ok = ok and step_ok
        state = "cancelled" if jid in _cancel else ("success" if ok else "failed")
        log(f"JOB {state.upper()}")
    # hooks (e.g. component state) run BEFORE the job is marked finished, so anyone who sees the final job state
    # also sees their effect
    row = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    hook = HOOKS.get(row["kind"])
    if hook:
        try:
            hook(dict(row, state=state), state == "success")
        except Exception:  # noqa: BLE001
            pass
    db.ex("UPDATE jobs SET state=?, finished=?, rc=?, progress=100 WHERE id=?", (state, time.time(), rc, jid))
    _cancel.discard(jid)


def _kill(p):
    try:
        os.killpg(p.pid, signal.SIGTERM)
        time.sleep(3)
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def cancel(jid):
    _cancel.add(jid)
    with _lock:
        p = _procs.get(jid)
    if p:
        _kill(p)


def wait(jid, timeout=600):
    t = time.time()
    while time.time() - t < timeout:
        r = db.one("SELECT state FROM jobs WHERE id=?", (jid,))
        if r and r["state"] not in ("queued", "running"):
            return r["state"]
        time.sleep(0.1)
    return "timeout"


def row_out(r):
    r = dict(r)
    r["meta"] = json.loads(r.get("meta") or "{}")
    r["steps"] = json.loads(r.get("steps") or "[]")
    r.pop("log_path", None)
    return r


@bp.route("/api/jobs")
@auth.login_required
def list_jobs():
    kind = request.args.get("kind")
    sql, args = "SELECT * FROM jobs", []
    if kind:
        sql += " WHERE kind LIKE ?"
        args.append(kind + "%")
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(min(500, int(request.args.get("limit", 100))))
    return jsonify(jobs=[row_out(r) for r in db.q(sql, args)])


@bp.route("/api/jobs/<int:jid>")
@auth.login_required
def get_job(jid):
    r = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    if not r:
        return jsonify(error="no such job"), 404
    off = max(0, int(request.args.get("offset", 0)))
    data = ""
    try:
        with open(r["log_path"], "rb") as fh:
            fh.seek(off)
            data = fh.read(256 * 1024).decode(errors="replace")
    except OSError:
        pass
    return jsonify(job=row_out(r), log=data, offset=off + len(data.encode()))


@bp.route("/api/jobs/<int:jid>/log.txt")
@auth.login_required
def job_log(jid):
    r = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    if not r:
        return jsonify(error="no such job"), 404
    try:
        data = open(r["log_path"], encoding="utf-8", errors="replace").read()
    except OSError:
        data = ""
    return Response(data, mimetype="text/plain", headers={"Content-Disposition": f"attachment; filename=job-{jid}.log"})


@bp.route("/api/jobs/<int:jid>/cancel", methods=["POST"])
@auth.role_required("operator")
def cancel_job(jid):
    r = db.one("SELECT state FROM jobs WHERE id=?", (jid,))
    if not r:
        return jsonify(error="no such job"), 404
    if r["state"] not in ("queued", "running"):
        return jsonify(error=f"job is already {r['state']}"), 409
    cancel(jid)
    auth.audit("job.cancel", jid)
    return jsonify(ok=True)
