/* Package Repository page */
"use strict";
window.page_packages = async function () {
  initTabs(document, (t) => ({ repo: loadRepo, jobs: loadJobs, required: loadRequired, disk: loadDisk, online: loadOnline }[t] || (() => {}))());

  /* ---------- upload: resumable chunks, retries, server-side verify + extract job ---------- */
  const drop = $("#drop"), input = $("#up-file"), list = $("#up-list");
  let shaMap = {}, totalBytes = 0;
  const rowId = (n) => "u-" + n.replace(/[^a-z0-9]/gi, "_");
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("hover"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("hover"); }));
  drop.addEventListener("drop", (e) => handle([...e.dataTransfer.files]));
  input.addEventListener("change", () => { handle([...input.files]); input.value = ""; });

  async function handle(files) {
    if (!can("operator")) return toast("operator role required to upload", "bad");
    for (const f of files.filter((f) => f.name.endsWith(".sha256"))) {
      const t = (await f.text()).trim().split(/\s+/)[0]; shaMap[f.name.replace(/\.sha256$/, "")] = t;
    }
    const real = files.filter((f) => !f.name.endsWith(".sha256"));
    if (!real.length) return;
    if (list.querySelector(".empty")) list.innerHTML = "";
    for (const f of real) { totalBytes += f.size; addRow(f); }
    $("#up-total").textContent = `Total: ${fmtBytes(totalBytes)}`;
    for (const f of real) await upload(f);
  }
  function addRow(f) {
    list.insertAdjacentHTML("beforeend", `<div class="upl" id="${rowId(f.name)}"><div class="fi">${ic("file")}</div>
      <div class="nm"><b>${esc(f.name)}</b><small>${fmtBytes(f.size)}</small></div><div class="bar"><i style="width:0"></i></div>
      <div class="st">Pending</div><button class="iconbtn" title="cancel">${ic("x")}</button></div>`);
    $(`#${rowId(f.name)} button`).addEventListener("click", () => { f._cancel = true; });
  }
  async function upload(f) {
    const row = $("#" + rowId(f.name)), bar = $(".bar i", row), st = $(".st", row);
    const repo = $("#up-repo").value;
    let u;
    try { u = await api("POST", "/api/packages/uploads", { name: f.name, size: f.size, repo, sha256: shaMap[f.name] || "" }); }
    catch (e) { st.innerHTML = pill("error", e.data?.fields ? Object.values(e.data.fields)[0] : e.message); return; }
    const CH = u.chunk_size || 8 * 1024 * 1024; let off = 0, t0 = Date.now(), tries = 0;
    while (off < f.size) {
      if (f._cancel) { st.innerHTML = pill("cancelled"); await api("DELETE", `/api/packages/uploads/${u.id}`).catch(() => {}); return; }
      const blob = f.slice(off, Math.min(off + CH, f.size));
      const r = await api("PUT", `/api/packages/uploads/${u.id}/chunk?offset=${off}`, blob, { raw: true }).catch((e) => ({ status: 0, data: { error: e.message } }));
      if (r.status === 200) { off = r.data.received; tries = 0; }
      else if (r.status === 409 && r.data.received !== undefined) { off = r.data.received; }        // resume where the server is
      else if (++tries > 5) { st.innerHTML = pill("error", r.data.error || "network error"); return; }
      else { await new Promise((res) => setTimeout(res, 1000 * tries)); continue; }
      const pct = Math.round((100 * off) / f.size), sp = off / Math.max(1, (Date.now() - t0) / 1000);
      bar.style.width = pct + "%"; st.innerHTML = `Uploading... ${pct}%<br><small class="muted">${fmtBytes(off)} / ${fmtBytes(f.size)} · ${fmtBytes(sp)}/s</small>`;
    }
    bar.style.width = "100%";
    try {
      const fin = await POST(`/api/packages/uploads/${u.id}/finish`);
      st.innerHTML = `${pill("ok", "Uploaded")}<br><small>Extracting...</small>`;
      $("#x-name").textContent = `(${f.name})`;
      const marks = $$("#x-steps .dot");
      watchJob(fin.job_id, $("#x-log"), (job) => {
        st.innerHTML = job.state === "success" ? pill("done", "Uploaded · Extracted") : pill("failed", "Extraction failed");
        $(".bar", row).classList.add(job.state === "success" ? "ok" : "bad");
        marks.forEach((m) => { m.className = "dot " + (job.state === "success" ? "d-ok" : "d-bad"); m.innerHTML = ic(job.state === "success" ? "check" : "x"); });
      }, () => {
        const txt = $("#x-log").textContent;
        [/\[2\/5\]|Single file/, /\[1\/5\].*OK/, /\[3\/5\]/, /\[4\/5\]/, /\[5\/5\]/].forEach((re, i) => { if (re.test(txt)) { marks[i].className = "dot d-ok"; marks[i].innerHTML = ic("check"); } });
      });
    } catch (e) { st.innerHTML = pill("error", e.message); }
  }

  /* ---------- online download kit ---------- */
  let product = "cloudstack";
  const prodBar = $('[data-tabs="prod"]');
  $$("button", prodBar).forEach((b) => b.addEventListener("click", () => { product = b.dataset.t; $$("button", prodBar).forEach((x) => x.classList.toggle("act", x === b)); loadOnline(); }));
  $("button", prodBar).classList.add("act");
  async function loadOnline() {
    const PRODUCTS = window.__products || (window.__products = await GET("/api/packages/online/manifest-catalog").catch(() => null));
    const r = await POST("/api/packages/online/manifest", { product, os: $("#on-os").value });
    $("#online-note").innerHTML = r.online_enabled ? "<b>Online mode is enabled</b> in Settings: this master can download directly." :
      "This master is air-gapped. Choose what you need, download the <b>download kit</b>, run it on any internet-connected machine (with Docker, or without Docker: <span class=\"mono\">sudo NO_DOCKER=1 bash kit.sh</span> on the same OS as the nodes) and upload the ZIP it creates.";
    $("#on-fetch").disabled = !r.online_enabled || !can("operator");
    const vsel = $("#on-version"); const versions = (PRODUCTS && PRODUCTS[product]) || [];
    if (vsel.dataset.p !== product) { vsel.innerHTML = versions.map((v) => `<option>${esc(v)}</option>`).join(""); vsel.dataset.p = product; }
    $("#on-comps").innerHTML = table([{ t: "", f: (c) => `<input type="checkbox" class="oc" value="${esc(c.id)}" checked>` }, { t: "Component", k: "label" },
      { t: "Packages / files", f: (c) => `<span class="mono">${esc(c.kind === "file" ? c.pattern : c.names.join(" "))}</span>` }, { t: "Size", f: (c) => `${c.size_mb >= 1024 ? (c.size_mb / 1024).toFixed(1) + " GB" : c.size_mb + " MB"}` }], r.components);
    $("#on-total").textContent = `${(r.total_mb / 1024).toFixed(1)} GB`;
  }
  $("#on-os").addEventListener("change", loadOnline);
  $("#on-all").addEventListener("change", (e) => $$(".oc").forEach((c) => (c.checked = e.target.checked)));
  $("#on-kit").addEventListener("click", async () => {
    const r = await api("POST", "/api/packages/online/kit", { product, os: $("#on-os").value, version: $("#on-version").value, templates: $("#on-tpl").checked }, { raw: true });
    if (r.status !== 200) return toast(r.data.error || "failed", "bad");
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([r.data], { type: "text/x-shellscript" }));
    a.download = `download-${product}.sh`; a.click(); toast(`Run it online; it creates ${r.headers.get("X-Zip-Name")}`, "ok");
  });
  $("#on-fetch").addEventListener("click", async () => {
    try { const r = await POST("/api/packages/online/fetch", { product, os: $("#on-os").value, version: $("#on-version").value });
      $("#on-job-card").hidden = false; watchJob(r.job_id, $("#on-log"), (j) => toast(`download ${j.state}`, j.state === "success" ? "ok" : "bad"), (j) => ($("#on-bar").style.width = j.progress + "%"));
    } catch (e) { fail(e); }
  });

  /* ---------- repository ---------- */
  let curRepo = "";
  async function loadRepo() {
    const r = await GET("/api/packages/repo");
    $("#repo-sum").innerHTML = table([{ t: "Repository", f: (x) => `<a href="#repo" data-repo="${esc(x.repo)}">${esc(x.label)}</a>` }, { t: "Packages", f: (x) => `${x.deb} deb · ${x.rpm} rpm · ${x.files} files` },
      { t: "Size", f: (x) => fmtBytes(x.size) }, { t: "apt / dnf URL on nodes", f: (x) => `<span class="mono">${esc(x.url_deb)} · ${esc(x.url_rpm)}</span>` },
      { t: "", f: (x) => `<button class="btn sm g" data-reindex="${esc(x.repo)}" ${can("operator") ? "" : "disabled"}>Re-index</button>` }], r.repos);
    $$("[data-repo]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); curRepo = a.dataset.repo; loadItems(); }));
    $$("[data-reindex]").forEach((b) => b.addEventListener("click", async () => { const j = await POST(`/api/packages/reindex/${b.dataset.reindex}`); toast(`re-index job #${j.job_id} started`, "ok"); }));
    loadItems();
  }
  async function loadItems() {
    const q = $("#repo-q").value.trim();
    const r = await GET(`/api/packages/items?limit=500${curRepo ? "&repo=" + curRepo : ""}${q ? "&q=" + encodeURIComponent(q) : ""}`);
    $("#items-h").textContent = `Packages ${curRepo ? "in " + curRepo : "(all repositories)"} - ${r.items.length} shown`;
    $("#repo-items").innerHTML = table([{ t: "Name", k: "name" }, { t: "Version", k: "version" }, { t: "Arch", k: "arch" }, { t: "Type", f: (x) => pill("info", x.kind) },
      { t: "Repository", k: "repo" }, { t: "Size", f: (x) => fmtBytes(x.size) }, { t: "SHA256", f: (x) => `<span class="mono">${esc((x.sha256 || "").slice(0, 16))}</span>` }], r.items, "No packages - upload a bundle first");
  }
  let qt; $("#repo-q").addEventListener("input", () => { clearTimeout(qt); qt = setTimeout(loadItems, 300); });

  /* ---------- extraction jobs ---------- */
  async function loadJobs() {
    const [u, j] = await Promise.all([GET("/api/packages/uploads"), GET("/api/jobs?kind=extract")]);
    $("#xjobs").innerHTML = table([{ t: "#", k: "id", cls: "n" }, { t: "File", k: "name" }, { t: "Repository", k: "repo" }, { t: "Size", f: (x) => fmtBytes(x.size) },
      { t: "Status", f: (x) => pill(x.state) }, { t: "Message", k: "message" }, { t: "Uploaded", f: (x) => fmtTs(x.created) },
      { t: "", f: (x) => x.job_id ? `<button class="btn sm g" data-log="${x.job_id}" data-n="${esc(x.name)}">Log</button>` : "" }], u.uploads, "No uploads yet");
    $$("[data-log]").forEach((b) => b.addEventListener("click", () => { $("#xj-name").textContent = b.dataset.n; watchJob(+b.dataset.log, $("#xj-log")); }));
    const other = j.jobs.filter((x) => !x.meta.upload_id);
    if (other.length) $("#xjobs").insertAdjacentHTML("beforeend", `<h2 class="mt">Re-index jobs</h2>` + table([{ t: "#", k: "id" }, { t: "Job", k: "title" }, { t: "State", f: (x) => pill(x.state) }, { t: "When", f: (x) => fmtTs(x.created) }], other));
  }

  /* ---------- verification ---------- */
  async function loadRequired() {
    const r = await GET(`/api/packages/required/${$("#rq-product").value}?family=${$("#rq-family").value}`);
    $("#rq-list").innerHTML = (r.ready ? `<div class="okbox">All required packages are available and ready for deployment.</div>` : `<div class="note">Some packages are missing - upload the product bundle.</div>`) +
      table([{ t: "Status", f: (x) => x.present ? dot("ok") : dot("bad") }, { t: "Component", k: "label" }, { t: "Needs", f: (x) => `<span class="mono">${esc(x.kind === "file" ? x.pattern : x.names.join(", "))}</span>` },
        { t: "Missing", f: (x) => `<span class="mono">${esc(x.missing.join(", "))}</span>` }, { t: "Size (approx)", f: (x) => x.size_mb + " MB" }], r.items);
  }
  $("#rq-product").addEventListener("change", loadRequired); $("#rq-family").addEventListener("change", loadRequired);
  $("#vf-run").addEventListener("click", async () => {
    try { const r = await POST(`/api/packages/verify/${$("#vf-repo").value}`);
      $("#vf-out").innerHTML = r.ok ? `<div class="okbox">${r.checked} file(s) verified - all SHA256 checksums match.</div>` : `<div class="note">${r.bad.length} of ${r.checked} file(s) changed or missing:<br><span class="mono">${r.bad.map(esc).join("<br>")}</span></div>`;
    } catch (e) { fail(e); }
  });
  $("#dep-run").addEventListener("click", async () => {
    const r = await GET(`/api/packages/deps/${$("#vf-repo").value}`);
    $("#vf-out").innerHTML = r.missing.length ? `<div class="note">${r.missing.length} dependency group(s) are not in the master repositories (fine if the base OS already has them):</div>` +
      table([{ t: "Dependency", k: "dependency" }, { t: "Needed by", f: (x) => esc(x.needed_by.join(", ")) }], r.missing) : `<div class="okbox">All dependencies are satisfied by the master repositories.</div>`;
  });

  async function loadDisk() {
    const d = await GET("/api/packages/disk");
    const pct = Math.round((100 * d.used) / d.total);
    $("#disk").innerHTML = `<div class="tiles">${tile("disk", "c-blue", fmtBytes(d.total), "Disk total")}${tile("disk", pct > 85 ? "c-red" : "c-orange", fmtBytes(d.used), "Used", pct + "%")}${tile("disk", "c-green", fmtBytes(d.free), "Free")}${tile("upload", "c-purple", fmtBytes(d.uploads_pending), "Partial uploads")}</div>
      <div class="bar ${pct > 85 ? "bad" : ""}"><i style="width:${pct}%"></i></div>` +
      table([{ t: "Repository", k: "repo" }, { t: "Size", f: (x) => fmtBytes(x.size) }], Object.entries(d.repos).map(([repo, size]) => ({ repo, size })));
  }
};
