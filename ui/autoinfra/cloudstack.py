"""CloudStack: 8-step deployment wizard (Ansible job) and zone/pod/cluster/host/storage/network management (CloudStack API)."""
import ipaddress, json, os, time

from flask import Blueprint, g, jsonify, render_template, request

from . import auth, catalog, csapi, db, drivers, inventory, jobs, packages, validators as V

bp = Blueprint("cloudstack", __name__)
STEPS = ["requirements", "management", "database", "mq", "systemvm", "zone", "cluster", "review"]
STEP_LABELS = ["Requirements", "Management Server", "Database", "Message Queue", "System VMs", "Zones & Pods", "Clusters & Hosts",
               "Review & Deploy"]
OS_CHOICES = {"ubuntu-24.04": ("debian", "Ubuntu 24.04 LTS"), "ubuntu-22.04": ("debian", "Ubuntu 22.04 LTS"),
              "rocky-9": ("rhel", "Rocky Linux 9"), "alma-9": ("rhel", "AlmaLinux 9"), "rhel-9": ("rhel", "RHEL 9"),
              "ol-9": ("rhel", "Oracle Linux 9")}
DEFAULTS = {
    "requirements": {"mode": "offline", "version": catalog.PRODUCTS["cloudstack"]["versions"][0], "os": "ubuntu-24.04",
                     "templates": True, "database": True, "mq": False, "monitoring": True, "examples": True, "deploy_type": "full"},
    "management": {"host_ids": [], "lb_host_ids": [], "hostname": "cloudstack-mgmt", "vip": "", "network": "", "netmask": "255.255.255.0",
                   "gateway": "", "timezone": "Asia/Kolkata", "ntp": "", "ssl": True, "firewall": True, "usage": False},
    "database": {"strategy": "gtid_agent", "host_ids": [], "vip": "", "port": 3306},
    "mq": {"enabled": False, "host_ids": []},
    "systemvm": {"template": "", "hypervisor": "KVM"},
    "zone": {"name": "Zone-1", "dns1": "", "dns2": "", "internal_dns1": "", "vlan_range": "100-200", "guest_cidr": "10.1.1.0/24",
             "pod_name": "Pod-1", "pod_gateway": "", "pod_netmask": "255.255.255.0", "pod_start": "", "pod_end": "",
             "public_gateway": "", "public_netmask": "255.255.255.0", "public_start": "", "public_end": "", "public_vlan": "untagged",
             "label_mgmt": "cloudbr0", "label_guest": "cloudbr0", "label_public": "cloudbr0", "internal_net": False},
    "cluster": {"clusters": [{"name": "Cluster-1", "host_ids": []}], "primary_server": "", "primary_path": "/export/primary",
                "secondary_server": "", "secondary_path": "/export/secondary", "kvm_auth": "key", "auto_bridge": True,
                "extra_storage": []},    # [{"kind": "primary"|"secondary", "server": ip, "path": "/export/x", "name": "primary-2"}]
}


def state():
    s = db.get_setting("cs_wizard") or {}
    out = {k: dict(v) for k, v in DEFAULTS.items()}
    for k, v in s.items():
        if k in out and isinstance(v, dict):
            out[k].update(v)
    return out


def _hosts_exist(ids, what):
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        return f"{what}: host_ids must be a list of host ids"
    missing = [i for i in ids if not db.one("SELECT id FROM hosts WHERE id=?", (i,))]
    return f"{what}: unknown host id(s) {missing}" if missing else None


def validate(step, d, full=None):
    full = full or state()
    e = {}
    if step == "requirements":
        e = V.collect(("mode", V.choice(d.get("mode"), ["offline", "online"], "mode")),
                      ("version", V.version(d.get("version"))),
                      ("os", V.choice(d.get("os"), list(OS_CHOICES), "OS")),
                      ("deploy_type", V.choice(d.get("deploy_type", "full"), ["full", "custom"], "deployment type")))
    elif step == "management":
        ids = d.get("host_ids", [])
        e = V.collect(("host_ids", _hosts_exist(ids, "management servers") or (None if 1 <= len(ids) <= 3 else "choose 1-3 management servers")),
                      ("hostname", V.hostname(d.get("hostname"))),
                      ("netmask", V.netmask(d.get("netmask")) if d.get("netmask") else None),
                      ("timezone", V.timezone(d.get("timezone"))))
        if d.get("network"):
            err = V.cidr(d["network"])
            if err:
                e["network"] = err
        if d.get("gateway"):
            err = V.ipv4(d["gateway"])
            if err:
                e["gateway"] = err
            elif d.get("network") and not V.cidr(d["network"]):
                err = V.ip_in(d["gateway"], d["network"])
                if err:
                    e["gateway"] = err
        # 2-3 servers without a VIP: CloudStack's own agent load balancing (global setting host = ip1,ip2,...)
        if d.get("vip"):
            # HAProxy binds VIP:8080/8250; CloudStack itself listens on 0.0.0.0:8080/8250 -> they cannot share a host
            lb = d.get("lb_host_ids", [])
            err = _hosts_exist(lb, "load balancer hosts")
            if err:
                e["lb_host_ids"] = err
            elif not 1 <= len(lb) <= 2:
                e["lb_host_ids"] = "choose 1-2 load balancer hosts for the VIP (HAProxy + keepalived); they may be the database hosts"
            elif set(lb) & set(ids if isinstance(ids, list) else []):
                e["lb_host_ids"] = "load balancer hosts must not be management servers (both use ports 8080 and 8250)"
        if d.get("vip"):
            err = V.ipv4(d["vip"])
            if err:
                e["vip"] = err
            elif db.one("SELECT id FROM hosts WHERE ip=?", (d["vip"],)):
                e["vip"] = "the VIP must be a free address, not a host IP"
        for n in [x.strip() for x in str(d.get("ntp", "")).split(",") if x.strip()]:
            err = V.host_or_ip(n)
            if err:
                e["ntp"] = err
    elif step == "database":
        st = drivers.DB_STRATEGIES.get(d.get("strategy"))
        if not st:
            e["strategy"] = f"strategy must be one of {', '.join(drivers.DB_STRATEGIES)}"
        ids = d.get("host_ids", [])
        err = _hosts_exist(ids, "database servers")
        if err:
            e["host_ids"] = err
        elif st and len(ids) not in st["nodes"]:
            e["host_ids"] = f"{st['name']} needs {' or '.join(map(str, st['nodes']))} node(s); {len(ids)} selected"
        if st and len(ids) > 1:
            if not d.get("vip"):
                e["vip"] = "a database VIP is required for an HA set"
            elif V.ipv4(d["vip"]):
                e["vip"] = V.ipv4(d["vip"])
            elif db.one("SELECT id FROM hosts WHERE ip=?", (d["vip"],)):
                e["vip"] = "the VIP must be a free address, not a host IP"
            mvip = full["management"].get("vip")
            if mvip and d.get("vip") == mvip:
                e["vip"] = "database VIP and management VIP must differ"
        err = V.port(d.get("port", 3306))
        if err:
            e["port"] = err
    elif step == "mq":
        if d.get("enabled"):
            ids = d.get("host_ids", [])
            err = _hosts_exist(ids, "RabbitMQ") or (None if len(ids) in (1, 3) else "RabbitMQ needs 1 or 3 nodes")
            if err:
                e["host_ids"] = err
    elif step == "systemvm":
        e = V.collect(("hypervisor", V.choice(d.get("hypervisor"), ["KVM"], "hypervisor")))
        t = d.get("template", "")
        if t and not db.one("SELECT id FROM repo_items WHERE repo IN ('cloudstack','templates') AND path=?", (t,)):
            e["template"] = "template not found in the master repository (upload it first)"
    elif step == "zone":
        e = V.collect(("name", V.name(d.get("name"), "zone name")), ("dns1", V.ipv4(d.get("dns1", ""))),
                      ("vlan_range", V.vlan_range(d.get("vlan_range"))), ("guest_cidr", V.cidr(d.get("guest_cidr", ""))),
                      ("pod_name", V.name(d.get("pod_name"), "pod name")),
                      ("pod_gateway", V.ipv4(d.get("pod_gateway", ""))), ("pod_netmask", V.netmask(d.get("pod_netmask", ""))),
                      ("public_gateway", V.ipv4(d.get("public_gateway", ""))), ("public_netmask", V.netmask(d.get("public_netmask", ""))),
                      ("label_mgmt", V.iface(d.get("label_mgmt"))), ("label_guest", V.iface(d.get("label_guest"))),
                      ("label_public", V.iface(d.get("label_public"))))
        for k in ("dns2", "internal_dns1"):
            if d.get(k) and V.ipv4(d[k]):
                e[k] = V.ipv4(d[k])
        if "pod_gateway" not in e and "pod_netmask" not in e:
            err = V.ip_range(d.get("pod_start", ""), d.get("pod_end", ""), d["pod_gateway"], d["pod_netmask"])
            if err:
                e["pod_range"] = err
        if "public_gateway" not in e and "public_netmask" not in e:
            err = V.ip_range(d.get("public_start", ""), d.get("public_end", ""), d["public_gateway"], d["public_netmask"])
            if err:
                e["public_range"] = err
        pv = str(d.get("public_vlan", "untagged"))
        if pv != "untagged" and V.vlan(pv):
            e["public_vlan"] = V.vlan(pv) + " (or 'untagged')"
        if d.get("internal_net"):
            # pod + public networks live on an internal bridge of the (single) KVM host, which is their gateway
            if not (d.get("label_mgmt") == d.get("label_guest") == d.get("label_public")):
                e["label_mgmt"] = "with the internal network all three labels must name the same internal bridge (e.g. cloudbr1)"
            elif d.get("label_mgmt") == INTERNAL_HOST_BRIDGE:
                e["label_mgmt"] = f"{INTERNAL_HOST_BRIDGE} carries the host's own IP; name the internal bridge differently (e.g. cloudbr1)"
            elif len(str(d.get("label_mgmt", ""))) > 12:
                e["label_mgmt"] = "internal bridge name: at most 12 characters"
            if "pod_gateway" not in e and "pod_netmask" not in e and "public_gateway" not in e and "public_netmask" not in e:
                pn = ipaddress.ip_network(f"{d['pod_gateway']}/{d['pod_netmask']}", strict=False)
                bn = ipaddress.ip_network(f"{d['public_gateway']}/{d['public_netmask']}", strict=False)
                if pn.overlaps(bn):
                    e["public_gateway"] = "with the internal network the pod and public networks must be different subnets"
                hosts_in = [h["ip"] for h in db.q("SELECT ip FROM hosts") if ipaddress.ip_address(h["ip"]) in pn or ipaddress.ip_address(h["ip"]) in bn]
                if hosts_in:
                    e["pod_gateway"] = f"the internal pod/public subnets must not contain host addresses ({', '.join(hosts_in)})"
        if "pod_range" not in e and "public_range" not in e and "pod_gateway" not in e and "public_gateway" not in e \
                and "pod_netmask" not in e and "public_netmask" not in e:
            a = (ipaddress.IPv4Address(d["pod_start"]), ipaddress.IPv4Address(d["pod_end"]))
            b = (ipaddress.IPv4Address(d["public_start"]), ipaddress.IPv4Address(d["public_end"]))
            if a[0] <= b[1] and b[0] <= a[1]:
                e["public_range"] = "public range overlaps the pod (system VM) range"
    elif step == "cluster":
        cl = d.get("clusters") or []
        if not cl:
            e["clusters"] = "add at least one cluster"
        names = set()
        all_ids = []
        for i, c in enumerate(cl):
            err = V.name(c.get("name"), "cluster name")
            if err:
                e[f"clusters.{i}.name"] = err
            if c.get("name") in names:
                e[f"clusters.{i}.name"] = "duplicate cluster name"
            names.add(c.get("name"))
            ids = c.get("host_ids", [])
            err = _hosts_exist(ids, f"cluster {c.get('name')}") or (None if ids else "each cluster needs at least one KVM host")
            if err:
                e[f"clusters.{i}.host_ids"] = err
            all_ids += ids if isinstance(ids, list) else []
        if len(all_ids) != len(set(all_ids)):
            e["clusters"] = "a KVM host can belong to one cluster only"
        for k in ("primary", "secondary"):
            err = V.host_or_ip(d.get(f"{k}_server", "")) if d.get(f"{k}_server") else f"{k} storage server is required"
            if err:
                e[f"{k}_server"] = err
            err = V.abspath(d.get(f"{k}_path", ""))
            if err:
                e[f"{k}_path"] = err
        z = full["zone"]
        if z.get("internal_net") and len(set(all_ids)) > 1 and not any(k in e for k in ("clusters",)):
            try:
                pn = ipaddress.ip_network(f"{z['pod_gateway']}/{z['pod_netmask']}", strict=False)
                lo, hi = ipaddress.IPv4Address(z["pod_start"]), ipaddress.IPv4Address(z["pod_end"])
                clash = [a for a in internal_member_ips(pn, len(set(all_ids)) - 1) if lo <= ipaddress.IPv4Address(a) <= hi or a == z["pod_gateway"]]
                if clash:
                    e["clusters"] = (f"internal network: the KVM hosts after the first get {', '.join(clash)} (top of the pod subnet) - "
                                     "end the pod range lower")
            except (KeyError, ValueError):
                pass
        ex = d.get("extra_storage") or []
        if not isinstance(ex, list):
            e["extra_storage"] = "extra_storage must be a list"
            ex = []
        seen = {"primary-nfs", "secondary-nfs"}
        for i, x in enumerate(ex):
            x = x if isinstance(x, dict) else {}
            err = (V.choice(x.get("kind"), ["primary", "secondary"], "kind") or V.host_or_ip(x.get("server", "")) or V.abspath(x.get("path", ""))
                   or V.name(x.get("name"), "storage name"))
            if not err and x["name"] in seen:
                err = f"duplicate storage name {x['name']}"
            if not err and (x["server"], x["path"]) in {(d.get("primary_server"), d.get("primary_path")), (d.get("secondary_server"), d.get("secondary_path"))}:
                err = "this export is already the main primary/secondary storage"
            if err:
                e[f"extra_storage.{i}"] = err
            seen.add(x.get("name"))
        err = V.choice(d.get("kvm_auth", "key"), ["key", "password"], "KVM authentication")
        if err:
            e["kvm_auth"] = err
    else:
        e["step"] = "unknown step"
    return e


def plan(s=None):
    """What 'Start CloudStack Deployment' will do (shown on the Review step and used to build the job)."""
    s = s or state()
    steps = [("00-preflight.yml", "Pre-flight checks"), ("01-common.yml", "Configure repositories, NTP and base packages")]
    if s["requirements"].get("database", True):
        st = drivers.DB_STRATEGIES[s["database"]["strategy"]]
        steps.append((st["playbook"], f"Install & configure MySQL ({st['name']})"))
    if s["mq"].get("enabled"):
        steps.append(("30-rabbitmq.yml", "Install & configure RabbitMQ"))
    steps.append(("20-cloudstack-mgmt.yml", "Install CloudStack management server"))
    if needs_lb(s):
        steps.append(("21-cloudstack-lb.yml", "HAProxy + keepalived management VIP"))
    if s["management"].get("ssl"):
        steps.append(("31-nginx.yml", "Configure Nginx and SSL (offline certificate)"))
    if s["management"].get("usage"):
        steps.append(("24-cloudstack-usage.yml", "Install CloudStack usage server"))
    steps.append(("22-nfs.yml", "Setup primary and secondary NFS storage"))
    steps.append(("23-cloudstack-kvm.yml", "Install KVM + CloudStack agent on hosts"))
    if s["systemvm"].get("template"):     # CloudStack 4.16+ also registers the template bundled in cloudstack-management
        steps.append(("25-systemvm-template.yml", "Seed system VM template into secondary storage"))
    if s["requirements"].get("monitoring"):
        steps.append(("41-zabbix-agent.yml", "Configure monitoring agents (Zabbix)"))
    return steps


def extra_vars(s):
    m, d, z = s["management"], s["database"], s["zone"]
    hosts = {h["id"]: h for h in db.q("SELECT * FROM hosts")}
    db_ips = [hosts[i]["ip"] for i in d["host_ids"] if i in hosts]
    mgmt_ips = [hosts[i]["ip"] for i in m["host_ids"] if i in hosts]
    single = d["strategy"] == "single" or len(db_ips) == 1
    ev = {
        "cs_version": s["requirements"]["version"], "cs_series": s["requirements"]["version"].rsplit(".", 1)[0],
        "db_strategy": d["strategy"], "db_port": int(d.get("port", 3306)), "db_ips": db_ips,
        "db_vip": d.get("vip") or (db_ips[0] if db_ips else ""), "db_single": single,
        "db_acs_admin_password": inventory.secret("db_acs_admin_password", 24),
        "cs_cloud_db_password": inventory.secret("cs_cloud_db_password", 24),
        "db_dbha_password": inventory.secret("db_dbha_password", 24), "db_vrrp_pass": inventory.secret("db_vrrp_pass", 8),
        "db_root_password": inventory.secret("db_root_password", 24),
        "cs_mgmt_vip": m.get("vip") or (mgmt_ips[0] if mgmt_ips else ""), "cs_mgmt_vrrp_pass": inventory.secret("cs_mgmt_vrrp_pass", 8),
        "cs_timezone": m.get("timezone", "UTC"), "ntp_server": (m.get("ntp") or "").split(",")[0].strip() or inventory.master_ip(),
        "cs_firewall": bool(m.get("firewall")), "cs_ssl": bool(m.get("ssl")), "cs_mgmt_hostname": m.get("hostname"),
        "nfs_exports": [{"name": "primary", "path": s["cluster"]["primary_path"]}, {"name": "secondary", "path": s["cluster"]["secondary_path"]}],
        "nfs_exports_by_host": nfs_exports_by_host(s, hosts),
        "systemvm_template_url": (inventory.repo_base() + "/" + ("cloudstack/" if not s["systemvm"]["template"].startswith("templates/") else "") +
                                  s["systemvm"]["template"]) if s["systemvm"].get("template") else "",
        "systemvm_secondary": f"{s['cluster']['secondary_server']}:{s['cluster']['secondary_path']}",
        "rabbitmq_password": inventory.secret("rabbitmq_password", 20), "zabbix_server": inventory.master_ip(),
        "kvm_bridge_name": z.get("label_mgmt", "cloudbr0"), "db_client_ips": mgmt_ips,
        "kvm_manage_network": bool(s["cluster"].get("auto_bridge", True)),
        "kvm_required_bridges": sorted({z.get(k) for k in ("label_mgmt", "label_guest", "label_public") if z.get(k)}),
        "nginx_hosts": "cs_mgmt", "zabbix_agent_hosts": "all", "cs_usage_hosts": "cs_mgmt[0]",
    }
    ev.update(internal_net_vars(s, hosts))
    return ev


INTERNAL_HOST_BRIDGE = "cloudbr0"


def internal_member_ips(pn, n):
    """Addresses for the member KVM hosts on the internal pod subnet: the top of the subnet, downwards."""
    top = int(pn.broadcast_address)
    return [str(ipaddress.IPv4Address(top - 1 - i)) for i in range(n)]


def _wizard_host_ids(s):
    return {i for ids in groups(s).values() for i in ids}


def internal_net_vars(s, hosts):
    """Zone option 'internal network': the KVM host gets an internal bridge that is the pod + public gateway, every other
    host (and the master) a route to those subnets via the KVM host, and the NFS server exports to the pod subnet."""
    z = s["zone"]
    kvm_ids = sorted({i for c in s["cluster"]["clusters"] for i in c["host_ids"]})
    if not z.get("internal_net") or not kvm_ids or kvm_ids[0] not in hosts:
        return {}
    pn = ipaddress.ip_network(f"{z['pod_gateway']}/{z['pod_netmask']}", strict=False)
    bn = ipaddress.ip_network(f"{z['public_gateway']}/{z['public_netmask']}", strict=False)
    kvm_ip = hosts[kvm_ids[0]]["ip"]
    peers = [hosts[i]["ip"] for i in kvm_ids if i in hosts]
    return {"kvm_bridge_name": INTERNAL_HOST_BRIDGE,
            "kvm_internal_net": {"bridge": z["label_mgmt"], "addresses": [f"{z['pod_gateway']}/{pn.prefixlen}", f"{z['public_gateway']}/{bn.prefixlen}"],
                                 "nat_cidrs": [str(pn), str(bn)],
                                 # hosts that get a route back must see the system VMs' real addresses: CloudStack checks
                                 # the agent certificate against the client IP ("Certificate ownership verification failed")
                                 "nat_exempt": sorted({h["ip"] for hid, h in hosts.items() if hid in _wizard_host_ids(s) and h["ip"] != kvm_ip}),
                                 # first KVM host = gateway; the others join the bridge over VXLAN
                                 "gateway": kvm_ip, "peers": peers,
                                 # the agent needs an address on its private bridge: members get one from the top of the pod subnet
                                 "host_addresses": {ip: f"{a}/{pn.prefixlen}" for ip, a in zip(peers[1:], internal_member_ips(pn, len(peers) - 1))},
                                 # explicit, per-host MAC: cloned VMs share /etc/machine-id, so systemd gives every bridge the same MAC
                                 "macs": {ip: "02:a1:" + ":".join(f"{int(o):02x}" for o in ip.split(".")) for ip in peers}},
            # KVM hosts are on the internal bridge themselves: no route for them (it would hide the connected subnet)
            "autoinfra_static_routes": [{"to": str(pn), "via": kvm_ip, "except": peers}, {"to": str(bn), "via": kvm_ip, "except": peers}],
            "nfs_extra_cidrs": [str(pn)]}


def zone_spec(s, kvm_password=None):
    z, c = s["zone"], s["cluster"]
    hosts = {h["id"]: h for h in db.q("SELECT * FROM hosts")}
    clusters = []
    for cl in c["clusters"]:
        clusters.append({"name": cl["name"], "hosts": [{"ip": hosts[i]["ip"], "hostname": hosts[i].get("hostname"), "user": "root",
                                                       "password": kvm_password if c.get("kvm_auth") == "password" else None}
                                                      for i in cl["host_ids"] if i in hosts]})
    return {"zone": {"name": z["name"], "dns1": z["dns1"], "dns2": z.get("dns2"), "internal_dns1": z.get("internal_dns1") or z["dns1"],
                     "vlan_range": z["vlan_range"], "guest_cidr": z["guest_cidr"]},
            "pod": {"name": z["pod_name"], "start": z["pod_start"], "end": z["pod_end"], "netmask": z["pod_netmask"], "gateway": z["pod_gateway"]},
            "public": {"start": z["public_start"], "end": z["public_end"], "netmask": z["public_netmask"], "gateway": z["public_gateway"],
                       "vlan": z.get("public_vlan") or "untagged"},
            "kvm_labels": {"management": z["label_mgmt"], "guest": z["label_guest"], "public": z["label_public"]},
            "clusters": clusters, "primary_nfs": f"{c['primary_server']}{c['primary_path']}",
            "secondary_nfs": f"{c['secondary_server']}{c['secondary_path']}", "management_vip": mgmt_host_setting(s),
            "extra_primary": [{"name": x["name"], "nfs": f"{x['server']}{x['path']}"} for x in c.get("extra_storage") or [] if x["kind"] == "primary"],
            "extra_secondary": [{"name": x["name"], "nfs": f"{x['server']}{x['path']}"} for x in c.get("extra_storage") or [] if x["kind"] == "secondary"]}


def _storage_list(s):
    c = s["cluster"]
    out = [("primary", c["primary_server"], c["primary_path"], "primary-nfs"), ("secondary", c["secondary_server"], c["secondary_path"], "secondary-nfs")]
    return out + [(x["kind"], x["server"], x["path"], x["name"]) for x in c.get("extra_storage") or []]


def nfs_exports_by_host(s, hosts):
    """Every inventory NFS server exports only its own paths (servers outside the inventory, e.g. the master, are set up
    with master/setup-nfs.sh)."""
    out = {}
    for h in hosts.values():
        mine = [{"name": n, "path": p} for _, srv, p, n in _storage_list(s) if srv in (h["ip"], h.get("hostname"))]
        if mine:
            out[h["ip"]] = mine
    return out


def needs_lb(s):
    """HAProxy + keepalived only in front of a VIP."""
    return bool(s["management"].get("vip"))


def mgmt_host_setting(s):
    """CloudStack global setting 'host' (what agents and system VMs connect to): the VIP, or the list of management
    servers (agent-side load balancing / failover, indirect.agent.lb.algorithm)."""
    hosts = {h["id"]: h["ip"] for h in db.q("SELECT id, ip FROM hosts")}
    ips = [hosts[i] for i in s["management"]["host_ids"] if i in hosts]
    return s["management"].get("vip") or ",".join(ips)


def needs_host_setting(s):
    """The setting must be in place BEFORE KVM hosts are added and the servers restarted afterwards."""
    return needs_lb(s) or len(s["management"]["host_ids"]) > 1


def groups(s):
    g_ = {"cs_mgmt": s["management"]["host_ids"], "cs_db": s["database"]["host_ids"],
          "cs_kvm": sorted({i for c in s["cluster"]["clusters"] for i in c["host_ids"]})}
    if needs_lb(s):
        g_["cs_lb"] = s["management"].get("lb_host_ids", [])
    if s["mq"].get("enabled"):
        g_["mq"] = s["mq"]["host_ids"]
    servers = {srv for _, srv, _, _ in _storage_list(s)}
    nfs = [h["id"] for h in db.q("SELECT id, ip, hostname FROM hosts") if h["ip"] in servers or h["hostname"] in servers]
    g_["cs_nfs"] = nfs
    return g_


def api_client():
    url = db.get_setting("cs_api_url")
    if not url:
        raise csapi.CSError("CloudStack API is not configured - Settings > CloudStack API (or deploy with the wizard)")
    return csapi.CS(url, db.get_setting("cs_api_key"), inventory.secret_get("cs_secret_key"),
                    poll_interval=float(db.get_setting("cs_poll_interval", 2)))


def _api_session(log, url, admin_user, admin_password, wait=None):
    """Client with API keys: the saved ones, else log in (a fresh management server needs minutes before it answers)."""
    wait = float(db.get_setting("cs_api_wait") or os.environ.get("AUTOINFRA_CS_API_WAIT", 900)) if wait is None else wait
    cs = csapi.CS(url, poll_interval=float(db.get_setting("cs_poll_interval", 2)))
    saved = db.get_setting("cs_api_key") and inventory.secret_get("cs_secret_key") and db.get_setting("cs_api_url") == url
    if saved:   # saved keys first: a re-run must not register new keys (that invalidates the old ones)
        cs.api_key, cs.secret = db.get_setting("cs_api_key"), inventory.secret_get("cs_secret_key")
        try:
            cs.wait_ready(wait, log)
            return cs
        except csapi.CSError as e:
            if not csapi.is_auth_error(e):
                raise
            if not admin_password:
                raise csapi.CSError("the saved CloudStack API keys were rejected (regenerated in CloudStack?) - enter the CloudStack admin "
                                    "password on the Review step and resume the deployment") from None
            log("[api] the saved API keys were rejected (regenerated in CloudStack?) - logging in again")
            cs = csapi.CS(url, poll_interval=float(db.get_setting("cs_poll_interval", 2)))
    log(f"[api] logging in to {url} as {admin_user}")
    key, sec = cs.login_retry(admin_user, admin_password or "password", wait, log)
    db.set_setting("cs_api_url", url)
    db.set_setting("cs_api_key", key)
    inventory.secret_set("cs_secret_key", sec)
    log("[api] API keys registered and saved (Settings > CloudStack API)")
    return cs


@jobs.register("cs_api_prepare")
def cs_api_prepare(log, vip, url=None, admin_user="admin", admin_password=None):
    url = url or db.get_setting("cs_api_url") or f"http://{vip.split(',')[0]}:8080"
    cs = _api_session(log, url, admin_user, admin_password)
    cs.run("updateConfiguration", name="host", value=vip)
    log(f"[api] global setting host = {vip} (management servers are restarted next)")
    return True


@jobs.register("cs_zone_bootstrap")
def cs_zone_bootstrap(log, spec, url=None, admin_user="admin", admin_password=None):
    url = url or db.get_setting("cs_api_url") or f"http://{spec['management_vip'].split(',')[0]}:8080"
    cs = _api_session(log, url, admin_user, admin_password)
    csapi.bootstrap_zone(cs, spec, log)
    return True


# ------------------------------------------------------------------ pages
@bp.route("/cloudstack/wizard")
@auth.login_required
def wizard_page():
    return render_template("cs_wizard.html", steps=list(zip(STEPS, STEP_LABELS)), os_choices=OS_CHOICES,
                           db_options=drivers.db_options(), versions=catalog.PRODUCTS["cloudstack"]["versions"])


@bp.route("/cloudstack/infra")
@auth.login_required
def infra_page():
    return render_template("cs_infra.html")


# ------------------------------------------------------------------ wizard API
@bp.route("/api/cs/wizard")
@auth.login_required
def get_wizard():
    s = state()
    fam = OS_CHOICES[s["requirements"]["os"]][0]
    return jsonify(state=s, steps=STEPS, labels=STEP_LABELS, plan=[{"playbook": p, "label": l} for p, l in plan(s)],
                   required=packages.presence("cloudstack", fam), db_options=drivers.db_options())


@bp.route("/api/cs/wizard/<step>", methods=["POST"])
@auth.role_required("operator")
def save_step(step):
    if step not in STEPS[:-1]:
        return jsonify(error="unknown step"), 404
    d = request.get_json(silent=True) or {}
    s = state()
    merged = dict(s[step])
    merged.update({k: v for k, v in d.items() if k in DEFAULTS[step]})
    errs = validate(step, merged, s)
    if errs:
        return jsonify(error="validation failed", fields=errs), 400
    raw = db.get_setting("cs_wizard") or {}
    raw[step] = merged
    db.set_setting("cs_wizard", raw)
    auth.audit("cs.wizard.save", step)
    return jsonify(ok=True, state=state())


@bp.route("/api/cs/wizard/reset", methods=["POST"])
@auth.role_required("operator")
def reset_wizard():
    db.set_setting("cs_wizard", {})
    auth.audit("cs.wizard.reset")
    return jsonify(ok=True)


def preflight_errors(s):
    errs = {}
    for st in STEPS[:-1]:
        e = validate(st, s[st], s)
        if e:
            errs[st] = e
    if not errs:
        all_ids = {i for ids in groups(s).values() for i in ids}
        bad = [h["ip"] for h in db.q("SELECT id, ip, status FROM hosts") if h["id"] in all_ids and h["status"] not in ("ready", "ready-with-warnings")]
        if bad:
            errs["hosts"] = {"hosts": f"run Pre-Deployment checks first; not ready: {', '.join(bad)}"}
        fam_want = OS_CHOICES[s["requirements"]["os"]][0]
        wrong = [h["ip"] for h in db.q("SELECT id, ip, os_family FROM hosts") if h["id"] in all_ids and h["os_family"] and h["os_family"] != fam_want]
        if wrong:
            errs.setdefault("hosts", {})["os"] = f"hosts with a different OS family than {s['requirements']['os']}: {', '.join(wrong)}"
    return errs


@bp.route("/api/cs/wizard/deploy", methods=["POST"])
@auth.role_required("operator")
def deploy():
    j = request.get_json(silent=True) or {}
    s = state()
    errs = preflight_errors(s)
    if errs and not j.get("force"):
        return jsonify(error="the wizard has errors", steps=errs), 400
    fam = OS_CHOICES[s["requirements"]["os"]][0]
    if not j.get("force") and s["requirements"]["mode"] == "offline":
        miss = [i for i in packages.presence("cloudstack", fam) if not i["present"] and i["id"] in ("cloudstack-management", "cloudstack-agent", "mysql")]
        if miss:
            return jsonify(error="required packages are missing from the master repository: " + ", ".join(m["label"] for m in miss)), 409
    grp = groups(s)
    used = {i for ids in grp.values() for i in ids}
    hosts = [h for h in db.q("SELECT * FROM hosts") if h["id"] in used]      # 'hosts: all' plays must not touch unrelated machines
    hosts = [dict(h, roles="") for h in hosts]                               # groups come from the wizard only, not from host roles
    pbs = plan(s) + ([("27-cloudstack-mgmt-restart.yml", "Restart management servers (apply the 'host' setting)")] if needs_host_setting(s) else [])
    steps, wd = inventory.playbook_steps(pbs, hosts, extra_vars=extra_vars(s), extra_groups=grp)
    kvm_pw = j.get("kvm_password") if s["cluster"].get("kvm_auth") == "password" else None
    if needs_host_setting(s):   # agents/system VMs must know the VIP or every server: set it BEFORE hosts are added, then restart
        restart = steps.pop()
        steps.append({"name": "Point CloudStack at the management VIP / servers (global setting 'host')", "fn": "cs_api_prepare",
                      "args": {"vip": mgmt_host_setting(s), "admin_password": j.get("admin_password")}})
        steps.append(restart)
    steps.append({"name": "Configure zones, pods, clusters, hosts and storage (CloudStack API)", "fn": "cs_zone_bootstrap",
                  "args": {"spec": zone_spec(s, kvm_pw), "admin_password": j.get("admin_password")}})
    title, meta = f"Deploy Apache CloudStack {s['requirements']['version']}", {"workdir": wd, "plan": [p for p, _ in plan(s)]}
    if j.get("resume"):
        start_at, prev, err = resume_point([x["name"] for x in steps])
        if err:
            return jsonify(error=err), 409
        steps = steps[start_at:]
        title += f" (resumed at step {start_at + 1} of job #{prev})"
        meta.update(resumed_from=prev, start_at=start_at)
    jid = jobs.start("deploy-cloudstack", title, steps, meta=meta, secrets=[kvm_pw, j.get("admin_password")])
    auth.audit("cs.deploy", s["requirements"]["version"], f"job {jid}")
    return jsonify(job_id=jid), 202


def resume_point(names):
    """(index of the first step to run, previous job id, error). Every playbook and the zone bootstrap are idempotent, so
    resuming means: skip the steps the last failed deployment finished, provided the plan is still the same."""
    prev = db.one("SELECT id, state, steps, meta FROM jobs WHERE kind='deploy-cloudstack' ORDER BY id DESC LIMIT 1")
    if not prev or prev["state"] != "failed":
        return 0, None, "nothing to resume: the last CloudStack deployment did not fail - use Start CloudStack Deployment"
    st = json.loads(prev["steps"] or "[]")
    bad = next((i for i, x in enumerate(st) if x["state"] == "failed"), None)
    if bad is None:
        return 0, None, f"job #{prev['id']} has no failed step to resume from"
    off = int((json.loads(prev["meta"] or "{}")).get("start_at") or 0)   # that job was itself a resume
    if [x["name"] for x in st] != names[off:]:
        return 0, None, f"the wizard changed since job #{prev['id']} - its steps differ; start a new deployment"
    return off + bad, prev["id"], None


# ------------------------------------------------------------------ infrastructure API (live CloudStack)
@bp.route("/api/cs/summary")
@auth.login_required
def summary():
    try:
        cs = api_client()
        out = {}
        for r in ("zones", "pods", "clusters", "hosts", "storagepools", "imagestores", "systemvms", "virtualmachines"):
            cmd, key = csapi.RESOURCES[r]
            out[r] = len(cs.items(cmd, key, **LIST_DEFAULTS_FOR(r)))
        cap = cs.items("listStoragePools", "storagepool")
        out["primary_bytes"] = sum(int(p.get("disksizetotal", 0) or 0) for p in cap)
        types = {}
        for p in cap:
            t = p.get("type") or "NFS"
            types[t] = types.get(t, 0) + 1
        out["primary_types"] = types
        return jsonify(configured=True, **out)
    except csapi.CSError as e:
        return jsonify(configured=False, error=str(e))


def LIST_DEFAULTS_FOR(r):  # noqa: N802
    return dict(csapi.LIST_DEFAULTS.get(r, {}))


@bp.route("/api/cs/<resource>")
@auth.login_required
def list_resource(resource):
    if resource not in csapi.RESOURCES:
        return jsonify(error="unknown resource"), 404
    cmd, key = csapi.RESOURCES[resource]
    params = LIST_DEFAULTS_FOR(resource)
    for k in ("zoneid", "podid", "clusterid", "keyword", "type"):
        if request.args.get(k):
            params[k] = request.args[k]
    try:
        return jsonify(items=api_client().items(cmd, key, **params))
    except csapi.CSError as e:
        return jsonify(error=str(e), items=[]), 502


CREATE = {
    "zones": ("createZone", {"name": V.name, "dns1": V.ipv4, "internaldns1": V.ipv4}, {"networktype": "Advanced"}),
    "pods": ("createPod", {"zoneid": None, "name": V.name, "gateway": V.ipv4, "netmask": V.netmask, "startip": V.ipv4, "endip": V.ipv4}, {}),
    "clusters": ("addCluster", {"zoneid": None, "podid": None, "clustername": V.name}, {"clustertype": "CloudManaged", "hypervisor": "KVM"}),
    "hosts": ("addHost", {"zoneid": None, "podid": None, "clusterid": None, "url": None}, {"hypervisor": "KVM", "username": "root"}),
    "storagepools": ("createStoragePool", {"zoneid": None, "podid": None, "clusterid": None, "name": V.name, "url": None},
                     {"scope": "cluster", "hypervisor": "KVM"}),
    "imagestores": ("addImageStore", {"zoneid": None, "name": V.name, "url": V.nfs_url}, {"provider": "NFS"}),
    "vlanipranges": ("createVlanIpRange", {"zoneid": None, "gateway": V.ipv4, "netmask": V.netmask, "startip": V.ipv4, "endip": V.ipv4},
                     {"forvirtualnetwork": "true"}),
    "serviceofferings": ("createServiceOffering", {"name": V.name, "displaytext": None, "cpunumber": None, "cpuspeed": None, "memory": None}, {}),
}


@bp.route("/api/cs/<resource>", methods=["POST"])
@auth.role_required("operator")
def create_resource(resource):
    if resource not in CREATE:
        return jsonify(error="cannot create this resource here"), 404
    cmd, fields, fixed = CREATE[resource]
    j = request.get_json(silent=True) or {}
    errs = {}
    for f, fn in fields.items():
        if f not in j or j[f] in ("", None):
            errs[f] = f"{f} is required"
        elif fn:
            e = fn(j[f])
            if e:
                errs[f] = e
    if resource == "hosts" and not str(j.get("url", "")).startswith("http://"):
        errs["url"] = "url must be http://<host ip>"
    if resource == "storagepools" and V.nfs_url(j.get("url", "")):
        errs["url"] = V.nfs_url(j.get("url", ""))
    if resource == "serviceofferings":
        for f, lo, hi in (("cpunumber", 1, 256), ("cpuspeed", 100, 10000), ("memory", 128, 4194304)):
            if f not in errs:
                e = V.int_between(j.get(f), lo, hi, f)
                if e:
                    errs[f] = e
    if resource in ("pods", "vlanipranges") and not errs:
        e = V.ip_range(j["startip"], j["endip"], j["gateway"], j["netmask"])
        if e:
            errs["startip"] = e
    if errs:
        return jsonify(error="validation failed", fields=errs), 400
    params = dict(fixed)
    params.update({k: j[k] for k in j if k in fields or k in ("password", "vlan", "dns2", "internaldns2", "guestcidraddress", "tags",
                                                                "storagetype", "offerha", "endip", "domainid")})
    try:
        res = api_client().run(cmd, **params)
    except csapi.CSError as e:
        auth.audit(f"cs.create.{resource}", j.get("name") or j.get("clustername") or j.get("url"), str(e), ok=False)
        return jsonify(error=str(e)), 502
    auth.audit(f"cs.create.{resource}", j.get("name") or j.get("clustername") or j.get("url"))
    return jsonify(result=res), 201


@bp.route("/api/cs/<resource>/<rid>/<action>", methods=["POST"])
@auth.role_required("operator")
def resource_action(resource, rid, action):
    a = csapi.ACTIONS.get((resource, action))
    if not a:
        return jsonify(error=f"action '{action}' is not available for {resource}"), 404
    if action in ("delete", "destroy") and not auth.can("admin"):
        return jsonify(error="admin role required to delete"), 403
    cmd, idp, extra = a
    try:
        res = api_client().run(cmd, **{idp: rid}, **extra)
    except csapi.CSError as e:
        auth.audit(f"cs.{resource}.{action}", rid, str(e), ok=False)
        return jsonify(error=str(e)), 502
    auth.audit(f"cs.{resource}.{action}", rid)
    return jsonify(result=res)


@bp.route("/api/cs/topology")
@auth.login_required
def topology():
    try:
        cs = api_client()
        zones = cs.items("listZones", "zone")
        out = []
        for z in zones:
            pods = []
            for p in cs.items("listPods", "pod", zoneid=z["id"]):
                cls = []
                for c in cs.items("listClusters", "cluster", zoneid=z["id"]):
                    if c.get("podid") != p["id"]:
                        continue
                    hosts = [{"name": h.get("name"), "ip": h.get("ipaddress", h.get("name")), "state": h.get("state")}
                             for h in cs.items("listHosts", "host", zoneid=z["id"], type="Routing") if h.get("clusterid") == c["id"]]
                    cls.append({"name": c["name"], "id": c["id"], "hosts": hosts})
                pods.append({"name": p["name"], "id": p["id"], "clusters": cls})
            out.append({"name": z["name"], "id": z["id"], "state": z.get("allocationstate"), "pods": pods})
        return jsonify(configured=True, zones=out)
    except csapi.CSError as e:
        return jsonify(configured=False, error=str(e), zones=[])
