"""AutoInfra - air-gapped CloudStack / OpenStack deployment, management and monitoring UI."""
import os, secrets
from datetime import timedelta

from flask import Flask, jsonify, render_template, request

__version__ = "1.0.0"


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("AUTOINFRA_SECRET") or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("AUTOINFRA_COOKIE_SECURE", "1") != "0",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=int(os.environ.get("AUTOINFRA_SESSION_HOURS", "8"))),
        MAX_CONTENT_LENGTH=80 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    if os.environ.get("AUTOINFRA_BEHIND_PROXY") == "1":     # nginx in front: real client IP for audit + login throttling
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    from . import (ai, auth, billing, cloudstack, components, db, hosts, jobs, monitoring, openstack, packages, pages,
                   reports)
    db.reset_cache()
    db.conn()
    auth.init_app(app)
    for m in (pages, hosts, packages, jobs, cloudstack, openstack, components, monitoring, ai, reports, billing):
        app.register_blueprint(m.bp)
    auth.ensure_admin()
    try:
        hosts.ensure_ssh_key()
    except Exception:  # noqa: BLE001 - UI still starts; Settings shows the problem
        pass
    # jobs that were running when the service stopped can never finish
    db.ex("UPDATE jobs SET state='failed', finished=strftime('%s','now') WHERE state IN ('queued','running')")
    if not app.config.get("TESTING") and os.environ.get("AUTOINFRA_MONITOR", "1") == "1":
        monitoring.start_background(app)

    @app.errorhandler(403)
    def e403(e):
        if request.path.startswith("/api/"):
            return jsonify(error="forbidden"), 403
        return render_template("error.html", code=403, msg="You do not have permission to open this page."), 403

    @app.errorhandler(404)
    def e404(e):
        if request.path.startswith("/api/"):
            return jsonify(error="not found"), 404
        return render_template("error.html", code=404, msg="Page not found."), 404

    @app.errorhandler(413)
    def e413(e):
        return jsonify(error="request too large"), 413

    @app.after_request
    def headers(resp):
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                                   "script-src 'self'; frame-ancestors 'none'; form-action 'self'")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    from markupsafe import Markup, escape
    app.jinja_env.globals["ic"] = lambda n, cls="i": Markup(f'<svg class="{escape(cls)}"><use href="#i-{escape(n)}"/></svg>')

    @app.context_processor
    def version():
        return {"version": __version__}

    return app
