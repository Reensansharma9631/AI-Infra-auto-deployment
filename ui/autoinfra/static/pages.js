/* Dashboard, CloudStack infra, Components, Monitoring, AI, Reports, Billing, Jobs, Audit, Settings */
"use strict";
const CAT_ICON = { server: "server", chart: "chart", chip: "chip", layers: "layers", db: "db", queue: "queue", globe: "globe", split: "split", disk: "disk", pulse: "pulse", spark: "spark" };

window.page_dashboard = async function () {
  const d = await GET("/api/dashboard"), o = d.overview;
  $("#tiles").innerHTML = tile("server", "c-blue", o.hosts.total, "Hosts", `${o.hosts.online} online · ${o.hosts.offline} offline`) +
    tile("check", "c-green", d.hosts.ready, "Ready for deployment") + tile("box", "c-purple", d.packages.items, "Packages in repository", fmtBytes(d.packages.bytes)) +
    tile("queue", "c-teal", `${d.components.installed}/${d.components.total}`, "Components installed", d.components.failed ? `${d.components.failed} failed` : "") +
    tile("cloud", "c-orange", o.vms.total, "Virtual machines", `${o.vms.running} running`) + tile("alert", "c-red", o.alerts.total, "Open alerts", `${o.alerts.critical} critical`);
  const flow = [["Upload packages", d.packages.items > 0], ["Add hosts", d.hosts.total > 0], ["Checks passed", d.hosts.ready > 0 && d.hosts.ready === d.hosts.total],
    ["Deploy", d.components.installed > 0], ["Monitor", o.samples > 0]];
  const cur = flow.findIndex((f) => !f[1]);
  $("#flow").innerHTML = flow.map((f, i) => `<div class="st ${f[1] ? "done" : i === cur ? "act" : ""}"><b>${f[1] ? "✓" : i + 1}</b>${esc(f[0])}</div>`).join("");
  $("#alerts").innerHTML = table([{ t: "Severity", f: (a) => pill(a.severity) }, { t: "Message", k: "message" }, { t: "When", f: (a) => ago(a.last_seen) }], d.alerts, "No open alerts");
  $("#jobs").innerHTML = jobsTable(d.jobs);
};
function jobsTable(jobs) {
  return table([{ t: "#", k: "id", cls: "n" }, { t: "Job", f: (j) => `<a href="/jobs#job-${j.id}">${esc(j.title)}</a>` }, { t: "State", f: (j) => pill(j.state) },
    { t: "Progress", f: (j) => `<div class="bar ${j.state === "failed" ? "bad" : j.state === "success" ? "ok" : ""}"><i style="width:${j.progress}%"></i></div>` },
    { t: "By", k: "by_user" }, { t: "Started", f: (j) => fmtTs(j.started || j.created) }], jobs, "No jobs yet");
}

/* ---------------- CloudStack infrastructure ---------------- */
window.page_csinfra = async function () {
  const FORMS = {
    zones: ["Add Zone", [["name", "Zone name"], ["dns1", "DNS 1"], ["internaldns1", "Internal DNS"], ["guestcidraddress", "Guest CIDR", "10.1.1.0/24"]]],
    pods: ["Add Pod", [["zoneid", "Zone", "zone"], ["name", "Pod name"], ["gateway", "Gateway"], ["netmask", "Netmask", "255.255.255.0"], ["startip", "Start IP"], ["endip", "End IP"]]],
    clusters: ["Add Cluster", [["zoneid", "Zone", "zone"], ["podid", "Pod", "pod"], ["clustername", "Cluster name"]]],
    hosts: ["Add KVM Host", [["zoneid", "Zone", "zone"], ["podid", "Pod", "pod"], ["clusterid", "Cluster", "cluster"], ["url", "URL", "http://10.0.1.21"], ["password", "Root password (optional with mgmt SSH key)", "", "password"]]],
    storagepools: ["Add Primary Storage", [["zoneid", "Zone", "zone"], ["podid", "Pod", "pod"], ["clusterid", "Cluster", "cluster"], ["name", "Name"], ["url", "URL", "nfs://10.0.0.31/export/primary"]]],
    imagestores: ["Add Secondary Storage", [["zoneid", "Zone", "zone"], ["name", "Name"], ["url", "URL", "nfs://10.0.0.31/export/secondary"]]],
    vlanipranges: ["Add Public IP range", [["zoneid", "Zone", "zone"], ["vlan", "VLAN", "untagged"], ["gateway", "Gateway"], ["netmask", "Netmask", "255.255.255.0"], ["startip", "Start IP"], ["endip", "End IP"]]],
    serviceofferings: ["Add Service Offering", [["name", "Name"], ["displaytext", "Description"], ["cpunumber", "vCPU", "2"], ["cpuspeed", "CPU MHz", "2000"], ["memory", "Memory MB", "4096"]]],
  };
  let D = {};
  initTabs(document, () => {});
  const sum = await GET("/api/cs/summary");
  if (!sum.configured) $("#cs-cfg").innerHTML = `<div class="note" style="margin-bottom:14px">CloudStack API not reachable: ${esc(sum.error || "")}. Deploy with the <a href="/cloudstack/wizard">wizard</a> or set the API in <a href="/settings#integrations">Settings</a>.</div>`;
  $("#cs-tiles").innerHTML = tile("globe", "c-blue", sum.zones ?? 0, "Total Zones") + tile("layers", "c-green", sum.pods ?? 0, "Total Pods") + tile("queue", "c-purple", sum.clusters ?? 0, "Total Clusters")
    + tile("server", "c-blue", sum.hosts ?? 0, "KVM Hosts") + tile("disk", "c-orange", sum.storagepools ?? 0, "Primary Storage", fmtBytes(sum.primary_bytes || 0)) + tile("disk", "c-gray", sum.imagestores ?? 0, "Secondary Storage");
  const act = (res, id, list) => list.map(([a, l]) => `<button class="btn sm g" data-act="${res}|${esc(id)}|${a}" ${can(a === "delete" || a === "destroy" ? "admin" : "operator") ? "" : "disabled"}>${l}</button>`).join(" ");
  const L = async (res, q = "") => { try { const r = await GET(`/api/cs/${res}${q}`); D[res] = r.items; return r.items; } catch (e) { D[res] = []; return []; } };
  const zname = (id) => (D.zones || []).find((z) => z.id === id)?.name || "";
  async function all() {
    await L("zones");
    const [pods, clusters, hosts, sp, is, pn, vl, nw, svm, rt, tp, so] = await Promise.all(["pods", "clusters", "hosts", "storagepools", "imagestores", "physicalnetworks", "vlanipranges", "networks", "systemvms", "routers", "templates", "serviceofferings"].map((r) => L(r)));
    const T = (id, cols, rows, empty) => ($(id).innerHTML = table(cols, rows, empty));
    T("#t-zones", [{ t: "#", f: (x, i) => i + 1, cls: "n" }, { t: "Name", k: "name" }, { t: "Type", k: "networktype" }, { t: "Network", f: (x) => esc(x.guestcidraddress || "") }, { t: "Status", f: (x) => pill(x.allocationstate) },
      { t: "Actions", f: (x) => act("zones", x.id, x.allocationstate === "Enabled" ? [["disable", "Disable"]] : [["enable", "Enable"], ["delete", "Delete"]]) }], D.zones, "No zones");
    const podCols = [{ t: "#", f: (x, i) => i + 1, cls: "n" }, { t: "Name", k: "name" }, { t: "Zone", f: (x) => esc(zname(x.zoneid)) }, { t: "IP Range", f: (x) => esc(x.startip ? `${x.startip[0] || x.startip} - ${x.endip?.[0] || x.endip || ""}` : (x.gateway || "")) },
      { t: "Status", f: (x) => pill(x.allocationstate || "Enabled") }, { t: "Actions", f: (x) => act("pods", x.id, [["disable", "Disable"], ["delete", "Delete"]]) }];
    T("#t-pods", podCols, pods, "No pods"); T("#t-pods2", podCols, pods, "No pods");
    T("#t-clusters", [{ t: "#", f: (x, i) => i + 1, cls: "n" }, { t: "Name", k: "name" }, { t: "Zone", f: (x) => esc(zname(x.zoneid)) }, { t: "Hypervisor", f: (x) => esc(x.hypervisortype || "KVM") }, { t: "Status", f: (x) => pill(x.allocationstate || "Enabled") },
      { t: "Actions", f: (x) => act("clusters", x.id, [["disable", "Disable"], ["enable", "Enable"]]) }], clusters, "No clusters");
    T("#t-hosts", [{ t: "#", f: (x, i) => i + 1, cls: "n" }, { t: "Hostname", k: "name" }, { t: "IP Address", f: (x) => esc(x.ipaddress || "") }, { t: "Cluster", f: (x) => esc(x.clustername || (clusters.find((c) => c.id === x.clusterid) || {}).name || "") },
      { t: "Status", f: (x) => pill(x.resourcestate === "Maintenance" ? "Maintenance" : x.state) }, { t: "CPU", f: (x) => esc(x.cpunumber || "") }, { t: "Memory", f: (x) => x.memorytotal ? fmtBytes(x.memorytotal) : "" },
      { t: "Actions", f: (x) => act("hosts", x.id, x.resourcestate === "Maintenance" ? [["cancel-maintenance", "Cancel maint."]] : [["maintenance", "Maintenance"]]) }], hosts, "No KVM hosts");
    T("#t-storagepools", [{ t: "Name", k: "name" }, { t: "Type", f: (x) => esc(x.type || "NFS") }, { t: "Path", f: (x) => `<span class="mono">${esc(x.path || x.ipaddress || "")}</span>` }, { t: "Capacity", f: (x) => x.disksizetotal ? fmtBytes(x.disksizetotal) : "" },
      { t: "Used", f: (x) => x.disksizeused ? fmtBytes(x.disksizeused) : "" }, { t: "Status", f: (x) => pill(x.state || "Up") }, { t: "", f: (x) => act("storagepools", x.id, [["maintenance", "Maint."]]) }], sp, "No primary storage");
    T("#t-imagestores", [{ t: "Name", k: "name" }, { t: "Provider", f: (x) => esc(x.providername || "NFS") }, { t: "URL", f: (x) => `<span class="mono">${esc(x.url || "")}</span>` }, { t: "Zone", f: (x) => esc(zname(x.zoneid)) }], is, "No secondary storage");
    T("#t-physicalnetworks", [{ t: "Name", k: "name" }, { t: "Zone", f: (x) => esc(zname(x.zoneid)) }, { t: "VLAN", f: (x) => esc(x.vlan || "") }, { t: "Status", f: (x) => pill(x.state) }], pn, "None");
    T("#t-vlanipranges", [{ t: "VLAN", f: (x) => esc(x.vlan || "untagged") }, { t: "Gateway", f: (x) => esc(x.gateway || "") }, { t: "Range", f: (x) => esc(`${x.startip || ""} - ${x.endip || ""}`) }, { t: "Zone", f: (x) => esc(zname(x.zoneid)) }], vl, "No public ranges");
    T("#t-networks", [{ t: "Name", k: "name" }, { t: "Type", f: (x) => esc(x.type || "") }, { t: "CIDR", f: (x) => esc(x.cidr || "") }, { t: "Account", f: (x) => esc(x.account || "") }, { t: "State", f: (x) => pill(x.state) }], nw, "No guest networks");
    T("#t-systemvms", [{ t: "Name", k: "name" }, { t: "Type", f: (x) => esc(x.systemvmtype || "") }, { t: "State", f: (x) => pill(x.state) }, { t: "Public IP", f: (x) => esc(x.publicip || "") }, { t: "Host", f: (x) => esc(x.hostname || "") },
      { t: "Actions", f: (x) => act("systemvms", x.id, [["reboot", "Reboot"], ["stop", "Stop"], ["start", "Start"]]) }], svm, "No system VMs (they appear after the zone is enabled and the template is seeded)");
    T("#t-routers", [{ t: "Name", k: "name" }, { t: "State", f: (x) => pill(x.state) }, { t: "Network", f: (x) => esc(x.guestnetworkname || "") }, { t: "Version", f: (x) => esc(x.version || "") }], rt, "No virtual routers");
    T("#t-templates", [{ t: "Name", k: "name" }, { t: "OS", f: (x) => esc(x.ostypename || "") }, { t: "Format", f: (x) => esc(x.format || "") }, { t: "Size", f: (x) => x.size ? fmtBytes(x.size) : "" }, { t: "Ready", f: (x) => pill(x.isready ? "ok" : "warn", x.isready ? "Ready" : x.status || "Not ready") }], tp, "No templates");
    T("#t-serviceofferings", [{ t: "Name", k: "name" }, { t: "vCPU", f: (x) => esc(x.cpunumber || "") }, { t: "MHz", f: (x) => esc(x.cpuspeed || "") }, { t: "Memory", f: (x) => `${esc(x.memory || "")} MB` }, { t: "", f: (x) => act("serviceofferings", x.id, [["delete", "Delete"]]) }], so, "No offerings");
    $$("[data-act]").forEach((b) => b.addEventListener("click", async () => { const [r, id, a] = b.dataset.act.split("|"); if (["delete", "destroy", "maintenance"].includes(a) && !confirm(`${a} this ${r.replace(/s$/, "")}?`)) return;
      try { await POST(`/api/cs/${r}/${id}/${a}`); toast(`${a} OK`, "ok"); all(); } catch (e) { fail(e); } }));
  }
  let curRes = "";
  $$("[data-add]").forEach((b) => b.addEventListener("click", () => {
    curRes = b.dataset.add; const [title, fields] = FORMS[curRes]; $("#m-cs-t").firstChild.textContent = title + " ";
    const opt = (list) => (D[list + "s"] || []).map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("");
    $("#f-cs").innerHTML = `<div class="errs"></div>` + fields.map(([n, l, ph, type]) => `<div class="f"><label>${esc(l)}</label>${["zone", "pod", "cluster"].includes(ph) ? `<select name="${n}">${opt(ph)}</select>` : `<input type="${type || "text"}" name="${n}" placeholder="${esc(ph || "")}">`}</div>`).join("");
    openModal("m-cs");
  }));
  $("#b-cs").addEventListener("click", async () => { const f = $("#f-cs"), d = formData(f); Object.keys(d).forEach((k) => d[k] === "" && delete d[k]);
    try { await POST(`/api/cs/${curRes}`, d); closeModal("m-cs"); toast("created", "ok"); all(); } catch (e) { showErrors(f, e.data?.fields || { x: e.message }); } });
  if (sum.configured) await all();
  else ["zones", "pods", "pods2", "clusters", "hosts", "storagepools", "imagestores", "physicalnetworks", "vlanipranges", "networks", "systemvms", "routers", "templates", "serviceofferings"]
    .forEach((r) => ($("#t-" + r).innerHTML = `<div class="empty">CloudStack API not configured</div>`));
};

/* ---------------- Components ---------------- */
window.page_components = async function () {
  let C = [], HOSTS = [], cur = null, cat = "all";
  initTabs(document, (t) => { cat = t; render(); });
  async function load() { const [r, h] = await Promise.all([GET("/api/components"), GET("/api/hosts")]); C = r.components; HOSTS = h.hosts;
    $("#actions").innerHTML = table([{ t: "Time", f: (a) => fmtTs(a.ts) }, { t: "Component", k: "component_name" }, { t: "Action", k: "action" }, { t: "Status", f: (a) => pill(a.status === "Completed" ? "ok" : a.status === "Failed" ? "failed" : "running", a.status) },
      { t: "Message", k: "message" }, { t: "", f: (a) => a.job_id ? `<a href="/jobs#job-${a.job_id}">log</a>` : "" }], r.actions, "No actions yet"); render(); }
  function render() {
    $("#cards").innerHTML = C.filter((c) => cat === "all" || c.cat === cat).map((c) => `<div class="cc" id="${esc(c.id)}"><div class="hd"><div class="lg" style="background:${esc(c.color)}">${ic(CAT_ICON[c.icon] || "box")}</div>
      <div><b>${esc(c.name)}</b><br><small>v${esc(c.version)}</small></div></div><div>${pill(c.state)}</div><small>Source: ${esc(c.source)} · hosts: ${c.hosts.length}</small>
      <div class="act"><button class="btn sm g" data-cfg="${esc(c.id)}">Configure</button><button class="btn sm" data-dep="${esc(c.id)}" ${can("operator") ? "" : "disabled"}>${c.state === "installed" || c.state === "running" ? "Manage" : "Deploy"}</button></div></div>`).join("");
    $$("[data-cfg],[data-dep]").forEach((b) => b.addEventListener("click", () => open(b.dataset.cfg || b.dataset.dep)));
  }
  function open(id) {
    cur = C.find((c) => c.id === id); $("#mc-title").firstChild.textContent = cur.name + " ";
    $("#mc-fields").innerHTML = Object.entries(cur.fields).map(([k, f]) => f.type === "bool" ? `<label class="chk"><input type="checkbox" name="${k}" ${cur.config[k] ? "checked" : ""}> ${esc(f.label || k)}</label>` :
      f.type === "choice" ? `<div class="f"><label>${esc(f.label || k)}</label><select name="${k}">${f.options.map((o) => `<option ${o === cur.config[k] ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></div>` :
        `<div class="f"><label>${esc(f.label || k)}</label><input type="${f.type === "int" ? "number" : "text"}" name="${k}" value="${esc(cur.config[k])}" ${f.type === "int" ? "data-num" : ""}></div>`).join("") || `<p class="muted">No settings.</p>`;
    $('[name="__version"]').value = cur.version;
    $("#mc-hosts").innerHTML = hostOptions(HOSTS, cur.hosts.length ? cur.hosts : HOSTS.filter((h) => cur.role && h.roles.includes(cur.role)).map((h) => h.id));
    $("#mc-hint").textContent = `${cur.min}-${cur.max} host(s)${cur.role ? `; hosts with role "${cur.role}" are pre-selected` : ""}. ✓ = passed the pre-deployment checks.`;
    $("#mc-job").hidden = true; showErrors($("#mc-form"), {}); openModal("m-comp");
  }
  const cfg = () => { const d = formData($("#mc-form")); delete d.__version; return d; };
  $("#mc-save").addEventListener("click", async () => { try { await POST(`/api/components/${cur.id}/config`, cfg()); toast("configuration saved", "ok"); load(); } catch (e) { showErrors($("#mc-form"), e.data?.fields); fail(e); } });
  $("#mc-deploy").addEventListener("click", async () => {
    const ids = $$("#mc-hosts option:checked").map((o) => +o.value);
    try { const r = await POST(`/api/components/${cur.id}/deploy`, { host_ids: ids, config: cfg(), version: $('[name="__version"]').value, check: $("#mc-check").checked, force: $("#mc-force").checked });
      $("#mc-job").hidden = false; $("#mc-jt").textContent = `#${r.job_id}`; watchJob(r.job_id, $("#mc-log"), (j) => { toast(`${cur.name}: ${j.state}`, j.state === "success" ? "ok" : "bad"); load(); });
    } catch (e) { showErrors($("#mc-form"), e.data?.fields); fail(e); }
  });
  $("#mc-status").addEventListener("click", async () => { try { const r = await POST(`/api/components/${cur.id}/status`); $("#mc-job").hidden = false; watchJob(r.job_id, $("#mc-log"), load); } catch (e) { fail(e); } });
  await load();
  const h = location.hash.slice(1); if (C.find((c) => c.id === h)) open(h);
};

/* ---------------- Monitoring ---------------- */
function gauge(v, label, color) {
  const pct = Math.max(0, Math.min(100, +v || 0)), r = 44, c = 2 * Math.PI * r;
  return `<div class="gauge"><svg viewBox="0 0 110 110"><circle cx="55" cy="55" r="${r}" stroke="#e8edf4" stroke-width="10" fill="none"/>
    <circle cx="55" cy="55" r="${r}" stroke="${color}" stroke-width="10" fill="none" stroke-linecap="round" stroke-dasharray="${(c * pct) / 100} ${c}" transform="rotate(-90 55 55)"/>
    <text x="55" y="61" text-anchor="middle" font-size="20" font-weight="700" fill="#14213d">${Math.round(pct)}%</text></svg><div class="gt">${esc(label)}</div></div>`;
}
function spark(series, key, color) {
  if (!series.length) return "";
  const w = 300, h = 60, xs = series.map((s) => s.ts), mn = Math.min(...xs), mx = Math.max(...xs) || 1;
  const pts = series.map((s) => `${((s.ts - mn) / Math.max(1, mx - mn)) * w},${h - ((+s[key] || 0) / 100) * h}`).join(" ");
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:60px"><polyline fill="none" stroke="${color}" stroke-width="2" points="${pts}"/></svg>`;
}
function chatBox(msgsEl, formEl, inputEl, quickEl) {
  const add = (role, text) => { msgsEl.insertAdjacentHTML("beforeend", `<div class="msg ${role}">${esc(text)}</div>`); msgsEl.scrollTop = msgsEl.scrollHeight; };
  const send = async (text) => { if (!text) return; add("user", text); inputEl.value = ""; add("assistant", "…");
    const ph = msgsEl.lastElementChild;
    try { const r = await POST("/api/ai/chat", { message: text, model: $("#ai-model")?.value || undefined });
      ph.textContent = r.answer; if (r.report) ph.insertAdjacentHTML("beforeend", `<br><a href="${esc(r.report.url)}">Download ${esc(r.report.fmt.toUpperCase())}</a>`); }
    catch (e) { ph.textContent = e.message; } msgsEl.scrollTop = msgsEl.scrollHeight; };
  formEl.addEventListener("submit", (e) => { e.preventDefault(); send(inputEl.value.trim()); });
  quickEl && $$("button", quickEl).forEach((b) => b.addEventListener("click", () => send(b.textContent)));
  return { add };
}
window.page_monitoring = async function () {
  initTabs(document, (t) => ({ hosts: hostsTab, vms: vmsTab, storage: stoTab, databases: dbTab, logs: logsTab, alerts: alertsTab, zabbix: zbxTab, topology: topoTab }[t] || (() => {}))());
  chatBox($("#m-msgs"), $("#m-chat"), $("#m-in"), $("#mq"));
  async function overview() {
    const o = await GET("/api/monitor/overview");
    $("#mon-tiles").innerHTML = tile("server", "c-blue", o.hosts.total, "Total Hosts", `${o.hosts.online} online · ${o.hosts.offline} offline`) + tile("cloud", "c-green", o.vms.total, "Virtual Machines", `${o.vms.running} running · ${o.vms.stopped} stopped`)
      + tile("db", "c-purple", o.databases.total, "Databases", `${o.databases.healthy} healthy · ${o.databases.warning} warning`) + tile("disk", "c-orange", fmtBytes(o.storage.total_bytes), "Total Storage", o.storage.used_bytes ? fmtBytes(o.storage.used_bytes) + " used" : o.storage.source)
      + tile("alert", "c-red", o.alerts.total, "Active Alerts", `${o.alerts.critical} critical · ${o.alerts.warning} warning`);
    $("#gauges").innerHTML = gauge(o.gauges.cpu, "CPU Usage", "#025cfc") + gauge(o.gauges.mem, "Memory Usage", "#16a34a") + gauge(o.gauges.disk, "Storage Usage", "#f59e0b") +
      `<div class="gauge"><div style="font-size:26px;font-weight:700;margin-top:34px">${fmtBytes(o.gauges.net_bytes_s)}/s</div><div class="gt">Network Traffic</div></div>`;
    const al = await GET("/api/alerts"); $("#mon-recent").innerHTML = table([{ t: "Severity", f: (a) => pill(a.severity) }, { t: "Alert", k: "message" }, { t: "Time", f: (a) => fmtTs(a.last_seen) }], al.alerts.slice(0, 6), "No open alerts");
    $("#mon-upd").textContent = `updated ${new Date().toLocaleTimeString()}`;
  }
  async function topoTab() {
    const t = await GET("/api/monitor/topology");
    const hostLi = (h) => `<li><span>${dot(["ready", "ready-with-warnings"].includes(h.status) ? "ok" : h.status === "new" ? "" : "bad")} ${esc(h.name)} (${esc(h.ip)})${h.cpu != null ? ` · cpu ${Math.round(h.cpu)}%` : ""}</span></li>`;
    const cs = t.cloudstack.map((z) => `<li><span class="zone">${ic("globe")} ${esc(z.name)} ${z.state ? pill(z.state) : ""}</span><ul>${z.pods.map((p) => `<li><span>${ic("layers")} ${esc(p.name)}</span><ul>${p.clusters.map((c) => `<li><span>${ic("queue")} ${esc(c.name)}</span><ul>${c.hosts.map((h) => h.ip ? hostLi(h.status ? h : { ...h, status: h.state === "Up" ? "ready" : "bad" }) : "").join("")}</ul></li>`).join("")}</ul></li>`).join("")}</ul></li>`).join("");
    const os = ["controller", "compute", "network", "storage"].filter((k) => t.openstack[k].length).map((k) => `<li><span>${esc(k)}</span><ul>${t.openstack[k].map(hostLi).join("")}</ul></li>`).join("");
    $("#topo").innerHTML = `<div class="tree"><div class="node"><b>Management / Master</b>${esc(t.master.ip)}</div></div><div class="topo"><ul>
      <li><span class="zone">${ic("cloud")} CloudStack (Management: ${esc(t.management.map((m) => m.ip).join(", ") || "-")})</span><ul>${cs || "<li class='muted'>not deployed yet</li>"}</ul></li>
      <li><span class="zone">${ic("layers")} OpenStack</span><ul>${os || "<li class='muted'>not deployed yet</li>"}</ul></li>
      <li><span class="zone">${ic("disk")} Storage</span><ul>${t.storage.map(hostLi).join("") || "<li class='muted'>no storage hosts</li>"}</ul></li></ul></div>`;
  }
  async function hostsTab() {
    const r = await GET("/api/monitor/hosts");
    const pc = (v, w, c) => v == null ? "-" : `<div class="flex nowrap"><div class="bar ${v >= c ? "bad" : v >= w ? "" : "ok"}" style="width:80px"><i style="width:${v}%"></i></div>${Math.round(v)}%</div>`;
    $("#mon-hosts").innerHTML = table([{ t: "Host", f: (h) => `<a href="#hosts" data-h="${h.id}">${esc(h.hostname || h.ip)}</a>` }, { t: "IP", k: "ip" }, { t: "Status", f: (h) => pill(h.status) }, { t: "CPU", f: (h) => pc(h.cpu, 80, 95) },
      { t: "Memory", f: (h) => pc(h.mem, 85, 95) }, { t: "Disk", f: (h) => pc(h.disk, 80, 90) }, { t: "Load", f: (h) => esc(h.load ?? "-") }, { t: "Sample", f: (h) => ago(h.ts) }], r.hosts, "No hosts");
    $$("[data-h]").forEach((a) => a.addEventListener("click", async (e) => { e.preventDefault(); const s = (await GET(`/api/monitor/metrics/${a.dataset.h}?hours=24`)).series;
      $("#mh-t").textContent = `Last 24 h - ${a.textContent}`; $("#mon-hist").innerHTML = s.length ? `<div class="grid g3"><div>CPU${spark(s, "cpu", "#025cfc")}</div><div>Memory${spark(s, "mem", "#16a34a")}</div><div>Disk${spark(s, "disk", "#f59e0b")}</div></div>` : "No samples yet"; }));
  }
  async function vmsTab() { try { const r = await GET("/api/cs/virtualmachines"); $("#mon-vms").innerHTML = table([{ t: "Name", k: "name" }, { t: "State", f: (v) => pill(v.state) }, { t: "vCPU", f: (v) => esc(v.cpunumber || "") }, { t: "Memory", f: (v) => `${esc(v.memory || "")} MB` }, { t: "Host", f: (v) => esc(v.hostname || "") }, { t: "Account", f: (v) => esc(v.account || "") }], r.items, "No VMs"); }
    catch (e) { $("#mon-vms").innerHTML = `<div class="note">${esc(e.message)}</div>`; } }
  async function stoTab() { try { const r = await GET("/api/cs/storagepools"); $("#mon-sto").innerHTML = table([{ t: "Name", k: "name" }, { t: "Type", f: (s) => esc(s.type || "") }, { t: "Total", f: (s) => fmtBytes(s.disksizetotal) }, { t: "Used", f: (s) => fmtBytes(s.disksizeused) }, { t: "State", f: (s) => pill(s.state) }], r.items, "No primary storage"); }
    catch (e) { $("#mon-sto").innerHTML = `<div class="note">${esc(e.message)}</div>`; } }
  async function dbTab() { const r = await GET("/api/components?cat=databases"); $("#mon-db").innerHTML = table([{ t: "Database", k: "name" }, { t: "Version", k: "version" }, { t: "State", f: (c) => pill(c.state) }, { t: "Hosts", f: (c) => c.hosts.length }, { t: "", f: (c) => `<a href="/components#${esc(c.id)}">manage</a>` }], r.components); }
  async function logsTab() { const q = $("#lg-q").value.trim(); const r = await GET(`/api/logs?limit=300${q ? "&q=" + encodeURIComponent(q) : ""}`);
    $("#mon-logs").innerHTML = table([{ t: "Time", f: (e) => fmtTs(e.ts) }, { t: "Type", f: (e) => pill("info", e.type) }, { t: "Level", f: (e) => pill(e.level === "error" ? "failed" : e.level) }, { t: "Message", k: "message" }, { t: "User", k: "user" }], r.events, "No events"); }
  let lt; $("#lg-q").addEventListener("input", () => { clearTimeout(lt); lt = setTimeout(logsTab, 300); });
  async function alertsTab() { const r = await GET(`/api/alerts?show=${$("#al-show").value}`);
    $("#mon-alerts").innerHTML = table([{ t: "Severity", f: (a) => pill(a.severity) }, { t: "Source", k: "source" }, { t: "Message", k: "message" }, { t: "First", f: (a) => fmtTs(a.ts) }, { t: "Last", f: (a) => ago(a.last_seen) },
      { t: "", f: (a) => a.resolved ? pill("ok", "Resolved") : `${a.ack ? pill("info", "Ack") : `<button class="btn sm g" data-ack="${a.id}">Ack</button>`} <button class="btn sm g" data-res="${a.id}">Resolve</button>` }], r.alerts, "No alerts");
    $$("[data-ack]").forEach((b) => b.addEventListener("click", async () => { await POST(`/api/alerts/${b.dataset.ack}/ack`); alertsTab(); }));
    $$("[data-res]").forEach((b) => b.addEventListener("click", async () => { await POST(`/api/alerts/${b.dataset.res}/resolve`); alertsTab(); overview(); })); }
  $("#al-show").addEventListener("change", alertsTab);
  async function zbxTab() { const r = await api("GET", "/api/zabbix/problems", undefined, { raw: true }); const d = r.data;
    $("#mon-zbx").innerHTML = !d.configured ? `<div class="note">Zabbix is not configured - deploy it in <a href="/components#monitoring">Components</a> and set URL + token in <a href="/settings#integrations">Settings</a>.</div>` :
      d.error ? `<div class="note">${esc(d.error)}</div>` : table([{ t: "Severity", f: (p) => pill(["high", "disaster"].includes(p.severity) ? "critical" : "warning", p.severity) }, { t: "Problem", k: "name" }, { t: "Since", f: (p) => fmtTs(p.clock) }], d.problems, "No problems"); }
  $("#mon-collect").addEventListener("click", async () => { const r = await POST("/api/monitor/collect"); toast(`collecting (job #${r.job_id})`); setTimeout(overview, 4000); });
  await overview(); setInterval(overview, 30000);
};

/* ---------------- AI ---------------- */
window.page_ai = async function () {
  initTabs(document, (t) => { if (t === "kb") kb(); });
  const m = await GET("/api/ai/models");
  $("#ai-model").innerHTML = (m.models.length ? m.models : [m.selected || "llama3.1:8b"]).map((x) => `<option ${x === m.selected ? "selected" : ""}>${esc(x)}</option>`).join("");
  $("#ai-status").innerHTML = m.ok ? pill("ok", `Ollama ${m.url}`) : pill("bad", m.error);
  const c = chatBox($("#ai-msgs"), $("#ai-form"), $("#ai-in"), $(".quick"));
  (await GET("/api/ai/history")).messages.forEach((x) => c.add(x.role, x.content));
  $("#ai-clear").addEventListener("click", async () => { await api("DELETE", "/api/ai/history"); $("#ai-msgs").innerHTML = ""; });
  async function kb() { const r = await GET("/api/ai/kb"); $("#kb-list").innerHTML = table([{ t: "Document", k: "name" }, { t: "Size", f: (d) => fmtBytes(d.size) }, { t: "Added", f: (d) => fmtTs(d.created) }, { t: "By", k: "by_user" },
    { t: "", f: (d) => `<button class="btn sm g" data-kd="${d.id}" ${can("operator") ? "" : "disabled"}>${ic("trash")}</button>` }], r.docs, "No documents");
    $$("[data-kd]").forEach((b) => b.addEventListener("click", async () => { await api("DELETE", `/api/ai/kb/${b.dataset.kd}`); kb(); })); }
  $("#kb-file").addEventListener("change", async (e) => { const fd = new FormData(); fd.append("file", e.target.files[0]); try { await api("POST", "/api/ai/kb", fd); toast("added", "ok"); kb(); } catch (er) { fail(er); } e.target.value = ""; });
  $$("[data-rep]").forEach((b) => b.addEventListener("click", async () => { const r = await POST("/api/reports", { kind: "infra", fmt: b.dataset.rep }); $("#ai-rep").innerHTML = `<div class="okbox">Report ready: <a href="${esc(r.url)}">download</a></div>`; }));
};

/* ---------------- Reports ---------------- */
window.page_reports = async function () {
  async function load() { const r = await GET("/api/reports"); $("#rp-list").innerHTML = table([{ t: "File", f: (x) => `${ic(x.fmt === "pdf" ? "file" : "list")} ${esc(x.name)}` }, { t: "Type", f: (x) => esc(r.kinds[x.kind] || x.kind) }, { t: "Size", f: (x) => fmtBytes(x.size) },
    { t: "Created", f: (x) => fmtTs(x.ts) }, { t: "By", k: "by_user" }, { t: "", f: (x) => `<a class="btn sm o" href="/api/reports/${x.id}/download">${ic("dl")} Download</a> <button class="btn sm g" data-rd="${x.id}" ${can("operator") ? "" : "disabled"}>${ic("trash")}</button>` }], r.reports, "No reports yet");
    $$("[data-rd]").forEach((b) => b.addEventListener("click", async () => { await api("DELETE", `/api/reports/${b.dataset.rd}`); load(); })); }
  $("#rp-go").addEventListener("click", async () => { try { await POST("/api/reports", { kind: $("#rp-kind").value, fmt: $("#rp-fmt").value }); toast("report generated", "ok"); load(); } catch (e) { fail(e); } });
  await load();
};

/* ---------------- Billing ---------------- */
window.page_billing = async function () {
  async function load() {
    const b = await GET("/api/billing/summary"), t = b.totals, cur = b.currency, m = (v) => `${cur} ${(+v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
    $("#bl-tiles").innerHTML = tile("wallet", "c-blue", m(t.total_month), "Total monthly cost") + tile("cloud", "c-green", m(t.vm_month), "Compute (running VMs)") + tile("disk", "c-orange", m(t.storage_month), "Storage")
      + tile("globe", "c-purple", m(t.ip_month), "Public IPs") + tile("server", "c-gray", m(t.infra_month), "Infrastructure", `${b.hosts} host(s)`) + tile("chart", t.margin_month >= 0 ? "c-teal" : "c-red", m(t.margin_month), "Margin (usage - infra)");
    $("#bl-acc").innerHTML = (b.source === "none" ? `<div class="note" style="margin-bottom:10px">CloudStack API not configured - only the infrastructure cost is shown.</div>` : "") +
      table([{ t: "Account", k: "account" }, { t: "VMs", f: (a) => `${a.running}/${a.vms}` }, { t: "vCPU", k: "vcpu" }, { t: "RAM GB", k: "ram_gb" }, { t: "Storage GB", k: "storage_gb" }, { t: "IPs", k: "ips" }, { t: "Monthly", f: (a) => m(a.month) }], b.accounts, "No usage");
    fillForm($("#bl-form"), b.rates);
    const parts = [["Compute", t.vm_month, "#025cfc"], ["Storage", t.storage_month, "#f59e0b"], ["Public IPs", t.ip_month, "#7c3aed"], ["Infrastructure", t.infra_month, "#64748b"]], mx = Math.max(1, ...parts.map((p) => p[1]));
    $("#bl-bars").innerHTML = parts.map(([l, v, c]) => `<div class="flex" style="margin:6px 0"><span style="width:120px">${l}</span><div class="bar" style="flex:1;height:14px"><i style="width:${(100 * v) / mx}%;background:${c}"></i></div><b style="width:140px;text-align:right">${m(v)}</b></div>`).join("");
  }
  $("#bl-save").addEventListener("click", async () => { try { await POST("/api/billing/rates", formData($("#bl-form"))); toast("rates saved", "ok"); load(); } catch (e) { showErrors($("#bl-form"), e.data?.fields); fail(e); } });
  $("#bl-xlsx").addEventListener("click", async () => { const r = await POST("/api/reports", { kind: "cost", fmt: "xlsx" }); location.href = r.url; });
  await load();
};

/* ---------------- Jobs ---------------- */
window.page_jobs = async function () {
  let stop = null, curJob = null;
  async function load() { const k = $("#jb-kind").value; const r = await GET(`/api/jobs?limit=200${k ? "&kind=" + k : ""}`);
    $("#jb-list").innerHTML = jobsTable(r.jobs).replace(/href="\/jobs#job-(\d+)"/g, 'href="#job-$1" data-j="$1"');
    $$("[data-j]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); show(+a.dataset.j); })); }
  function show(id) { curJob = id; history.replaceState(null, "", `#job-${id}`); $("#jb-t").textContent = `#${id}`; const dl = $("#jb-dl"); dl.hidden = false; dl.href = `/api/jobs/${id}/log.txt`;
    stop && stop(); stop = watchJob(id, $("#jb-log"), (j) => { $("#jb-cancel").hidden = true; $("#jb-steps").innerHTML = stepsList(j.steps); load(); }, (j) => { $("#jb-steps").innerHTML = stepsList(j.steps); $("#jb-cancel").hidden = !["queued", "running"].includes(j.state); }); }
  $("#jb-cancel").addEventListener("click", async () => { if (confirm("Cancel this job?")) { await POST(`/api/jobs/${curJob}/cancel`).catch(fail); } });
  $("#jb-kind").addEventListener("change", load);
  await load(); const m = location.hash.match(/job-(\d+)/); if (m) show(+m[1]);
};

/* ---------------- Audit ---------------- */
window.page_audit = async function () {
  async function load() { const q = $("#au-q").value.trim(); const r = await GET(`/api/audit${q ? "?q=" + encodeURIComponent(q) : ""}`); $("#au-csv").href = `/api/audit?format=csv${q ? "&q=" + encodeURIComponent(q) : ""}`;
    $("#au-list").innerHTML = table([{ t: "Time", f: (a) => fmtTs(a.ts) }, { t: "User", k: "user" }, { t: "IP", k: "ip" }, { t: "Action", f: (a) => `<span class="mono">${esc(a.action)}</span>` }, { t: "Target", k: "target" },
      { t: "Detail", f: (a) => `<span class="muted">${esc((a.detail || "").slice(0, 160))}</span>` }, { t: "Result", f: (a) => pill(a.ok ? "ok" : "failed", a.ok ? "OK" : "Failed") }], r.audit, "Empty"); }
  let t; $("#au-q").addEventListener("input", () => { clearTimeout(t); t = setTimeout(load, 300); });
  await load();
};

/* ---------------- Settings ---------------- */
window.page_settings = async function () {
  initTabs(document, (t) => { $("#st-save-bar").hidden = ["users", "account", "help"].includes(t); if (t === "users") users(); });
  const s = (await GET("/api/settings")).settings;
  fillForm($("#st-form"), s); $("#st-key").value = s.pubkey || "";
  $("#st-ev").value = s.ansible_extra_vars && Object.keys(s.ansible_extra_vars).length ? JSON.stringify(s.ansible_extra_vars, null, 1) : "";
  $("#th-cpu").value = s.thresholds.min_cpu; $("#th-mem").value = s.thresholds.min_mem_mb; $("#th-disk").value = s.thresholds.min_disk_gb;
  $("#al-rules").innerHTML = Object.entries(s.alert_rules).map(([k, v]) => `<div class="f"><label>${esc(k.replace("_", " "))}</label><input type="number" class="ar" data-k="${k}" value="${v}"></div>`).join("");
  $("#st-save").addEventListener("click", async () => {
    const d = formData($("#st-form")); Object.keys(d).forEach((k) => { if (d[k] === "" && !["cs_api_url", "zabbix_url", "kolla_ansible_bin", "kolla_share_dir", "ansible_playbook_bin", "cs_api_key", "repo_base_url"].includes(k)) delete d[k]; });
    if (d.repo_base_url === "") delete d.repo_base_url;
    d.thresholds = { min_cpu: +$("#th-cpu").value, min_mem_mb: +$("#th-mem").value, min_disk_gb: +$("#th-disk").value };
    d.alert_rules = Object.fromEntries($$(".ar").map((i) => [i.dataset.k, +i.value]));
    try { d.ansible_extra_vars = $("#st-ev").value.trim() ? JSON.parse($("#st-ev").value) : {}; } catch (e) { return showErrors($("#st-form"), { ansible_extra_vars: "not valid JSON" }); }
    if (!can("admin")) { const keep = ["ollama_model", "thresholds", "alert_rules"]; Object.keys(d).forEach((k) => !keep.includes(k) && delete d[k]); }
    try { await POST("/api/settings", d); showErrors($("#st-form"), {}); $("#st-msg").textContent = "saved"; toast("settings saved", "ok"); } catch (e) { showErrors($("#st-form"), e.data?.fields); fail(e); }
  });
  async function users() { if (!can("admin")) return; const r = await GET("/api/users");
    $("#us-list").innerHTML = table([{ t: "User", k: "username" }, { t: "Name", k: "full_name" }, { t: "Role", f: (u) => `<select data-ur="${u.id}">${["viewer", "operator", "admin"].map((x) => `<option ${x === u.role ? "selected" : ""}>${x}</option>`).join("")}</select>` },
      { t: "Last login", f: (u) => ago(u.last_login) }, { t: "Status", f: (u) => pill(u.disabled ? "Disabled" : "Enabled") }, { t: "", f: (u) => `<button class="btn sm g" data-ud="${u.id}" data-v="${u.disabled ? 0 : 1}">${u.disabled ? "Enable" : "Disable"}</button>` }], r.users);
    $$("[data-ur]").forEach((s) => s.addEventListener("change", async () => { try { await api("PATCH", `/api/users/${s.dataset.ur}`, { role: s.value }); toast("role changed", "ok"); } catch (e) { fail(e); users(); } }));
    $$("[data-ud]").forEach((b) => b.addEventListener("click", async () => { try { await api("PATCH", `/api/users/${b.dataset.ud}`, { disabled: b.dataset.v === "1" }); users(); } catch (e) { fail(e); } })); }
  $("#us-add")?.addEventListener("click", async () => { const f = $("#us-form"); try { await POST("/api/users", formData(f)); f.reset(); showErrors(f, {}); toast("user added", "ok"); users(); } catch (e) { showErrors(f, e.data?.fields); } });
  $("#pw-save").addEventListener("click", async () => { const f = $("#pw-form"); try { await POST("/api/me/password", formData(f)); f.reset(); showErrors(f, {}); toast("password changed", "ok"); } catch (e) { showErrors(f, { new: e.message }); } });
};
