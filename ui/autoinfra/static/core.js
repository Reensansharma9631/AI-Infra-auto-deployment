/* AutoInfra core: API helper, widgets shared by every page. No external libraries (air-gapped). */
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const CSRF = () => document.querySelector('meta[name="csrf-token"]').content;
const ROLE = () => document.body.dataset.role || "";
const RANK = { viewer: 1, operator: 2, admin: 3 };
const can = (r) => (RANK[ROLE()] || 0) >= RANK[r];

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const ic = (n, cls = "i") => `<svg class="${cls}"><use href="#i-${n}"/></svg>`;

async function api(method, url, body, opts = {}) {
  const o = { method, headers: { "X-CSRF-Token": CSRF() }, credentials: "same-origin" };
  if (body instanceof FormData || body instanceof Blob || body instanceof ArrayBuffer) o.body = body;
  else if (body !== undefined) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(body); }
  const r = await fetch(url, o);
  if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname); throw new Error("login required"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok && !opts.raw) { const e = new Error((data && data.error) || `HTTP ${r.status}`); e.status = r.status; e.data = data; throw e; }
  return opts.raw ? { status: r.status, data, headers: r.headers } : data;
}
const GET = (u) => api("GET", u), POST = (u, b) => api("POST", u, b ?? {});

function toast(msg, kind = "") {
  const d = document.createElement("div"); d.className = kind; d.textContent = msg; $("#toast").appendChild(d);
  setTimeout(() => d.remove(), kind === "bad" ? 7000 : 3500);
}
function fail(e) { toast(e.message || String(e), "bad"); if (e.data && e.data.fields) console.warn(e.data.fields); }

const STATUS = {
  ready: ["p-ok", "Ready"], "ready-with-warnings": ["p-warn", "Ready (warnings)"], "not-ready": ["p-bad", "Not ready"],
  unreachable: ["p-bad", "Unreachable"], "auth-failed": ["p-bad", "Auth failed"], new: ["p-mut", "Not checked"],
  success: ["p-ok", "Completed"], failed: ["p-bad", "Failed"], running: ["p-info", "Running"], queued: ["p-mut", "Queued"],
  cancelled: ["p-mut", "Cancelled"], done: ["p-ok", "Completed"], error: ["p-bad", "Error"], uploading: ["p-info", "Uploading"],
  verifying: ["p-info", "Verifying"], extracting: ["p-info", "Extracting"], indexing: ["p-info", "Indexing"],
  installed: ["p-ok", "Installed"], not_installed: ["p-mut", "Not installed"], deploying: ["p-info", "Deploying"],
  degraded: ["p-warn", "Degraded"], pass: ["p-ok", "Pass"], warn: ["p-warn", "Warning"], fail: ["p-bad", "Fail"],
  critical: ["p-bad", "Critical"], warning: ["p-warn", "Warning"], info: ["p-info", "Info"], ok: ["p-ok", "OK"],
  Enabled: ["p-ok", "Enabled"], Disabled: ["p-mut", "Disabled"], Up: ["p-ok", "Online"], Down: ["p-bad", "Down"],
  Running: ["p-ok", "Running"], Stopped: ["p-mut", "Stopped"], Maintenance: ["p-warn", "Maintenance"], PrepareForMaintenance: ["p-warn", "Maintenance"],
  Ready: ["p-ok", "Ready"], planned: ["p-info", "Planned"],
};
function pill(s, label) { const [c, l] = STATUS[s] || ["p-mut", s || "-"]; return `<span class="pill ${c}">${esc(label || l)}</span>`; }
function dot(ok) { return ok === "ok" || ok === true ? `<span class="dot d-ok">${ic("check")}</span>` : ok === "" || ok == null ? `<span class="dot d-mut"></span>` : `<span class="dot d-bad">${ic("x")}</span>`; }
function fmtBytes(n) { n = +n || 0; const u = ["B", "KB", "MB", "GB", "TB", "PB"]; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${u[i]}`; }
function fmtTs(t) { if (!t) return ""; const d = new Date(t * 1000); return d.toLocaleString(undefined, { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }); }
function ago(t) { if (!t) return "never"; const s = Date.now() / 1000 - t; if (s < 60) return "just now"; if (s < 3600) return `${Math.floor(s / 60)} min ago`; if (s < 86400) return `${Math.floor(s / 3600)} h ago`; return `${Math.floor(s / 86400)} d ago`; }

function table(cols, rows, empty = "Nothing here yet") {
  if (!rows.length) return `<div class="empty">${esc(empty)}</div>`;
  return `<div class="tw"><table><thead><tr>${cols.map((c) => `<th>${esc(c.t)}</th>`).join("")}</tr></thead><tbody>${rows.map((r, i) =>
    `<tr>${cols.map((c) => `<td class="${c.cls || ""}">${c.f ? c.f(r, i) : esc(r[c.k])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
function tile(icon, color, value, label, sub = "") {
  return `<div class="tile"><div class="ic ${color}">${ic(icon)}</div><div><div class="v">${esc(value)}</div><div class="l">${esc(label)}</div>${sub ? `<div class="s">${esc(sub)}</div>` : ""}</div></div>`;
}

/* tabs: <div class="tabs" data-tabs="name"><button data-t="a">..</button></div> + <div class="pane" data-pane="a"> ; linked to location.hash */
function initTabs(root = document, onShow) {
  $$("[data-tabs]", root).forEach((bar) => {
    const show = (t, push) => {
      const btn = $(`button[data-t="${t}"]`, bar); if (!btn) return false;
      $$("button", bar).forEach((b) => b.classList.toggle("act", b === btn));
      const scope = bar.parentElement;
      $$(`[data-pane]`, scope).forEach((p) => { if (p.closest("[data-tabs-scope]") === bar.closest("[data-tabs-scope]")) p.classList.toggle("act", p.dataset.pane === t); });
      if (push && bar.dataset.hash !== "no") history.replaceState(null, "", "#" + t);
      onShow && onShow(t); return true;
    };
    $$("button", bar).forEach((b) => b.addEventListener("click", () => show(b.dataset.t, true)));
    bar._show = show;
    const h = location.hash.slice(1);
    if (!(bar.dataset.hash !== "no" && h && show(h))) show(($("button.act", bar) || $("button", bar)).dataset.t);
  });
  window.addEventListener("hashchange", () => { const h = location.hash.slice(1); $$("[data-tabs]").forEach((b) => b.dataset.hash !== "no" && b._show && b._show(h)); markNav(); });
}

function openModal(id) { $("#" + id).classList.add("show"); }
function closeModal(id) { $("#" + id).classList.remove("show"); }
document.addEventListener("click", (e) => {
  const c = e.target.closest("[data-close]"); if (c) c.closest(".modal").classList.remove("show");
  const o = e.target.closest("[data-open]"); if (o) openModal(o.dataset.open);
});

/* forms: fields with name=..., checkboxes -> bool, data-num -> number, data-ids -> int list from multi-select */
function formData(root) {
  const o = {};
  $$("[name]", root).forEach((el) => {
    if (el.type === "checkbox") o[el.name] = el.checked;
    else if (el.multiple) o[el.name] = $$("option:checked", el).map((x) => +x.value);
    else if (el.dataset.num !== undefined) o[el.name] = el.value === "" ? "" : +el.value;
    else o[el.name] = el.value.trim();
  });
  return o;
}
function fillForm(root, data) {
  $$("[name]", root).forEach((el) => {
    if (!(el.name in data)) return; const v = data[el.name];
    if (el.type === "checkbox") el.checked = !!v;
    else if (el.multiple) $$("option", el).forEach((o) => (o.selected = (v || []).includes(+o.value)));
    else el.value = v ?? "";
  });
}
function showErrors(root, fields) {
  $$(".err", root).forEach((e) => e.remove()); $$(".ferr", root).forEach((e) => e.classList.remove("ferr"));
  Object.entries(fields || {}).forEach(([k, msg]) => {
    const el = $(`[name="${k.split(".")[0]}"]`, root) || $(`[data-err="${k}"]`, root);
    const d = document.createElement("div"); d.className = "err"; d.textContent = msg;
    if (el) { el.closest(".f")?.classList.add("ferr"); (el.closest(".f") || el.parentElement).appendChild(d); }
    else { (root.querySelector(".errs") || root).prepend(d); }
  });
}

/* live job log: polls /api/jobs/<id>?offset= and colours lines */
function colorLine(l) {
  const e = esc(l);
  if (/(FAILED|ERROR|[Ff]atal:|unreachable=[1-9]|failed=[1-9]|: FAIL\b)/.test(l)) return `<span class="bad">${e}</span>`;
  if (/(WARN|WARNING|changed:)/.test(l)) return `<span class="warn">${e}</span>`;
  if (/(: OK|\bOK\b|ok:|JOB SUCCESS|Completed successfully|PASS)/.test(l)) return `<span class="ok">${e}</span>`;
  if (/^(=====|-----|PLAY |TASK |\[\d+\/\d+\])/.test(l)) return `<span class="hd">${e}</span>`;
  return e;
}
function watchJob(jid, el, onDone, onTick) {
  let off = 0, stop = false;
  el.innerHTML = "";
  const tick = async () => {
    if (stop) return;
    try {
      const r = await GET(`/api/jobs/${jid}?offset=${off}`);
      if (r.log) { const atEnd = el.scrollTop + el.clientHeight >= el.scrollHeight - 30; el.insertAdjacentHTML("beforeend", r.log.split("\n").filter((x, i, a) => x || i < a.length - 1).map(colorLine).join("\n") + "\n"); if (atEnd) el.scrollTop = el.scrollHeight; }
      off = r.offset; onTick && onTick(r.job);
      if (["queued", "running"].includes(r.job.state)) setTimeout(tick, 800); else onDone && onDone(r.job);
    } catch (e) { if (e.status !== 401) setTimeout(tick, 2500); }
  };
  tick();
  return () => (stop = true);
}
function stepsList(steps) {
  return `<div class="clist">${steps.map((s) => `<div>${s.state === "ok" ? `<span class="dot d-ok">${ic("check")}</span>` : s.state === "failed" ? `<span class="dot d-bad">${ic("x")}</span>` : s.state === "running" ? `<span class="dot d-warn"></span>` : `<span class="dot d-mut"></span>`}<span>${esc(s.name)}</span></div>`).join("")}</div>`;
}
function hostOptions(hosts, selected = [], filterRole) {
  return hosts.filter((h) => !filterRole || h.roles.includes(filterRole) || selected.includes(h.id))
    .map((h) => `<option value="${h.id}" ${selected.includes(h.id) ? "selected" : ""}>${esc(h.hostname || h.ip)} (${esc(h.ip)}) ${esc(h.roles.join(","))} ${h.status === "ready" || h.status === "ready-with-warnings" ? "✓" : ""}</option>`).join("");
}

/* ---- layout: nav groups, active links, search, notifications, user menu ---- */
function markNav() {
  const p = location.pathname, h = location.hash.slice(1);
  $$("#nav .sub a").forEach((a) => {
    const u = new URL(a.href, location.origin);
    a.classList.toggle("act", u.pathname === p && (!a.dataset.hash || a.dataset.hash === h || (!h && a === $$(`#nav .sub a[href^="${p}"]`)[0])));
  });
}
document.addEventListener("DOMContentLoaded", () => {
  $$("#nav .grp > span").forEach((s) => s.addEventListener("click", () => s.parentElement.classList.toggle("open")));
  markNav();
  $("#burger")?.addEventListener("click", () => $("#layout").classList.toggle("collapsed"));
  const ub = $("#umenu-btn"); ub && ub.addEventListener("click", (e) => { if (!e.target.closest(".menu")) $("#umenu").classList.toggle("show"); });
  document.addEventListener("click", (e) => { if (!e.target.closest("#umenu-btn")) $("#umenu")?.classList.remove("show"); if (!e.target.closest("#bell") && !e.target.closest("#nmenu")) $("#nmenu")?.classList.remove("show"); if (!e.target.closest(".search")) $("#gres") && ($("#gres").style.display = "none"); });
  let st; const gs = $("#gsearch");
  gs && gs.addEventListener("input", () => { clearTimeout(st); st = setTimeout(async () => {
    const q = gs.value.trim(); const res = $("#gres"); if (q.length < 2) { res.style.display = "none"; return; }
    const r = await GET("/api/search?q=" + encodeURIComponent(q));
    res.innerHTML = r.results.length ? r.results.map((x) => `<a href="${esc(x.url)}"><small>${esc(x.type)}</small>${esc(x.label)}</a>`).join("") : `<div class="empty">No matches</div>`;
    res.style.display = "block"; }, 250); });
  const bell = $("#bell");
  const loadN = async () => { try { const n = await GET("/api/notifications"); const b = $("#nbadge"); b.hidden = !n.count; b.textContent = n.count;
    $("#nmenu").innerHTML = (n.alerts.length ? n.alerts.map((a) => `<div>${pill(a.severity)} ${esc(a.message)}<br><small class="muted">${ago(a.ts)}</small></div>`).join("") : `<div class="muted">No new alerts</div>`)
      + (n.running_jobs ? `<div><a href="/jobs">${n.running_jobs} job(s) running</a></div>` : "") + `<div><a href="/monitoring#alerts">All alerts</a></div>`; } catch (e) {} };
  if (bell) { bell.addEventListener("click", () => $("#nmenu").classList.toggle("show")); loadN(); setInterval(loadN, 30000); }
  $$("[data-need]").forEach((el) => { if (!can(el.dataset.need)) { el.disabled = true; el.title = `${el.dataset.need} role required`; } });
  const page = document.body.dataset.page; const fn = window["page_" + page]; if (fn) fn().catch?.(fail);
});
