"""Operating-system knowledge: which OS/version is supported, which package manager it uses."""

# support: "supported" = tested end-to-end in the lab; "compat" = same family, expected to work; else "unsupported"
OS_TABLE = {
    "ubuntu":    {"family": "debian", "supported": ["24.04"], "compat": ["22.04"]},
    "debian":    {"family": "debian", "supported": [], "compat": ["12"]},
    "rocky":     {"family": "rhel", "supported": ["9"], "compat": ["8"]},
    "almalinux": {"family": "rhel", "supported": ["9"], "compat": ["8"]},
    "rhel":      {"family": "rhel", "supported": ["9"], "compat": ["8"]},
    "centos":    {"family": "rhel", "supported": ["9"], "compat": ["8"]},
    "ol":        {"family": "rhel", "supported": ["9"], "compat": ["8"]},
}
OS_NAMES = {"ubuntu": "Ubuntu", "debian": "Debian", "rocky": "Rocky Linux", "almalinux": "AlmaLinux", "rhel": "RHEL",
            "centos": "CentOS Stream", "ol": "Oracle Linux"}

PKG_MANAGERS = {
    "debian": {"tool": "apt", "refresh": "DEBIAN_FRONTEND=noninteractive apt-get update -q",
               "upgrade": "DEBIAN_FRONTEND=noninteractive apt-get -y -q -o Dpkg::Options::=--force-confold upgrade",
               "count": "apt-get -s upgrade 2>/dev/null | grep -c '^Inst ' || true",
               "installed": "dpkg-query -W -f='${Status}' {pkg} 2>/dev/null | grep -q 'install ok installed'",
               "base_pkgs": ["chrony", "python3", "python3-apt", "curl", "jq", "rsync"]},
    "rhel": {"tool": "dnf", "refresh": "dnf -q -y makecache",
             "upgrade": "dnf -q -y upgrade",
             "count": "dnf -q check-update 2>/dev/null | grep -cE '^[A-Za-z0-9_.+-]+\\.(x86_64|noarch|aarch64)' || true",
             "installed": "rpm -q {pkg} >/dev/null 2>&1",
             "base_pkgs": ["chrony", "python3", "curl", "jq", "rsync"]},
}


def parse_os_release(text):
    out = {}
    for line in (text or "").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def major(version_id):
    return (version_id or "").split(".")[0]


def classify_os(osr):
    """osr: dict from parse_os_release -> dict(id, version, family, support, pretty)."""
    oid = (osr.get("ID") or "").lower()
    ver = osr.get("VERSION_ID") or ""
    like = (osr.get("ID_LIKE") or "").lower().split()
    pretty = osr.get("PRETTY_NAME") or f"{oid} {ver}".strip()
    ent = OS_TABLE.get(oid)
    if ent:
        key = ver if ent["family"] == "debian" and oid == "ubuntu" else major(ver)
        sup = "supported" if key in ent["supported"] else "compat" if key in ent["compat"] else "unsupported"
        return {"id": oid, "version": ver, "family": ent["family"], "support": sup, "pretty": pretty}
    if any(x in like for x in ("rhel", "fedora", "centos")) and major(ver) in ("8", "9"):
        return {"id": oid, "version": ver, "family": "rhel", "support": "compat", "pretty": pretty}
    if "debian" in like or "ubuntu" in like:
        return {"id": oid, "version": ver, "family": "debian", "support": "unsupported", "pretty": pretty}
    return {"id": oid or "unknown", "version": ver, "family": "", "support": "unsupported", "pretty": pretty or "unknown"}


def short_name(os_id, version):
    return f"{OS_NAMES.get(os_id, os_id or '?')} {version or ''}".strip()


# Database high-availability strategies offered by the CloudStack wizard and the MySQL component.
DB_STRATEGIES = {
    "gtid_agent": {"name": "MySQL 8 GTID + semi-sync + auto-heal agent (2-3 nodes, VIP)", "playbook": "10-cloudstack-db.yml",
                   "nodes": [2, 3], "families": ["debian", "rhel"]},
    "pxc": {"name": "Percona XtraDB Cluster (3 nodes, Galera, VIP)", "playbook": "11-cloudstack-db-pxc.yml",
            "nodes": [3], "families": ["debian", "rhel"]},
    "single": {"name": "Single MySQL 8 server (lab / small)", "playbook": "12-cloudstack-db-single.yml",
               "nodes": [1], "families": ["debian", "rhel"]},
}


def db_options():
    return [{"id": k, **v} for k, v in DB_STRATEGIES.items()]
