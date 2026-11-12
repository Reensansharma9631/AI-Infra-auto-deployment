"""PDF (reportlab) and Excel (openpyxl) reports: infrastructure, hosts, cost, audit."""
import json, os, time

from flask import Blueprint, g, jsonify, render_template, request, send_file

from . import auth, components, db, monitoring

bp = Blueprint("reports", __name__)
KINDS = {"infra": "Infrastructure report", "hosts": "Hosts & checks report", "cost": "Cost analysis report", "audit": "Audit log report"}


def report_dir():
    d = os.path.join(db.data_dir(), "reports")
    os.makedirs(d, exist_ok=True)
    return d


def fmt_ts(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""


def sections(kind):
    """[(title, header, rows)] - the same data feeds PDF and Excel."""
    out = []
    ov = monitoring.overview()
    if kind in ("infra", "hosts"):
        out.append(("Summary", ["Item", "Value"], [
            ["Generated", fmt_ts(time.time())], ["Hosts", f"{ov['hosts']['total']} ({ov['hosts']['online']} online)"],
            ["Virtual machines", f"{ov['vms']['total']} ({ov['vms']['running']} running)"],
            ["Open alerts", f"{ov['alerts']['critical']} critical, {ov['alerts']['warning']} warning"],
            ["Average CPU / memory / disk", f"{ov['gauges']['cpu']}% / {ov['gauges']['mem']}% / {ov['gauges']['disk']}%"]]))
        lm = monitoring.latest()
        rows = []
        for h in db.q("SELECT * FROM hosts ORDER BY id"):
            f = json.loads(h["facts"] or "{}")
            m = lm.get(h["id"], {})
            rows.append([h["hostname"] or "", h["ip"], h["roles"] or "", h["os_pretty"] or "", h["status"], f.get("CPUS", ""),
                         f.get("MEM_MB", ""), f.get("DISK_FREE_GB", ""), "" if m.get("cpu") is None else f"{m['cpu']:.0f}%"])
        out.append(("Hosts", ["Hostname", "IP", "Roles", "OS", "Status", "CPUs", "Memory MB", "Disk free GB", "CPU now"], rows))
    if kind == "hosts":
        rows = []
        for h in db.q("SELECT id, ip FROM hosts"):
            last = db.one("SELECT MAX(run_id) r FROM checks WHERE host_id=?", (h["id"],))
            for c in db.q("SELECT name, status, detail FROM checks WHERE host_id=? AND run_id IS ? ORDER BY id", (h["id"], last["r"] if last else None)):
                rows.append([h["ip"], c["name"], c["status"], c["detail"]])
        out.append(("Latest checks", ["Host", "Check", "Result", "Detail"], rows))
    if kind == "infra":
        rows = []
        for cid, c in components.COMPONENTS.items():
            r = components.comp_row(cid)
            rows.append([c["name"], r["version"] or c["version"], r["state"], ", ".join(str(x) for x in r["hosts"]), fmt_ts(r["updated"])])
        out.append(("Components", ["Component", "Version", "State", "Host ids", "Updated"], rows))
        out.append(("Open alerts", ["Time", "Severity", "Source", "Message"],
                    [[fmt_ts(a["ts"]), a["severity"], a["source"], a["message"]] for a in db.q("SELECT * FROM alerts WHERE resolved=0 ORDER BY id DESC")]))
        out.append(("Recent jobs", ["#", "Job", "State", "Started", "By"],
                    [[j["id"], j["title"], j["state"], fmt_ts(j["started"]), j["by_user"]] for j in db.q("SELECT * FROM jobs ORDER BY id DESC LIMIT 50")]))
    if kind in ("cost", "infra"):
        from . import billing
        b = billing.compute()
        out.append(("Cost summary", ["Item", f"Monthly ({b['currency']})"],
                    [["Virtual machines (usage)", f"{b['totals']['vm_month']:.2f}"], ["Storage", f"{b['totals']['storage_month']:.2f}"],
                     ["Public IPs", f"{b['totals']['ip_month']:.2f}"], ["Infrastructure (hosts)", f"{b['totals']['infra_month']:.2f}"],
                     ["Total", f"{b['totals']['total_month']:.2f}"]]))
        out.append(("Cost per account", ["Account", "VMs", "vCPU", "RAM GB", "Storage GB", f"Monthly ({b['currency']})"],
                    [[a["account"], a["vms"], a["vcpu"], a["ram_gb"], a["storage_gb"], f"{a['month']:.2f}"] for a in b["accounts"]]))
    if kind == "audit":
        out.append(("Audit log (last 1000)", ["Time", "User", "IP", "Action", "Target", "Result"],
                    [[fmt_ts(a["ts"]), a["user"], a["ip"], a["action"], a["target"], "ok" if a["ok"] else "failed"]
                     for a in db.q("SELECT * FROM audit ORDER BY id DESC LIMIT 1000")]))
    return out


def write_xlsx(path, title, secs):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    wb.remove(wb.active)
    for name, header, rows in secs:
        ws = wb.create_sheet(name[:31])
        ws.append(header)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="025CFC")
        for r in rows:
            ws.append([v if isinstance(v, (int, float)) else str(v) for v in r])
        for i, col in enumerate(header, start=1):
            width = max([len(str(col))] + [len(str(r[i - 1])) for r in rows if len(r) >= i] + [8])
            ws.column_dimensions[ws.cell(1, i).column_letter].width = min(60, width + 2)
    wb.properties.title = title
    wb.save(path)


def write_pdf(path, title, secs):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    st = getSampleStyleSheet()
    doc = SimpleDocTemplate(path, pagesize=landscape(A4), title=title, leftMargin=28, rightMargin=28, topMargin=28, bottomMargin=28)
    els = [Paragraph(f"AutoInfra - {title}", st["Title"]), Paragraph(fmt_ts(time.time()), st["Normal"]), Spacer(1, 10)]
    cell = st["BodyText"]
    cell.fontSize = 8
    cell.leading = 10
    for name, header, rows in secs:
        els.append(Paragraph(name, st["Heading2"]))
        data = [header] + [[Paragraph(str(v).replace("&", "&amp;").replace("<", "&lt;")[:500], cell) for v in r] for r in rows]
        if not rows:
            data.append(["(none)"] + [""] * (len(header) - 1))
        t = Table(data, repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#025cfc")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                               ("FONTSIZE", (0, 0), (-1, 0), 8), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d0d7e2")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f7fb")])]))
        els += [t, Spacer(1, 12)]
    doc.build(els)


def generate(kind, fmt, user):
    if kind not in KINDS or fmt not in ("pdf", "xlsx"):
        raise ValueError("bad kind/format")
    secs = sections(kind)
    name = f"{kind}-report-{time.strftime('%Y-%m-%d-%H%M%S')}.{fmt}"
    path = os.path.join(report_dir(), name)
    (write_pdf if fmt == "pdf" else write_xlsx)(path, KINDS[kind], secs)
    return db.ex("INSERT INTO reports(ts,kind,fmt,name,path,size,by_user) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), kind, fmt, name, path, os.path.getsize(path), user))


@bp.route("/reports")
@auth.login_required
def page():
    return render_template("reports.html", kinds=KINDS)


@bp.route("/api/reports")
@auth.login_required
def list_reports():
    return jsonify(reports=db.q("SELECT id, ts, kind, fmt, name, size, by_user FROM reports ORDER BY id DESC LIMIT 200"), kinds=KINDS)


@bp.route("/api/reports", methods=["POST"])
@auth.login_required
def create_report():
    j = request.get_json(silent=True) or {}
    kind, fmt = j.get("kind", "infra"), j.get("fmt", "pdf")
    if kind not in KINDS:
        return jsonify(error=f"kind must be one of {', '.join(KINDS)}"), 400
    if fmt not in ("pdf", "xlsx"):
        return jsonify(error="fmt must be pdf or xlsx"), 400
    if kind == "audit" and not auth.can("admin"):
        return jsonify(error="admin role required for the audit report"), 403
    rid = generate(kind, fmt, g.user["username"])
    auth.audit("report.generate", f"{kind}.{fmt}", rid)
    return jsonify(id=rid, url=f"/api/reports/{rid}/download"), 201


@bp.route("/api/reports/<int:rid>/download")
@auth.login_required
def download(rid):
    r = db.one("SELECT * FROM reports WHERE id=?", (rid,))
    if not r or not os.path.exists(r["path"]):
        return jsonify(error="no such report"), 404
    if r["kind"] == "audit" and not auth.can("admin"):
        return jsonify(error="admin role required"), 403
    mt = "application/pdf" if r["fmt"] == "pdf" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return send_file(r["path"], mimetype=mt, as_attachment=True, download_name=r["name"])


@bp.route("/api/reports/<int:rid>", methods=["DELETE"])
@auth.role_required("operator")
def delete(rid):
    r = db.one("SELECT * FROM reports WHERE id=?", (rid,))
    if not r:
        return jsonify(error="no such report"), 404
    if os.path.exists(r["path"]):
        os.unlink(r["path"])
    db.ex("DELETE FROM reports WHERE id=?", (rid,))
    auth.audit("report.delete", rid)
    return jsonify(ok=True)
