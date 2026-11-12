"""Every route of the app: login required, CSRF on every write, role checks (viewer < operator < admin), security headers."""
import pytest

from conftest import Client

LOGIN_ONLY_WRITES = {("POST", "/api/me/password"), ("POST", "/api/ai/chat"), ("DELETE", "/api/ai/history"), ("POST", "/api/reports"),
                     ("POST", "/api/packages/online/manifest"), ("POST", "/api/packages/online/kit")}
ADMIN = {("POST", "/api/billing/rates"), ("GET", "/api/users"), ("POST", "/api/users"), ("PATCH", "/api/users/<int:uid>"),
         ("POST", "/api/os/passwords/regenerate"), ("POST", "/api/components/<cid>/reset"), ("GET", "/api/audit"), ("GET", "/audit")}
PUBLIC = {"/login", "/static/<path:filename>", "/repo/", "/repo/<path:p>"}
SAMPLE = {"<int:hid>": "1", "<int:uid>": "1", "<int:jid>": "1", "<int:aid>": "1", "<int:did>": "1", "<int:rid>": "1",
          "<cid>": "mysql", "<resource>": "zones", "<rid>": "abc", "<action>": "enable", "<repo>": "custom", "<product>": "cloudstack",
          "<step>": "requirements", "<path:filename>": "app.css"}


def routes():
    from autoinfra import create_app
    import os, tempfile
    d = tempfile.mkdtemp()
    os.environ["AUTOINFRA_DATA"] = d
    app = create_app({"TESTING": True})
    out = []
    for r in app.url_map.iter_rules():
        for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
            out.append((m, r.rule))
    return sorted(set(out))


ROUTES = routes()
WRITES = [(m, r) for m, r in ROUTES if m != "GET" and r not in PUBLIC and r != "/logout"]
API_GETS = [(m, r) for m, r in ROUTES if m == "GET" and r.startswith("/api/")]
PAGES = [(m, r) for m, r in ROUTES if m == "GET" and not r.startswith("/api/") and r not in PUBLIC]


def url(rule):
    for k, v in SAMPLE.items():
        rule = rule.replace(k, v)
    return rule


def call(c, m, u, csrf=True):
    if m == "GET":
        return c.get(u)
    kw = {} if csrf else {"headers": {"X-CSRF-Token": "wrong"}}
    return {"POST": c.post, "PUT": c.put, "PATCH": c.patch, "DELETE": c.delete}[m](u, **kw)


def test_route_inventory_is_large():
    assert len(ROUTES) >= 100


@pytest.mark.parametrize("m,rule", [x for x in ROUTES if x[1] not in PUBLIC and x[1] != "/logout"])
def test_anonymous_is_rejected(anon, m, rule):
    r = call(anon, m, url(rule))
    if rule.startswith("/api/"):
        assert r.status_code == 401, (m, rule, r.status_code)
    else:
        assert r.status_code == 302 and "/login" in r.headers["Location"]


@pytest.mark.parametrize("m,rule", WRITES)
def test_csrf_required(admin, m, rule):
    r = call(admin, m, url(rule), csrf=False)
    assert r.status_code == 400 and "CSRF" in r.get_json()["error"]


@pytest.mark.parametrize("m,rule", [w for w in WRITES if w not in LOGIN_ONLY_WRITES])
def test_viewer_cannot_write(viewer, m, rule):
    r = call(viewer, m, url(rule))
    assert r.status_code == 403, (m, rule, r.status_code, r.data[:200])


@pytest.mark.parametrize("m,rule", sorted(ADMIN))
def test_operator_cannot_use_admin_endpoints(operator, m, rule):
    r = call(operator, m, url(rule))
    assert r.status_code == 403, (m, rule, r.status_code)


@pytest.mark.parametrize("m,rule", [g for g in API_GETS if g not in ADMIN])
def test_viewer_can_read(viewer, m, rule):
    r = call(viewer, m, url(rule))
    assert r.status_code not in (401, 403, 500), (rule, r.status_code, r.data[:300])


@pytest.mark.parametrize("m,rule", [p for p in PAGES if p not in ADMIN])
def test_pages_render_for_viewer(viewer, m, rule):
    r = viewer.get(url(rule))
    assert r.status_code == 200, (rule, r.status_code)
    assert b"<!doctype html>" in r.data[:50].lower() and b"AutoInfra" in r.data


@pytest.mark.parametrize("m,rule", PAGES)
def test_pages_render_for_admin(admin, m, rule):
    r = admin.get(url(rule))
    assert r.status_code == 200, (rule, r.status_code)


@pytest.mark.parametrize("m,rule", [p for p in PAGES][:6] + [g for g in API_GETS][:6])
def test_security_headers(admin, m, rule):
    r = admin.get(url(rule))
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["X-Content-Type-Options"] == "nosniff"
    assert "script-src 'self'" in r.headers["Content-Security-Policy"] and "unsafe-inline" not in r.headers["Content-Security-Policy"].split("script-src")[1].split(";")[0]


def test_no_inline_scripts_in_pages(admin):
    for m, rule in PAGES:
        html = admin.get(url(rule)).data.decode()
        import re
        for tag in re.findall(r"<script[^>]*>", html):
            assert "src=" in tag, (rule, tag)


def test_login_success_and_audit(app):
    c = Client(app, "admin")
    rows = c.get("/api/audit").get_json()["audit"]
    assert any(r["action"] == "login" and r["user"] == "admin" for r in rows)


def test_login_failure_never_logs_password(app, admin):
    c = Client(app)
    c.c.post("/login", data={"username": "admin", "password": "Secret-Wrong-Pw-123", "csrf_token": c.csrf()})
    rows = admin.get("/api/audit").get_json()["audit"]
    assert any(r["action"] == "login.failed" for r in rows)
    assert all("Secret-Wrong-Pw-123" not in (r["detail"] or "") + (r["target"] or "") for r in rows)


def test_login_throttle(app):
    c = Client(app)
    for _ in range(5):
        assert c.c.post("/login", data={"username": "admin", "password": "x", "csrf_token": c.csrf()}).status_code == 401
    assert c.c.post("/login", data={"username": "admin", "password": "x", "csrf_token": c.csrf()}).status_code == 429


@pytest.mark.parametrize("nxt,expect", [("/hosts", "/hosts"), ("https://evil.com/", "/"), ("//evil.com", "/"), ("/\\evil.com", "/"), ("javascript:alert(1)", "/")])
def test_open_redirect_blocked(app, nxt, expect):
    c = Client(app)
    r = c.c.post(f"/login?next={nxt}", data={"username": "admin", "password": "Admin-Pass-2026!", "csrf_token": c.csrf()})
    assert r.status_code == 302 and r.headers["Location"].endswith(expect)


def test_logout_clears_session(app):
    c = Client(app, "admin")
    assert c.get("/api/hosts").status_code == 200
    c.c.post("/logout", data={"csrf_token": c.csrf()})
    assert c.get("/api/hosts").status_code == 401


def test_disabled_user_cannot_login(app, admin):
    uid = [u for u in admin.get("/api/users").get_json()["users"] if u["username"] == "viewer"][0]["id"]
    assert admin.patch(f"/api/users/{uid}", json={"disabled": True}).status_code == 200
    c = Client(app)
    r = c.c.post("/login", data={"username": "viewer", "password": "Viewer-Pass-2026!", "csrf_token": c.csrf()})
    assert r.status_code == 401


def test_404_json_for_api(admin):
    assert admin.get("/api/does-not-exist").status_code == 404
    assert admin.get("/api/does-not-exist").get_json()["error"] == "not found"


def test_404_page(admin):
    r = admin.get("/nope")
    assert r.status_code == 404 and b"Page not found" in r.data


def test_cookie_flags(app):
    c = Client(app)
    r = c.c.post("/login", data={"username": "admin", "password": "Admin-Pass-2026!", "csrf_token": c.csrf()})
    cookie = r.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
