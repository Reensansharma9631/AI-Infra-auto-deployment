"""Login, roles (viewer < operator < admin), CSRF and audit log."""
import functools, hmac, os, secrets, time
from urllib.parse import urlparse

from flask import Blueprint, abort, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, validators as V

bp = Blueprint("auth", __name__)
RANK = {"viewer": 1, "operator": 2, "admin": 3}
_fails = {}          # ip -> [timestamps]
FAIL_LIMIT, FAIL_WINDOW = 5, 300


def audit(action, target="", detail="", ok=True, user=None):
    db.ex("INSERT INTO audit(ts,user,ip,action,target,detail,ok) VALUES(?,?,?,?,?,?,?)",
          (time.time(), user if user is not None else (g.get("user") or {}).get("username", "-"),
           request.remote_addr if request else "-", action, str(target)[:200], str(detail)[:2000], 1 if ok else 0))


def ensure_admin():
    if db.one("SELECT id FROM users LIMIT 1"):
        return
    h = os.environ.get("AUTOINFRA_ADMIN_HASH")
    pw = os.environ.get("AUTOINFRA_ADMIN_PASSWORD")
    if not h and pw:
        h = generate_password_hash(pw)
    if not h:
        return  # first-run: install script sets AUTOINFRA_ADMIN_HASH
    db.ex("INSERT INTO users(username,full_name,pw_hash,role,created) VALUES(?,?,?,?,?)",
          ("admin", "Administrator", h, "admin", time.time()))


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    u = db.one("SELECT id,username,full_name,role,disabled FROM users WHERE id=?", (uid,))
    if not u or u["disabled"]:
        return None
    return u


def is_api():
    return request.path.startswith("/api/")


def login_required(fn):
    @functools.wraps(fn)
    def w(*a, **k):
        if not g.get("user"):
            if is_api():
                return jsonify(error="login required"), 401
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return fn(*a, **k)
    return w


def role_required(role):
    def deco(fn):
        @functools.wraps(fn)
        def w(*a, **k):
            u = g.get("user")
            if not u:
                if is_api():
                    return jsonify(error="login required"), 401
                return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
            if RANK.get(u["role"], 0) < RANK[role]:
                if is_api():
                    return jsonify(error=f"{role} role required"), 403
                abort(403)
            return fn(*a, **k)
        return w
    return deco


def can(role):
    u = g.get("user")
    return bool(u) and RANK.get(u["role"], 0) >= RANK[role]


def safe_next(target):
    if not target:
        return url_for("pages.dashboard")
    p = urlparse(target)
    if p.scheme or p.netloc or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return url_for("pages.dashboard")
    return target


def init_app(app):
    @app.before_request
    def _load():
        g.user = current_user()
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
            if not hmac.compare_digest(sent, session["csrf"]):
                if is_api():
                    return jsonify(error="CSRF token missing or wrong - reload the page"), 400
                abort(400)

    @app.context_processor
    def _ctx():
        return {"csrf_token": session.get("csrf", ""), "me": g.get("user"), "can": can}

    app.register_blueprint(bp)


@bp.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        ip = request.remote_addr or "-"
        now = time.time()
        fails = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
        _fails[ip] = fails
        if len(fails) >= FAIL_LIMIT:
            audit("login.throttled", request.form.get("username", ""), ok=False, user="-")
            return render_template("login.html", error="Too many failed logins. Wait 5 minutes."), 429
        u = db.one("SELECT * FROM users WHERE username=?", (request.form.get("username", "").strip(),))
        if u and not u["disabled"] and check_password_hash(u["pw_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = u["id"]
            session["csrf"] = secrets.token_urlsafe(32)
            session.permanent = True
            db.ex("UPDATE users SET last_login=? WHERE id=?", (now, u["id"]))
            _fails.pop(ip, None)
            audit("login", u["username"], user=u["username"])
            return redirect(safe_next(request.args.get("next")))
        fails.append(now)
        audit("login.failed", request.form.get("username", "")[:64], ok=False, user="-")
        err = "Wrong user name or password"
    return render_template("login.html", error=err), (401 if err else 200)


@bp.route("/logout", methods=["POST"])
def logout():
    if g.get("user"):
        audit("logout")
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/api/me/password", methods=["POST"])
@login_required
def change_password():
    j = request.get_json(silent=True) or {}
    u = db.one("SELECT * FROM users WHERE id=?", (g.user["id"],))
    if not check_password_hash(u["pw_hash"], j.get("current", "")):
        audit("password.change", u["username"], "wrong current password", ok=False)
        return jsonify(error="current password is wrong"), 400
    e = V.password(j.get("new"))
    if e:
        return jsonify(error=e), 400
    db.ex("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(j["new"]), u["id"]))
    audit("password.change", u["username"])
    return jsonify(ok=True)
