"""Browser end-to-end tests: a real Chromium drives the real UI served by the app (CloudStack + Ollama simulators behind it)."""
import json, os, pathlib, re, subprocess, sys, threading, time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ui"))
sys.path.insert(0, str(ROOT / "tests"))
pw = pytest.importorskip("playwright.sync_api")
SHOTS = pathlib.Path(os.environ.get("E2E_SHOTS", "/tmp/autoinfra-e2e-shots"))
ADMIN_PW = "Admin-Pass-2026!"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    os.environ.update(AUTOINFRA_DATA=str(tmp / "data"), AUTOINFRA_REPO=str(tmp / "repo"), AUTOINFRA_ADMIN_PASSWORD=ADMIN_PW,
                      AUTOINFRA_COOKIE_SECURE="0", AUTOINFRA_MONITOR="0", AUTOINFRA_MASTER_IP="10.0.0.5",
                      AUTOINFRA_ANSIBLE_PLAYBOOK=str(ROOT / "tests/fakes/ansible-playbook"), FAKE_ANSIBLE_LOG=str(tmp / "calls.jsonl"))
    import mock_cloudstack_api as mock
    from conftest import OllamaState
    from autoinfra import create_app, db, inventory
    from werkzeug.security import generate_password_hash
    from werkzeug.serving import make_server
    S = mock.State()
    cs = mock.make_server(S)
    app = create_app({"TESTING": False})
    with app.app_context():
        db.ex("INSERT INTO users(username, full_name, pw_hash, role, created) VALUES('viewer','View Only',?,'viewer',?)",
              (generate_password_hash("Viewer-Pass-2026!"), time.time()))
        db.set_setting("cs_api_url", f"http://127.0.0.1:{cs.server_address[1]}")
        db.set_setting("cs_api_key", S.api_key)
        db.set_setting("cs_poll_interval", 0.01)
        inventory.secret_set("cs_secret_key", S.secret)
    # tiny Ollama
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class O(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _o(self, obj):
            b = json.dumps(obj).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        def do_GET(self): self._o({"models": [{"name": "llama3.1:8b"}]})

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self._o({"message": {"role": "assistant", "content": "E2E: infrastructure looks healthy."}})
    osrv = ThreadingHTTPServer(("127.0.0.1", 0), O)
    threading.Thread(target=osrv.serve_forever, daemon=True).start()
    with app.app_context():
        db.set_setting("ollama_url", f"http://127.0.0.1:{osrv.server_address[1]}")
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    yield {"base": base, "app": app, "cs": S, "tmp": tmp}
    srv.shutdown(); cs.shutdown(); osrv.shutdown()


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def pick(p, sel, text):
    """select the option(s) whose label contains text (works for multi-selects)"""
    vals = p.eval_on_selector(sel, "(el, t) => Array.from(el.options).filter(o => o.text.includes(t)).map(o => o.value)", text)
    assert vals, f"no option containing {text!r} in {sel}"
    p.select_option(sel, vals)


class Page:
    def __init__(self, browser, base, user="admin", password=ADMIN_PW):
        self.ctx = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        self.p = self.ctx.new_page()
        self.errors = []
        self.p.on("pageerror", lambda e: self.errors.append(str(e)))
        self.p.on("console", lambda m: m.type == "error" and "502" not in m.text and "409" not in m.text and "400" not in m.text and self.errors.append(m.text))
        self.base = base
        self.p.goto(base + "/login")
        self.p.fill("#u", user)
        self.p.fill("#p", password)
        self.p.click("button[type=submit]")
        self.p.wait_for_url(base + "/")

    def go(self, path):
        self.p.goto(self.base + path)
        self.p.wait_for_load_state("networkidle")
        return self.p

    def close(self):
        self.ctx.close()


@pytest.fixture
def page(server, browser):
    pg = Page(browser, server["base"])
    yield pg
    pg.close()
    assert not pg.errors, pg.errors


PAGES = ["/", "/packages#upload", "/packages#online", "/packages#repo", "/packages#jobs", "/packages#required", "/packages#disk",
         "/predeploy#test", "/predeploy#logs", "/hosts", "/hosts#groups", "/hosts#key", "/cloudstack/wizard", "/cloudstack/infra#zones",
         "/cloudstack/infra#hosts", "/cloudstack/infra#storage", "/cloudstack/infra#networks", "/cloudstack/infra#systemvms",
         "/cloudstack/infra#offerings", "/openstack/wizard", "/components#all", "/components#databases", "/monitoring#topology",
         "/monitoring#hosts", "/monitoring#logs", "/monitoring#alerts", "/monitoring#zabbix", "/ai", "/ai#kb", "/reports", "/billing",
         "/jobs", "/audit", "/settings#general", "/settings#integrations", "/settings#users"]


@pytest.mark.parametrize("path", PAGES)
def test_page_loads_without_js_errors(page, path):
    p = page.go(path)
    p.wait_for_timeout(300)
    assert p.locator(".side .brand b").inner_text() == "AutoInfra"
    assert p.locator("h1").first.inner_text().strip()
    SHOTS.mkdir(parents=True, exist_ok=True)
    p.screenshot(path=str(SHOTS / ((re.sub(r"[^a-z0-9]+", "_", path).strip("_") or "dashboard") + ".png")))


def test_login_wrong_password(server, browser):
    ctx = browser.new_context()
    p = ctx.new_page()
    p.goto(server["base"] + "/login")
    p.fill("#u", "admin"); p.fill("#p", "nope"); p.click("button[type=submit]")
    assert "Wrong user name or password" in p.inner_text(".note")
    ctx.close()


def test_sidebar_groups_and_active(page):
    p = page.go("/cloudstack/infra#hosts")
    assert p.locator('#nav a[data-hash="hosts"].act').count() == 1
    grp = p.locator("#nav .grp").nth(1)
    grp.locator("> span").click()
    assert "open" in grp.get_attribute("class")


def test_add_host_modal_and_validation(page):
    p = page.go("/predeploy")
    p.click("button[data-open=m-add]")
    p.fill("#f-add [name=ip]", "10.0.0.999")
    p.click("#b-add")
    p.wait_for_selector("#f-add .err")
    assert "not a valid IPv4" in p.inner_text("#f-add .err")
    p.fill("#f-add [name=ip]", "127.0.0.1")
    p.fill("#f-add [name=hostname]", "e2e-host")
    p.fill("#f-add [name=ssh_port]", "1")
    p.check("#f-add .role-cb[value=compute]")
    p.click("#b-add")
    p.wait_for_selector("#pd-table >> text=e2e-host")
    assert "Compute" in p.inner_text("#pd-table")


def test_csv_import(page):
    p = page.go("/predeploy")
    p.click("button[data-open=m-csv]")
    p.fill("#csv-text", "ip,hostname,roles\n10.20.0.1,csv-a,management\n10.20.0.2,csv-b,compute;storage\nbad,x,y\n")
    p.click("#b-csv")
    p.wait_for_selector("#csv-out .okbox")
    assert "2 host(s) added" in p.inner_text("#csv-out") and "line 4" in p.inner_text("#csv-out")


def test_run_checks_live_log(page):
    p = page.go("/predeploy")
    if p.locator("#pd-table >> text=e2e-host").count() == 0:
        test_add_host_modal_and_validation(page)
    p.fill("#f-test [name=retries]", "1")
    p.fill("#f-test [name=timeout]", "1")
    p.click("#b-runall")
    p.wait_for_selector("#pd-log:has-text('Summary:')", timeout=60000)
    assert "Ping: FAILED" in p.inner_text("#pd-log")
    p.wait_for_selector("#pd-table >> text=Unreachable")


def test_upload_deb_through_browser(page, server, tmp_path):
    from test_packages import make_deb
    deb = make_deb(tmp_path, "e2e-probe", "3.1")
    p = page.go("/packages#upload")
    p.select_option("#up-repo", "apps")
    p.set_input_files("#up-file", str(deb))
    p.wait_for_selector("#up-list >> text=Uploaded · Extracted", timeout=60000)
    assert "Completed successfully" in p.inner_text("#x-log")
    p.click("button[data-t=repo]")
    p.fill("#repo-q", "e2e-probe")
    p.wait_for_selector("#repo-items >> text=3.1")


def test_online_kit_download(page):
    p = page.go("/packages#online")
    p.wait_for_selector("#on-comps table")
    with p.expect_download() as d:
        p.click("#on-kit")
    path = d.value.path()
    assert open(path).read().startswith("#!/usr/bin/env bash")
    assert subprocess.run(["bash", "-n", path]).returncode == 0


def test_verification_tab(page):
    p = page.go("/packages#required")
    p.wait_for_selector("#rq-list table")
    p.select_option("#rq-family", "rhel")
    p.wait_for_timeout(300)
    assert "cloudstack-management" in p.inner_text("#rq-list")


def test_cs_wizard_validation_and_review(page, server):
    from autoinfra import db
    p = page.go("/predeploy")
    for ip, name, role in [("10.30.0.10", "w-mgmt", "management"), ("10.30.0.41", "w-db", "database"), ("10.30.1.21", "w-kvm", "compute"), ("10.30.0.31", "w-nfs", "storage")]:
        page.p.evaluate("([ip,n,r]) => api('POST','/api/hosts',{ip:ip,hostname:n,roles:[r]})", [ip, name, role])
    with server["app"].app_context():
        db.ex("UPDATE hosts SET status='ready', os_family='debian' WHERE ip LIKE '10.30.%'")
    p = page.go("/cloudstack/wizard#requirements")
    p.click("#wz-next")
    p.wait_for_selector('[data-step="management"].act')
    p.click("#wz-next")                                   # no management host chosen -> error shown
    p.wait_for_selector('[data-step="management"] .err')
    pick(p, '[data-step="management"] [name=host_ids]', "w-mgmt")
    p.click("#wz-next")
    p.wait_for_selector('[data-step="database"].act')
    p.select_option('[data-step="database"] [name=strategy]', "single")
    pick(p, '[data-step="database"] [name=host_ids]', "w-db")
    p.click("#wz-next"); p.wait_for_selector('[data-step="mq"].act')
    p.click("#wz-next"); p.wait_for_selector('[data-step="systemvm"].act')
    p.click("#wz-next"); p.wait_for_selector('[data-step="zone"].act')
    z = '[data-step="zone"] '
    p.fill(z + "[name=dns1]", "not-an-ip")
    p.click("#wz-next")
    p.wait_for_selector(z + ".err")
    vals = {"dns1": "8.8.8.8", "pod_gateway": "10.30.0.1", "pod_start": "10.30.0.100", "pod_end": "10.30.0.130", "public_gateway": "10.30.2.1",
            "public_start": "10.30.2.100", "public_end": "10.30.2.150"}
    for k, v in vals.items():
        p.fill(z + f"[name={k}]", v)
    p.click("#wz-next"); p.wait_for_selector('[data-step="cluster"].act')
    pick(p, "#cl-list .cl-hosts", "w-kvm")
    c = '[data-step="cluster"] '
    p.fill(c + "[name=primary_server]", "10.30.0.31"); p.fill(c + "[name=secondary_server]", "10.30.0.31")
    p.click("#wz-next")
    p.wait_for_selector('[data-step="review"].act')
    p.wait_for_selector("#rv-summary .kv")
    txt = p.inner_text("#rv-summary")
    assert "w-mgmt" in txt and "single" in txt and "Zone-1" in txt and "Install CloudStack management server" in txt


def test_cs_infra_add_zone_and_disable(page, server):
    p = page.go("/cloudstack/infra#zones")
    p.click('[data-add="zones"]')
    p.fill("#f-cs [name=name]", "E2E-Zone")
    p.fill("#f-cs [name=dns1]", "8.8.8.8")
    p.fill("#f-cs [name=internaldns1]", "10.0.0.5")
    p.click("#b-cs")
    p.wait_for_selector("#t-zones >> text=E2E-Zone")
    p.click("#t-zones button:has-text('Enable')")
    p.wait_for_selector("#t-zones >> text=Enabled")
    assert "Total Zones" in p.inner_text("#cs-tiles")


def test_components_deploy_from_ui(page, server):
    from autoinfra import db
    page.p.evaluate("() => api('POST','/api/hosts',{ip:'10.40.0.1',hostname:'pg-01',roles:['database']})")
    with server["app"].app_context():
        db.ex("UPDATE hosts SET status='ready', os_family='rhel' WHERE ip='10.40.0.1'")
    p = page.go("/components#databases")
    p.click('#postgresql [data-dep]')
    p.wait_for_selector("#m-comp.show")
    p.fill("#mc-fields [name=port]", "5433")
    p.click("#mc-save")
    p.wait_for_selector("#toast >> text=configuration saved")
    pick(p, "#mc-hosts", "pg-01")
    p.click("#mc-deploy")
    p.wait_for_selector("#mc-log:has-text('JOB SUCCESS')", timeout=60000)
    assert "PLAY RECAP" in p.inner_text("#mc-log")
    p.click("#m-comp [data-close]")
    p.wait_for_selector("#postgresql >> text=Installed")
    calls = [json.loads(l) for l in (server["tmp"] / "calls.jsonl").read_text().splitlines()]
    assert calls[-1]["playbook"] == "32-postgresql.yml" and calls[-1]["vars"]["pg_port"] == 5433


def test_os_wizard_preview(page, server):
    from autoinfra import db
    for ip, n, r in [("10.50.0.11", "os-ctl", "controller"), ("10.50.0.21", "os-cmp", "os-compute"), ("10.50.0.31", "os-sto", "os-storage")]:
        page.p.evaluate("([ip,n,r]) => api('POST','/api/hosts',{ip:ip,hostname:n,roles:[r]})", [ip, n, r])
    p = page.go("/openstack/wizard#requirements")
    p.click("#ow-next"); p.wait_for_selector('[data-step="nodes"].act')
    pick(p, '[name=controller_ids]', "os-ctl")
    p.click("button[data-t=cmp]")
    pick(p, '[name=compute_ids]', "os-cmp")
    p.click("button[data-t=sto]")
    pick(p, '[name=storage_ids]', "os-sto")
    p.click("#ow-next"); p.wait_for_selector('[data-step="services"].act')
    p.click("#ow-next"); p.wait_for_selector('[data-step="network"].act')
    p.fill('[data-step="network"] [name=vip]', "10.50.0.50")
    p.click("#ow-next"); p.wait_for_selector('[data-step="deploy"].act')
    p.wait_for_selector("#ow-globals:has-text('kolla_internal_vip_address')")
    assert 'kolla_internal_vip_address: "10.50.0.50"' in p.inner_text("#ow-globals")
    p.click("button[data-t=m]")
    assert "os-ctl ansible_host=10.50.0.11" in p.inner_text("#ow-multi")


def test_monitoring_and_ai_chat(page):
    p = page.go("/monitoring")
    p.wait_for_selector("#mon-tiles .tile")
    assert "Total Hosts" in p.inner_text("#mon-tiles") and "CPU Usage" in p.inner_text("#gauges")
    p.click("#mq button >> nth=0")
    p.wait_for_selector("#m-msgs >> text=E2E: infrastructure looks healthy.")
    p.click("button[data-t=topology]")
    p.wait_for_selector("#topo .topo")


def test_ai_page_and_kb(page, tmp_path):
    p = page.go("/ai")
    p.wait_for_selector("#ai-status .pill")
    p.fill("#ai-in", "Show recent errors")
    p.press("#ai-in", "Enter")
    p.wait_for_selector("#ai-msgs >> text=E2E: infrastructure looks healthy.")
    p.click("button[data-t=kb]")
    f = tmp_path / "runbook.txt"
    f.write_text("restart libvirtd")
    p.set_input_files("#kb-file", str(f))
    p.wait_for_selector("#kb-list >> text=runbook.txt")


def test_reports_generate_and_download(page):
    p = page.go("/reports")
    p.select_option("#rp-fmt", "pdf")
    p.click("#rp-go")
    p.wait_for_selector("#rp-list >> text=infra-report-")
    with p.expect_download() as d:
        p.click("#rp-list a:has-text('Download') >> nth=0")
    assert open(d.value.path(), "rb").read(5) == b"%PDF-"


def test_billing_rates(page):
    p = page.go("/billing")
    p.wait_for_selector("#bl-tiles .tile")
    p.fill("#bl-form [name=currency]", "USD")
    p.click("#bl-save")
    p.wait_for_selector("#bl-tiles >> text=USD")


def test_settings_and_users(page):
    p = page.go("/settings#general")
    p.fill("[name=master_ip]", "10.0.0.5")
    p.click("#st-save")
    p.wait_for_selector("#st-msg >> text=saved")
    p.click("button[data-t=users]")
    p.fill("#us-form [name=username]", "e2euser")
    p.fill("#us-form [name=password]", "E2e-Password-2026")
    p.click("#us-add")
    p.wait_for_selector("#us-list >> text=e2euser")


def test_jobs_and_audit(page):
    p = page.go("/jobs")
    p.wait_for_selector("#jb-list table")
    p.click("#jb-list a[data-j] >> nth=0")
    p.wait_for_selector("#jb-log:has-text('=====')")
    p.wait_for_selector("#jb-log:has-text('JOB ')")
    assert not p.locator("#jb-cancel").is_visible()          # finished job: no Cancel button
    p = page.go("/audit")
    p.fill("#au-q", "login")
    p.wait_for_selector("#au-list >> text=login")


def test_log_colouring(page):
    p = page.go("/")
    cls = p.evaluate("""() => [colorLine('n1 : ok=9 changed=6 unreachable=0 failed=0'), colorLine('n1 : ok=1 changed=0 unreachable=0 failed=2'),
                               colorLine('fatal: [n1]: FAILED! => {}'), colorLine('[1/5] Verifying checksum ... OK')]""")
    assert "bad" not in cls[0] and "bad" in cls[1] and "bad" in cls[2] and "ok" in cls[3]


def test_global_search(page):
    p = page.go("/")
    p.fill("#gsearch", "mysql")
    p.wait_for_selector("#gres a")
    assert "MySQL" in p.inner_text("#gres")


def test_viewer_sees_disabled_write_buttons(server, browser):
    v = Page(browser, server["base"], "viewer", "Viewer-Pass-2026!")
    p = v.go("/predeploy")
    assert p.locator("#b-runall").is_disabled() and p.locator("button[data-open=m-add]").is_disabled()
    assert p.locator("#nav >> text=Audit Logs").count() == 0
    p = v.go("/audit")
    assert "Error 403" in p.inner_text("h1")
    v.close()


def test_mobile_layout(server, browser):
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    p = ctx.new_page()
    p.goto(server["base"] + "/login"); p.fill("#u", "admin"); p.fill("#p", ADMIN_PW); p.click("button[type=submit]")
    p.wait_for_url(server["base"] + "/")
    assert p.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    ctx.close()
