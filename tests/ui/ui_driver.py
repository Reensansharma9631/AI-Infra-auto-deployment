#!/usr/bin/env python3
"""Drive AutoInfra's UI in a real browser (Playwright + Chromium) - the same clicks an operator makes.
Used for the live lab runs; every page/step is saved as a screenshot (evidence).

  AI_URL=https://172.27.15.238 AI_USER=lab-automation AI_PASS_FILE=... python tests/ui/ui_driver.py openstack plan.json
plan.json = {"requirements": {...}, "nodes": {...}, "services": {...}, "network": {...}} (field name -> value; lists = multi-select)
Prints the deployment job id on success; exit 1 with the UI's error text otherwise."""
import json, os, pathlib, sys, time

from playwright.sync_api import sync_playwright

URL = os.environ.get("AI_URL", "https://127.0.0.1")
SHOTS = pathlib.Path(os.environ.get("AI_SHOTS", "ui-shots"))


def login(page):
    page.goto(f"{URL}/login")
    page.fill('input[name="username"]', os.environ.get("AI_USER", "admin"))
    page.fill('input[name="password"]', open(os.environ["AI_PASS_FILE"]).read().strip())
    page.click('button[type="submit"]')
    page.wait_for_url(lambda u: "/login" not in u, timeout=20000)


def shot(page, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def fill_form(page, form, values):
    for k, v in values.items():
        el = form.locator(f'[name="{k}"]').first
        if el.count() == 0:
            raise SystemExit(f"UI has no field '{k}'")
        tag = el.evaluate("e => e.tagName.toLowerCase()")
        typ = (el.get_attribute("type") or "").lower()
        if tag == "select":
            el.select_option([str(x) for x in v] if isinstance(v, list) else str(v))
        elif typ == "checkbox":
            if el.is_disabled():
                continue
            el.set_checked(bool(v))
        else:
            el.fill(str(v))


def wizard(page, path, prefix, plan, next_sel, msg_sel, tabs=None):
    page.goto(f"{URL}{path}")
    page.wait_for_load_state("networkidle")
    steps = [s for s in plan]
    for i, st in enumerate(steps):
        form = page.locator(f'form.pane[data-step="{st}"]')
        form.wait_for(state="visible", timeout=20000)
        for tab, fields in (tabs or {}).get(st, {}).items():      # fields that sit on a hidden tab
            form.locator(f'button[data-t="{tab}"]').click()
            fill_form(page, form, {k: plan[st][k] for k in fields if k in plan[st]})
        rest = {k: v for k, v in plan[st].items() if not any(k in f for f in (tabs or {}).get(st, {}).values())}
        fill_form(page, form, rest)
        shot(page, f"{prefix}-{i + 1}-{st}")
        page.click(next_sel)
        time.sleep(1.5)
        err = page.locator(f'form.pane[data-step="{st}"] .errs').inner_text().strip() if form.is_visible() else ""
        msg = page.locator(msg_sel).inner_text().strip() if page.locator(msg_sel).count() else ""
        if form.is_visible():
            shot(page, f"{prefix}-{i + 1}-{st}-ERROR")
            raise SystemExit(f"step '{st}' was not accepted: {err or msg}")


def openstack(page, plan, force=False):
    wizard(page, "/openstack/wizard", "openstack", plan, "#ow-next", "#ow-msg",
           tabs={"nodes": {"cmp": ["compute_ids"], "sto": ["storage_ids"], "net": ["network_ids"], "ctl": ["hostname", "controller_ids"]}})
    page.locator('[data-step="deploy"]').wait_for(state="visible")
    # no wait_for_function: the UI's CSP (script-src 'self', no unsafe-eval) forbids evaluated strings
    page.locator("#ow-globals").filter(has_text="kolla_base_distro").wait_for(timeout=20000)
    shot(page, "openstack-5-review")
    if force:
        page.check("#ow-force")
    page.click("#ow-deploy")
    page.locator("#ow-job:not([hidden]), #ow-errors .note").first.wait_for(timeout=30000)
    err = page.locator("#ow-errors").inner_text().strip()
    if err:
        shot(page, "openstack-6-deploy-ERROR")
        raise SystemExit(f"deploy refused: {err}")
    jid = page.locator("#ow-jt").inner_text().strip().lstrip("job #")
    time.sleep(5)
    shot(page, "openstack-6-deploy-started")
    return jid


def openstack_destroy(page, plan=None, force=False):
    """Admin: 'Remove OpenStack deployment' - answers the typed confirmation like an operator."""
    page.goto(f"{URL}/openstack/wizard#deploy")
    page.wait_for_load_state("networkidle")
    page.once("dialog", lambda d: d.accept("destroy"))
    btn = page.locator("#ow-destroy")
    btn.scroll_into_view_if_needed()
    btn.click()
    page.locator("#ow-job:not([hidden])").wait_for(timeout=30000)
    jid = page.locator("#ow-jt").inner_text().strip().lstrip("job #")
    shot(page, "openstack-destroy-started")
    return jid


def openstack_release(page, plan=None, force=False):
    page.goto(f"{URL}/openstack/wizard#deploy")
    page.wait_for_load_state("networkidle")
    page.once("dialog", lambda d: d.accept("release"))
    btn = page.locator("#ow-release")
    btn.scroll_into_view_if_needed()
    btn.click()
    page.locator("#ow-job:not([hidden])").wait_for(timeout=30000)
    shot(page, "openstack-release-started")
    return page.locator("#ow-jt").inner_text().strip().lstrip("job #")


if __name__ == "__main__":
    what, plan_file = sys.argv[1], sys.argv[2]
    plan = json.load(open(plan_file))
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        login(page)
        jid = {"openstack": openstack, "openstack-destroy": openstack_destroy, "openstack-release": openstack_release}[what](page, plan, force=bool(os.environ.get("AI_FORCE")))
        b.close()
    if errors:
        print("JavaScript errors:", errors)
    print(f"JOB {jid}")
