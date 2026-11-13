/* CloudStack + OpenStack deployment wizards */
"use strict";
function reqList(items) {
  const ok = items.every((i) => i.present);
  return table([{ t: "Status", f: (x) => (x.present ? dot("ok") : dot("bad")) }, { t: "Package", f: (x) => esc(x.label) },
    { t: "Version / file", f: (x) => `<span class="mono">${esc(x.kind === "file" ? x.pattern : x.names[0] || "")}</span>` }, { t: "Size", f: (x) => `${x.size_mb} MB` }], items)
    + (ok ? `<div class="okbox mt">All required packages are available and ready for deployment.</div>` : `<div class="note mt">Missing packages are marked red - upload them in Package Repository.</div>`);
}
function wizard(cfg) {
  let W, HOSTS = [], cur = 0;
  const forms = $$(cfg.formSel), steps = cfg.steps;
  async function load() {
    const [w, h] = await Promise.all([GET(cfg.api), GET("/api/hosts")]); W = w; HOSTS = h.hosts;
    $$(".hostsel").forEach((s) => (s.innerHTML = hostOptions(HOSTS, [], null)));
    forms.forEach((f) => { const st = f.dataset.step; if (W.state[st]) fillForm(f, W.state[st]); });
    cfg.afterLoad && cfg.afterLoad(W, HOSTS);
    $(cfg.req).innerHTML = reqList(W.required);
  }
  function show(i) {
    cur = Math.max(0, Math.min(steps.length - 1, i));
    forms.forEach((f) => f.classList.toggle("act", f.dataset.step === steps[cur]));
    $$(`${cfg.stepper} .st`).forEach((el, k) => { el.classList.toggle("act", k === cur); el.classList.toggle("done", k < cur); });
    $(cfg.prev).disabled = cur === 0; $(cfg.next).hidden = cur === steps.length - 1;
    $(cfg.next).textContent = cur < steps.length - 2 ? `Next : ${cfg.labels[cur + 1]} →` : "Review →";
    history.replaceState(null, "", "#" + steps[cur]);
    cfg.onShow && cfg.onShow(steps[cur], W, HOSTS);
  }
  async function save() {
    const f = forms.find((x) => x.dataset.step === steps[cur]); if (!f || f.tagName !== "FORM") return true;
    const d = cfg.collect ? cfg.collect(steps[cur], f) : formData(f);
    try { const r = await POST(`${cfg.api}/${steps[cur]}`, d); W.state = r.state; showErrors(f, {}); $(cfg.msg).textContent = "saved"; return true; }
    catch (e) { showErrors(f, e.data?.fields || {}); $(cfg.msg).textContent = e.message; return false; }
  }
  $(cfg.prev).addEventListener("click", () => show(cur - 1));
  $(cfg.next).addEventListener("click", async () => { if (await save()) { const w = await GET(cfg.api); W.required = w.required; W.plan = w.plan; $(cfg.req).innerHTML = reqList(W.required); show(cur + 1); } });
  $$(`${cfg.stepper} .st`).forEach((el, k) => el.addEventListener("click", async () => { if (k <= cur || (await save())) show(k); }));
  return { load, show, get W() { return W; }, get HOSTS() { return HOSTS; }, start: async () => { await load(); const h = location.hash.slice(1); show(Math.max(0, steps.indexOf(h))); } };
}

window.page_cswizard = async function () {
  const STEPS = ["requirements", "management", "database", "mq", "systemvm", "zone", "cluster", "review"];
  const LABELS = $$("#wz-steps .st").map((s) => s.textContent.replace(/^\d+/, "").trim());
  let clusters = [];
  const renderClusters = (HOSTS) => {
    $("#cl-list").innerHTML = clusters.map((c, i) => `<div class="row" data-ci="${i}"><div class="f"><label>Cluster name</label><input type="text" class="cl-name" value="${esc(c.name)}"></div>
      <div class="f"><label>KVM hosts</label><select class="cl-hosts" multiple size="4">${hostOptions(HOSTS, c.host_ids, null)}</select></div>
      <div class="f"><label>&nbsp;</label><button type="button" class="btn sm g cl-del">Remove</button></div><div class="f" data-err="clusters.${i}.name"></div><div class="f" data-err="clusters.${i}.host_ids"></div></div>`).join("");
    $$(".cl-del").forEach((b) => b.addEventListener("click", () => { syncClusters(); clusters.splice(+b.closest("[data-ci]").dataset.ci, 1); renderClusters(HOSTS); }));
  };
  const syncClusters = () => { clusters = $$("#cl-list [data-ci]").map((r) => ({ name: $(".cl-name", r).value.trim(), host_ids: $$(".cl-hosts option:checked", r).map((o) => +o.value) })); };
  const wz = wizard({
    api: "/api/cs/wizard", formSel: "#wz-forms > .pane", steps: STEPS, labels: LABELS, stepper: "#wz-steps", prev: "#wz-prev", next: "#wz-next", msg: "#wz-msg", req: "#wz-req",
    afterLoad: async (W, HOSTS) => {
      $$('[name="mode_r"]').forEach((r) => { r.checked = r.value === W.state.requirements.mode; r.addEventListener("change", () => ($('[data-step="requirements"] [name="mode"]').value = r.value)); });
      clusters = JSON.parse(JSON.stringify(W.state.cluster.clusters)); renderClusters(HOSTS);
      $("#cl-extra").value = (W.state.cluster.extra_storage || []).map((x) => `${x.kind} ${x.server} ${x.path} ${x.name}`).join("\n");
      const items = (await GET("/api/packages/items?limit=200&q=systemvm")).items;
      $("#tpl-sel").innerHTML = `<option value="">- choose -</option>` + items.map((x) => `<option value="${esc(x.path)}" ${W.state.systemvm.template === x.path ? "selected" : ""}>${esc(x.repo)}/${esc(x.path)} (${fmtBytes(x.size)})</option>`).join("");
      ["management", "database", "mq"].forEach((st) => fillForm($(`[data-step="${st}"]`), { host_ids: W.state[st].host_ids, lb_host_ids: W.state[st].lb_host_ids || [] }));
      $("#wz-plan").innerHTML = W.plan.map((p) => `<li>${esc(p.label)} <span class="mono">${esc(p.playbook)}</span></li>`).join("") + "<li>Configure zones, pods, clusters, hosts and storage (API)</li>";
    },
    collect: (st, f) => { const d = formData(f); delete d.mode_r; if (st === "cluster") {
        syncClusters(); d.clusters = clusters;
        d.extra_storage = $("#cl-extra").value.split("\n").map((l) => l.trim().split(/\s+/)).filter((p) => p[0])
          .map(([kind, server, path, name]) => ({ kind, server: server || "", path: path || "", name: name || "" }));
      } return d; },
    onShow: async (st, W) => { if (st === "review") review(); },
  });
  $("#cl-add").addEventListener("click", () => { syncClusters(); clusters.push({ name: `Cluster-${clusters.length + 1}`, host_ids: [] }); renderClusters(wz.HOSTS); });
  async function review() {
    const W = await GET("/api/cs/wizard"), s = W.state, H = Object.fromEntries(wz.HOSTS.map((h) => [h.id, h]));
    const names = (ids) => ids.map((i) => H[i] ? `${H[i].hostname || H[i].ip} (${H[i].ip})` : `#${i}`).join(", ") || "-";
    $("#rv-kvm-f").hidden = s.cluster.kvm_auth !== "password";
    $("#rv-summary").innerHTML = `<div class="kv"><div>Version / OS</div><div>${esc(s.requirements.version)} on ${esc(s.requirements.os)} (${esc(s.requirements.mode)})</div>
      <div>Management</div><div>${esc(names(s.management.host_ids))} ${s.management.vip ? "VIP " + esc(s.management.vip) + " on " + esc(names(s.management.lb_host_ids || [])) : ""}</div>
      <div>Database</div><div>${esc(s.database.strategy)} · ${esc(names(s.database.host_ids))} ${s.database.vip ? "VIP " + esc(s.database.vip) : ""}</div>
      <div>RabbitMQ</div><div>${s.mq.enabled ? esc(names(s.mq.host_ids)) : "not installed"}</div><div>System VM template</div><div class="mono">${esc(s.systemvm.template || "-")}</div>
      <div>Zone / Pod</div><div>${esc(s.zone.name)} / ${esc(s.zone.pod_name)} · pod ${esc(s.zone.pod_start)}-${esc(s.zone.pod_end)} · public ${esc(s.zone.public_start)}-${esc(s.zone.public_end)}</div>
      <div>Clusters</div><div>${s.cluster.clusters.map((c) => `${esc(c.name)}: ${esc(names(c.host_ids))}`).join("<br>")}</div>
      <div>Storage</div><div>primary nfs://${esc(s.cluster.primary_server)}${esc(s.cluster.primary_path)} · secondary nfs://${esc(s.cluster.secondary_server)}${esc(s.cluster.secondary_path)}</div></div>
      <h2 class="mt">Deployment Preview</h2>${stepsList(W.plan.map((p) => ({ name: p.label, state: "pending" })).concat([{ name: "Configure zones, pods, clusters and storage", state: "pending" }]))}`;
    $("#rv-errors").innerHTML = "";
  }
  const startDeploy = async (resume) => {
    const body = { admin_password: $("#rv-admin").value || undefined, kvm_password: $("#rv-kvm").value || undefined, force: $("#rv-force").checked, resume };
    try {
      const r = await POST("/api/cs/wizard/deploy", body); $("#rv-job").hidden = false; $("#rv-jt").textContent = `job #${r.job_id}`;
      watchJob(r.job_id, $("#rv-log"), (j) => toast(`CloudStack deployment ${j.state}`, j.state === "success" ? "ok" : "bad"),
        (j) => { $("#rv-steps").innerHTML = stepsList(j.steps); $("#rv-bar").style.width = j.progress + "%"; });
    } catch (e) {
      const st = e.data?.steps; $("#rv-errors").innerHTML = st ? `<div class="note">${Object.entries(st).map(([k, v]) => `<b>${esc(k)}</b>: ${esc(Object.values(v).join("; "))}`).join("<br>")}</div>` : `<div class="note">${esc(e.message)}</div>`;
    }
  };
  $("#rv-deploy").addEventListener("click", () => startDeploy(false));
  $("#rv-resume").addEventListener("click", () => startDeploy(true));
  await wz.start();
};

window.page_oswizard = async function () {
  const STEPS = ["requirements", "nodes", "services", "network", "deploy"];
  const LABELS = $$("#ow-steps .st").map((s) => s.textContent.replace(/^\d+/, "").trim());
  initTabs(document);
  const wz = wizard({
    api: "/api/os/wizard", formSel: "#ow-steps ~ .grid > div:first-child > .pane", steps: STEPS, labels: LABELS, stepper: "#ow-steps", prev: "#ow-prev", next: "#ow-next", msg: "#ow-msg", req: "#ow-req",
    afterLoad: (W) => {
      $$('[data-step="services"] input[type=checkbox]').forEach((c) => (c.checked = !!W.state.services[c.name] || c.disabled));
      ["controller_ids", "compute_ids", "storage_ids", "network_ids"].forEach((k) => fillForm($('[data-step="nodes"]'), { [k]: W.state.nodes[k] }));
      $("#ow-kolla").innerHTML = W.kolla_share ? `<div class="okbox">kolla-ansible found: <span class="mono">${esc(W.kolla_share)}</span></div>` : `<div class="note">kolla-ansible is not installed on the master yet: run <span class="mono">master/install-kolla-tools.sh</span> (offline wheels) before deploying.</div>`;
    },
    collect: (st, f) => { const d = formData(f); if (st === "services") $$("input[type=checkbox]", f).forEach((c) => (d[c.name] = c.checked)); return d; },
    onShow: async (st) => { if (st === "deploy") { const p = await GET("/api/os/preview"); $("#ow-globals").textContent = p.globals; $("#ow-multi").textContent = p.multinode; } },
  });
  $("#ow-deploy").addEventListener("click", async () => {
    try { const r = await POST("/api/os/deploy", { force: $("#ow-force").checked }); $("#ow-job").hidden = false; $("#ow-jt").textContent = `job #${r.job_id}`;
      watchJob(r.job_id, $("#ow-log"), (j) => toast(`OpenStack deployment ${j.state}`, j.state === "success" ? "ok" : "bad"), (j) => ($("#ow-jsteps").innerHTML = stepsList(j.steps)));
    } catch (e) { const st = e.data?.steps; $("#ow-errors").innerHTML = `<div class="note">${st ? Object.entries(st).map(([k, v]) => `<b>${esc(k)}</b>: ${esc(Object.values(v).join("; "))}`).join("<br>") : esc(e.message)}</div>`; }
  });
  $("#ow-destroy").addEventListener("click", async () => {
    const typed = prompt("This deletes every OpenStack container and volume on the nodes. Type destroy to continue:");
    if (typed !== "destroy") return;
    try { const r = await POST("/api/os/destroy", { confirm: "destroy" }); $("#ow-job").hidden = false; $("#ow-jt").textContent = `job #${r.job_id}`;
      watchJob(r.job_id, $("#ow-log"), (j) => toast(`OpenStack removal ${j.state}`, j.state === "success" ? "ok" : "bad"), (j) => ($("#ow-jsteps").innerHTML = stepsList(j.steps)));
    } catch (e) { toast(e.message, "bad"); }
  });
  $("#ow-release").addEventListener("click", async () => {
    if (prompt("OpenStack stops on these nodes and the host services it replaced start again. Type release to continue:") !== "release") return;
    try { const r = await POST("/api/os/release", { confirm: "release" }); $("#ow-job").hidden = false; $("#ow-jt").textContent = `job #${r.job_id}`;
      watchJob(r.job_id, $("#ow-log"), (j) => toast(`Release ${j.state}`, j.state === "success" ? "ok" : "bad"), (j) => ($("#ow-jsteps").innerHTML = stepsList(j.steps)));
    } catch (e) { toast(e.message, "bad"); }
  });
  $("#ow-regen").addEventListener("click", async () => { if (confirm("Only for a cloud that is NOT deployed yet. Continue?")) { const r = await POST("/api/os/passwords/regenerate"); toast(r.note, "ok"); } });
  await wz.start();
};
