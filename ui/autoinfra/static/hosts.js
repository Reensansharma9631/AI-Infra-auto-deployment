/* Pre-Deployment + Host Management pages */
"use strict";
let HOSTS = [];
const ROLE_SHORT = { management: "Management", database: "Database", compute: "Compute", storage: "Storage", monitoring: "Monitoring", lb: "LB", mq: "MQ",
  controller: "OS Controller", network: "OS Network", "os-compute": "OS Compute", "os-storage": "OS Storage", ai: "AI", app: "App" };
const roleTxt = (h) => esc(h.roles.map((r) => ROLE_SHORT[r] || r).join(", ") || "-");

function bindHostModals(reload) {
  $("#b-add").addEventListener("click", async () => {
    const f = $("#f-add"), d = formData(f); d.roles = $$(".role-cb", f).filter((c) => c.checked).map((c) => c.value);
    if (!d.password) delete d.password;
    try { const r = await POST("/api/hosts", d); toast(r.message, r.message.includes("rejected") || r.message.includes("not reachable") ? "bad" : "ok");
      f.reset(); showErrors(f, {}); closeModal("m-add"); reload(); } catch (e) { showErrors(f, e.data?.fields || { ip: e.message }); }
  });
  $("#csv-file").addEventListener("change", async (e) => { $("#csv-text").value = await e.target.files[0].text(); });
  $("#b-csv").addEventListener("click", async () => {
    try { const r = await POST("/api/hosts/import", { csv: $("#csv-text").value });
      $("#csv-out").innerHTML = `<div class="okbox">${r.added.length} host(s) added</div>` + (r.errors.length ? `<div class="note mt">${r.errors.map((x) => `line ${x.line}: ${esc(Object.values(x.errors).join("; "))}`).join("<br>")}</div>` : "");
      reload(); } catch (e) { $("#csv-out").innerHTML = `<div class="note">${esc(e.message)}</div>`; }
  });
}

async function showDetails(id) {
  const r = await GET(`/api/hosts/${id}`); const h = r.host, f = r.facts;
  $("#d-title").firstChild.textContent = `${h.hostname || h.ip} (${h.ip}) `;
  $("#d-body").innerHTML = `<div class="grid g2"><div><h2>System information</h2><div class="kv">
    <div>OS</div><div>${esc(h.os_pretty || "-")} ${h.os_support ? pill(h.os_support === "supported" ? "ok" : h.os_support === "compat" ? "warn" : "fail", h.os_support) : ""}</div>
    <div>Family / package manager</div><div>${esc(h.os_family || "-")} / ${esc(f.PKG || "-")}</div><div>Kernel</div><div>${esc(h.kernel || "-")}</div>
    <div>CPU cores</div><div>${esc(f.CPUS || "-")} (vmx/svm flags: ${esc(f.VIRT_FLAGS || "0")}, /dev/kvm: ${f.KVM_DEV === "1" ? "yes" : "no"})</div>
    <div>Memory</div><div>${esc(f.MEM_MB || "-")} MB</div><div>Disk /</div><div>${esc(f.DISK_FREE_GB || "-")} GB free of ${esc(f.DISK_TOTAL_GB || "-")} GB</div>
    <div>Python</div><div>${esc(f.PYTHON || "-")}</div><div>Updates pending</div><div>${h.updates >= 0 ? h.updates : "-"}</div>
    <div>Firewall</div><div>${esc(f.FW || "-")}</div><div>SELinux / AppArmor</div><div>${esc(f.SELINUX || "-")} / ${f.APPARMOR ? "yes" : "-"}</div>
    <div>Interfaces</div><div class="mono">${esc((f.IFACES || []).join(" · "))}</div><div>Last check</div><div>${fmtTs(h.last_check)}</div></div></div>
    <div><h2>Checks</h2>${table([{ t: "Check", k: "name" }, { t: "Result", f: (c) => pill(c.status) }, { t: "Detail", k: "detail" }], r.checks, "No checks yet")}</div></div>`;
  openModal("m-detail");
}

window.page_predeploy = async function () {
  let selected = new Set(), stopLog = null;
  initTabs(document, (t) => { if (t === "required") loadRequired(); });
  bindHostModals(load);
  const gsel = $("#pd-group");
  gsel.addEventListener("change", load);
  async function load() {
    const [r, g] = await Promise.all([GET("/api/hosts" + (gsel.value ? "?group=" + encodeURIComponent(gsel.value) : "")), GET("/api/hosts/groups")]);
    HOSTS = r.hosts; const s = r.stats;
    if (gsel.options.length - 1 !== g.groups.length) { const cur = gsel.value; gsel.innerHTML = `<option value="">All groups</option>` + g.groups.map((x) => `<option>${esc(x.name)}</option>`).join(""); gsel.value = cur; }
    $("#pd-tiles").innerHTML = tile("server", "c-blue", s.total, "Total Hosts") + tile("check", "c-green", s.reachable, "Reachable") + tile("x", "c-red", s.unreachable, "Unreachable")
      + tile("key", "c-orange", s.auth_failed, "Auth Failed") + tile("shield", "c-teal", s.root_ok, "Root/Sudo OK") + tile("refresh", "c-purple", s.needs_update, "Needs Update");
    const step = !s.total ? 0 : s.ready === s.total ? 5 : s.reachable ? 2 : 1;
    $$("#pd-steps .st").forEach((el, i) => { el.classList.toggle("done", i < step); el.classList.toggle("act", i === step); });
    $("#pd-table").innerHTML = table([
      { t: "", f: (h) => `<input type="checkbox" class="sel" value="${h.id}" ${selected.has(h.id) ? "checked" : ""}>` }, { t: "#", f: (h, i) => i + 1, cls: "n" },
      { t: "Hostname", f: (h) => esc(h.hostname || "-") }, { t: "IP Address", k: "ip" }, { t: "Role", f: roleTxt }, { t: "OS", f: (h) => esc(h.os_short || "-") },
      { t: "Ping", f: (h) => dot(h.ping === "ok" ? "ok" : h.ping ? "bad" : "") }, { t: "SSH", f: (h) => dot(h.ssh === "ok" ? "ok" : h.ssh ? "bad" : "") },
      { t: "Root/Sudo", f: (h) => dot(h.root === "ok" ? "ok" : h.root ? "bad" : "") },
      { t: "Update", f: (h) => h.updates > 0 ? pill("warn", "Update Available") : h.updates === 0 ? pill("ok", "Updated") : "-" },
      { t: "Status", f: (h) => pill(h.status) }, { t: "Actions", f: (h) => `<button class="btn sm o" data-d="${h.id}">Details</button>` }], HOSTS, "No hosts yet - click Add Host or Bulk Import (CSV)");
    $$(".sel").forEach((c) => c.addEventListener("change", () => (c.checked ? selected.add(+c.value) : selected.delete(+c.value))));
    $$("[data-d]").forEach((b) => b.addEventListener("click", () => showDetails(+b.dataset.d)));
    $("#pd-upd").innerHTML = table([{ t: "Host", f: (h) => esc(h.hostname || h.ip) }, { t: "OS", f: (h) => esc(h.os_short || "-") }, { t: "Pending updates", f: (h) => h.updates >= 0 ? h.updates : "run checks" }], HOSTS);
  }
  const ids = () => [...selected];
  async function runChecks(all) {
    const f = $("#f-test"), d = formData(f); if (!d.password) delete d.password;
    if (!all && ids().length) d.host_ids = ids(); else if (gsel.value) d.group = gsel.value;
    try { const r = await POST("/api/checks/run", d); showErrors(f, {}); $('[data-tabs="pd"]')._show("logs", true);
      $("#log-title").textContent = `Connectivity Log - job #${r.job_id}`; const dl = $("#log-dl"); dl.hidden = false; dl.href = `/api/jobs/${r.job_id}/log.txt`;
      stopLog && stopLog(); stopLog = watchJob(r.job_id, $("#pd-log"), () => { load(); toast("checks finished", "ok"); }, () => load());
    } catch (e) { showErrors(f, e.data?.fields); fail(e); }
  }
  $("#b-test").addEventListener("click", () => runChecks(false));
  $("#b-runall").addEventListener("click", () => runChecks(true));
  const pkg = async (action) => { try { const r = await POST("/api/hosts/packages", { action, host_ids: ids().length ? ids() : undefined });
    $('[data-tabs="pd"]')._show("logs", true); $("#log-title").textContent = `Package ${action} - job #${r.job_id}`; stopLog && stopLog(); stopLog = watchJob(r.job_id, $("#pd-log"), load); } catch (e) { fail(e); } };
  $("#b-refresh").addEventListener("click", () => pkg("refresh"));
  $("#b-upgrade").addEventListener("click", () => { if (confirm("Upgrade all packages on the selected hosts from the offline repository?")) pkg("upgrade"); });
  async function loadRequired() {
    const r = await GET("/api/hosts/required");
    $("#pd-req").innerHTML = table([{ t: "Host", k: "host" }, { t: "Role", k: "role" }, { t: "Package", f: (x) => `<span class="mono">${esc(x.package)}</span>` },
      { t: "In master repository", f: (x) => x.in_repo ? pill("ok", "Available") : pill("bad", "Missing") }], r.required, "Give hosts roles to see what they need");
  }
  await load();
};

window.page_hosts = async function () {
  initTabs(document, (t) => { if (t === "groups") loadGroups(); if (t === "key") GET("/api/ssh/pubkey").then((r) => ($("#hm-key").value = r.pubkey)); });
  bindHostModals(load);
  let qt; $("#hm-q").addEventListener("input", () => { clearTimeout(qt); qt = setTimeout(load, 250); });
  $("#hm-role").addEventListener("change", load);
  async function load() {
    const q = $("#hm-q").value.trim(), role = $("#hm-role").value;
    const r = await GET(`/api/hosts?${q ? "q=" + encodeURIComponent(q) + "&" : ""}${role ? "role=" + role : ""}`); HOSTS = r.hosts;
    $("#hm-table").innerHTML = table([{ t: "#", k: "id", cls: "n" }, { t: "Hostname", f: (h) => `<span id="host-${h.id}">${esc(h.hostname || "-")}</span>` }, { t: "IP", k: "ip" },
      { t: "SSH", f: (h) => `${esc(h.ssh_user)}@:${h.ssh_port} ${h.key_ok ? pill("ok", "key") : pill("mut", "no key")}` }, { t: "Roles", f: roleTxt }, { t: "Group", k: "grp" },
      { t: "OS", f: (h) => esc(h.os_short || "-") }, { t: "Status", f: (h) => pill(h.status) }, { t: "Last check", f: (h) => ago(h.last_check) },
      { t: "", f: (h) => `<div class="flex nowrap"><button class="btn sm o" data-d="${h.id}">Details</button><button class="btn sm g" data-e="${h.id}" ${can("operator") ? "" : "disabled"}>Edit</button><button class="btn sm g" data-x="${h.id}" ${can("operator") ? "" : "disabled"}>${ic("trash")}</button></div>` }],
      HOSTS, "No hosts");
    $$("[data-d]").forEach((b) => b.addEventListener("click", () => showDetails(+b.dataset.d)));
    $$("[data-x]").forEach((b) => b.addEventListener("click", async () => { const h = HOSTS.find((x) => x.id === +b.dataset.x); if (!confirm(`Remove ${h.ip} from the inventory? (nothing is changed on the host)`)) return; await api("DELETE", `/api/hosts/${h.id}`); toast("removed", "ok"); load(); }));
    $$("[data-e]").forEach((b) => b.addEventListener("click", () => { const h = HOSTS.find((x) => x.id === +b.dataset.e); const f = $("#f-edit"); fillForm(f, h); $$(".erole-cb", f).forEach((c) => (c.checked = h.roles.includes(c.value))); if ($("#e-vars")) $("#e-vars").value = Object.keys(h.vars || {}).length ? JSON.stringify(h.vars, null, 1) : ""; openModal("m-edit"); }));
  }
  $("#b-edit").addEventListener("click", async () => {
    const f = $("#f-edit"), d = formData(f); d.roles = $$(".erole-cb", f).filter((c) => c.checked).map((c) => c.value); const id = d.id; delete d.id;
    if ($("#e-vars")) { try { d.vars = $("#e-vars").value.trim() ? JSON.parse($("#e-vars").value) : {}; } catch (e) { return showErrors(f, { vars: "not valid JSON" }); } }
    try { await api("PATCH", `/api/hosts/${id}`, d); closeModal("m-edit"); toast("saved", "ok"); load(); } catch (e) { showErrors(f, e.data?.fields); }
  });
  async function loadGroups() { const g = await GET("/api/hosts/groups"); $("#hm-groups").innerHTML = table([{ t: "Group", k: "name" }, { t: "Hosts", k: "hosts" }, { t: "", f: (x) => `<a href="/predeploy#test">Run checks</a>` }], g.groups); }
  await load();
};
