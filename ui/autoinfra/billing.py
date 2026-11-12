"""Billing & cost: a rate card applied to CloudStack usage (VMs, volumes, public IPs) plus the infrastructure's own cost."""
import csv, io

from flask import Blueprint, Response, jsonify, render_template, request

from . import auth, db, validators as V

bp = Blueprint("billing", __name__)
HOURS_MONTH = 730
DEFAULT_RATES = {"currency": "INR", "vcpu_hour": 1.5, "ram_gb_hour": 0.75, "storage_gb_month": 4.0, "public_ip_month": 250.0,
                 "host_month": 12000.0, "license_month": 0.0, "stopped_vm_storage_only": True}


def rates():
    r = dict(DEFAULT_RATES)
    r.update(db.get_setting("billing_rates", {}) or {})
    return r


def vm_month(vm, r):
    if vm.get("state") != "Running" and r["stopped_vm_storage_only"]:
        return 0.0
    cpu = float(vm.get("cpunumber", 0) or 0)
    ram_gb = float(vm.get("memory", 0) or 0) / 1024
    return HOURS_MONTH * (cpu * r["vcpu_hour"] + ram_gb * r["ram_gb_hour"])


def compute(vms=None, volumes=None, ips=None, host_count=None):
    """All arguments optional: by default they come from the CloudStack API (if configured) and the hosts table."""
    r = rates()
    source = "given"
    if vms is None:
        vms, volumes, ips, source = [], [], [], "none"
        try:
            from . import cloudstack
            cs = cloudstack.api_client()
            vms = cs.items("listVirtualMachines", "virtualmachine", listall="true")
            volumes = cs.items("listVolumes", "volume", listall="true")
            ips = cs.items("listPublicIpAddresses", "publicipaddress", listall="true", allocatedonly="true")
            source = "cloudstack"
        except Exception:  # noqa: BLE001
            pass
    volumes, ips = volumes or [], ips or []
    if host_count is None:
        host_count = db.one("SELECT COUNT(*) n FROM hosts")["n"]
    acc = {}

    def a(name):
        return acc.setdefault(name or "-", {"account": name or "-", "vms": 0, "running": 0, "vcpu": 0, "ram_gb": 0.0, "storage_gb": 0.0,
                                            "ips": 0, "vm_month": 0.0, "storage_month": 0.0, "ip_month": 0.0, "month": 0.0})
    for vm in vms:
        x = a(vm.get("account"))
        x["vms"] += 1
        x["running"] += vm.get("state") == "Running"
        x["vcpu"] += int(vm.get("cpunumber", 0) or 0)
        x["ram_gb"] += round(float(vm.get("memory", 0) or 0) / 1024, 2)
        x["vm_month"] += vm_month(vm, r)
    for v in volumes:
        x = a(v.get("account"))
        gb = float(v.get("size", 0) or 0) / 1024 ** 3
        x["storage_gb"] += round(gb, 2)
        x["storage_month"] += gb * r["storage_gb_month"]
    for ip in ips:
        x = a(ip.get("account"))
        x["ips"] += 1
        x["ip_month"] += r["public_ip_month"]
    for x in acc.values():
        x["month"] = round(x["vm_month"] + x["storage_month"] + x["ip_month"], 2)
        for k in ("vm_month", "storage_month", "ip_month"):
            x[k] = round(x[k], 2)
        x["storage_gb"] = round(x["storage_gb"], 2)
        x["ram_gb"] = round(x["ram_gb"], 2)
    tot = {k: round(sum(x[k] for x in acc.values()), 2) for k in ("vm_month", "storage_month", "ip_month")}
    tot["infra_month"] = round(host_count * r["host_month"] + r["license_month"], 2)
    tot["total_month"] = round(sum(tot.values()), 2)
    tot["revenue_month"] = round(tot["vm_month"] + tot["storage_month"] + tot["ip_month"], 2)
    tot["margin_month"] = round(tot["revenue_month"] - tot["infra_month"], 2)
    return {"currency": r["currency"], "rates": r, "source": source, "accounts": sorted(acc.values(), key=lambda x: -x["month"]),
            "totals": tot, "hosts": host_count}


@bp.route("/billing")
@auth.login_required
def page():
    return render_template("billing.html")


@bp.route("/api/billing/summary")
@auth.login_required
def summary():
    return jsonify(compute())


@bp.route("/api/billing/rates", methods=["POST"])
@auth.role_required("admin")
def set_rates():
    j = request.get_json(silent=True) or {}
    r = rates()
    errs = {}
    for k, v in j.items():
        if k not in DEFAULT_RATES:
            errs[k] = "unknown rate"
        elif k == "currency":
            if not (isinstance(v, str) and v.isalpha() and len(v) == 3):
                errs[k] = "currency must be a 3-letter code (INR, USD, EUR)"
            else:
                r[k] = v.upper()
        elif k == "stopped_vm_storage_only":
            r[k] = bool(v)
        else:
            e = V.number_between(v, 0, 1e9, k)
            if e:
                errs[k] = e
            else:
                r[k] = float(v)
    if errs:
        return jsonify(error="validation failed", fields=errs), 400
    db.set_setting("billing_rates", r)
    auth.audit("billing.rates", "", str(r))
    return jsonify(ok=True, rates=r)


@bp.route("/api/billing/export.csv")
@auth.login_required
def export():
    b = compute()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["account", "vms", "running", "vcpu", "ram_gb", "storage_gb", "public_ips", f"vm_month_{b['currency']}",
                f"storage_month_{b['currency']}", f"ip_month_{b['currency']}", f"total_month_{b['currency']}"])
    for x in b["accounts"]:
        w.writerow([x["account"], x["vms"], x["running"], x["vcpu"], x["ram_gb"], x["storage_gb"], x["ips"], x["vm_month"],
                    x["storage_month"], x["ip_month"], x["month"]])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=cost-analysis.csv"})
