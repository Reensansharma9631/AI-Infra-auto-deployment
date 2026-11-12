"""Shared fixtures: an isolated AutoInfra app per test (own data dir + DB), users of every role, CloudStack and Ollama simulators."""
import json, os, pathlib, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))
sys.path.insert(0, str(ROOT / "tests"))
FAKES = ROOT / "tests" / "fakes"
PW = {"admin": "Admin-Pass-2026!", "operator": "Operator-Pass-2026!", "viewer": "Viewer-Pass-2026!"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOINFRA_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("AUTOINFRA_REPO", str(tmp_path / "repo"))
    monkeypatch.setenv("AUTOINFRA_ADMIN_PASSWORD", PW["admin"])
    monkeypatch.setenv("AUTOINFRA_ANSIBLE_PLAYBOOK", str(FAKES / "ansible-playbook"))
    monkeypatch.setenv("AUTOINFRA_KOLLA_BIN", str(FAKES / "kolla-ansible"))
    monkeypatch.setenv("FAKE_ANSIBLE_LOG", str(tmp_path / "ansible-calls.jsonl"))
    monkeypatch.setenv("AUTOINFRA_COOKIE_SECURE", "0")
    monkeypatch.setenv("AUTOINFRA_MASTER_IP", "10.0.0.5")
    monkeypatch.setenv("AUTOINFRA_CS_API_WAIT", "3")
    monkeypatch.delenv("AUTOINFRA_KOLLA_SHARE", raising=False)
    from autoinfra import auth, create_app, db
    auth._fails.clear()
    a = create_app({"TESTING": True})
    from werkzeug.security import generate_password_hash
    with a.app_context():
        for role in ("operator", "viewer"):
            db.ex("INSERT INTO users(username, full_name, pw_hash, role, created) VALUES(?,?,?,?,?)",
                  (role, role.title(), generate_password_hash(PW[role]), role, time.time()))
    a.tmp = tmp_path
    yield a
    db.reset_cache()


class Client:
    def __init__(self, app, role=None):
        self.c = app.test_client()
        self.app = app
        self.role = role
        if role:
            r = self.c.post("/login", data={"username": role, "password": PW[role], "csrf_token": self.csrf()})
            assert r.status_code == 302, r.data
        self.token = self.csrf()

    def csrf(self):
        with self.c.session_transaction() as s:
            if "csrf" not in s:
                s["csrf"] = "t-" + os.urandom(8).hex()
            return s["csrf"]

    def _h(self, h=None):
        x = {"X-CSRF-Token": self.csrf()}
        x.update(h or {})
        return x

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, json=None, **kw):
        kw.setdefault("headers", self._h(kw.pop("headers", None)))
        return self.c.post(url, json=json if json is not None else ({} if "data" not in kw else None), **kw)

    def put(self, url, data=None, **kw):
        kw.setdefault("headers", self._h(kw.pop("headers", None)))
        return self.c.put(url, data=data, **kw)

    def patch(self, url, json=None, **kw):
        kw.setdefault("headers", self._h(kw.pop("headers", None)))
        return self.c.patch(url, json=json or {}, **kw)

    def delete(self, url, **kw):
        kw.setdefault("headers", self._h(kw.pop("headers", None)))
        return self.c.delete(url, **kw)


@pytest.fixture
def admin(app):
    return Client(app, "admin")


@pytest.fixture
def operator(app):
    return Client(app, "operator")


@pytest.fixture
def viewer(app):
    return Client(app, "viewer")


@pytest.fixture
def anon(app):
    return Client(app, None)


def add_host(client, ip, **kw):
    body = {"ip": ip, "hostname": kw.pop("hostname", f"h-{ip.replace('.', '-')}"), "roles": kw.pop("roles", []), **kw}
    r = client.post("/api/hosts", json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def mark_ready(app, hid, family="debian", os_id="ubuntu", version="24.04"):
    from autoinfra import db
    with app.app_context():
        db.ex("UPDATE hosts SET status='ready', key_ok=1, os_family=?, os_id=?, os_version=? WHERE id=?", (family, os_id, version, hid))


def wait_job(client, jid, timeout=60):
    t = time.time()
    while time.time() - t < timeout:
        j = client.get(f"/api/jobs/{jid}").get_json()["job"]
        if j["state"] not in ("queued", "running"):
            return j
        time.sleep(0.05)
    raise AssertionError(f"job {jid} did not finish")


def job_log(client, jid):
    return client.get(f"/api/jobs/{jid}/log.txt").data.decode()


@pytest.fixture
def cs_mock():
    import mock_cloudstack_api as mock
    S = mock.State()
    srv = mock.make_server(S)
    S.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield S
    srv.shutdown()


@pytest.fixture
def cs_configured(app, cs_mock):
    from autoinfra import db, inventory
    with app.app_context():
        db.set_setting("cs_api_url", cs_mock.url)
        db.set_setting("cs_api_key", cs_mock.api_key)
        db.set_setting("cs_poll_interval", 0.01)
        inventory.secret_set("cs_secret_key", cs_mock.secret)
    return cs_mock


class OllamaState:
    def __init__(self):
        self.calls, self.status, self.answer, self.models = [], 200, "All 3 hosts are healthy.", ["llama3.1:8b", "mistral:7b"]


@pytest.fixture
def ollama(app):
    st = OllamaState()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _out(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/api/tags":
                return self._out(200, {"models": [{"name": m} for m in st.models]})
            self._out(404, {"error": "not found"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            st.calls.append(body)
            if st.status != 200:
                return self._out(st.status, {"error": "model not found"})
            self._out(200, {"model": body.get("model"), "message": {"role": "assistant", "content": st.answer}, "done": True})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from autoinfra import db
    with app.app_context():
        db.set_setting("ollama_url", f"http://127.0.0.1:{srv.server_address[1]}")
    st.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield st
    srv.shutdown()


def ansible_calls(app):
    p = app.tmp / "ansible-calls.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
