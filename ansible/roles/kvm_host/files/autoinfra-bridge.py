#!/usr/bin/env python3
"""AutoInfra: put a KVM host's management IP on a Linux bridge (cloudbr0) without a console visit.

  plan     detect the NIC that carries the host IP, its prefix, gateway, DNS, MAC -> JSON (changes nothing)
  write    back up the network config and write the bridge config (netplan on Ubuntu, NetworkManager on RHEL family)
  apply    activate it, then wait for 'confirm'; if no confirmation arrives in time, restore the old config (auto rollback)
  confirm  called by the master over SSH after the switch: proves the host is still reachable through the bridge
  rollback restore the saved config by hand

The same address, gateway, DNS and MAC move from the NIC to the bridge, so the host keeps its identity.
ROOT (env AUTOINFRA_BRIDGE_ROOT) prefixes every path - used by the tests.
"""
import argparse, glob, json, os, shutil, subprocess, sys, time

ROOT = os.environ.get("AUTOINFRA_BRIDGE_ROOT", "")
NETPLAN_DIR = ROOT + "/etc/netplan"
STATE = ROOT + "/var/lib/autoinfra-bridge"
OKFILE = ROOT + "/run/autoinfra-bridge.ok"
CLOUD_DIR = ROOT + "/etc/cloud/cloud.cfg.d"
CLOUD_FILE = CLOUD_DIR + "/99-autoinfra-disable-network-config.cfg"
OUR_FILE = NETPLAN_DIR + "/60-autoinfra-cloudbr.yaml"
IP_KEYS = ("addresses", "dhcp4", "dhcp6", "gateway4", "gateway6", "routes", "routing-policy", "nameservers", "dhcp4-overrides",
           "dhcp6-overrides", "accept-ra")


def sh(cmd, check=False):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd)}\n{r.stdout}{r.stderr}")
    return r


def have(binary):
    return shutil.which(binary) is not None


def log(msg):
    os.makedirs(STATE, exist_ok=True)
    with open(STATE + "/last.log", "a") as fh:
        fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    print(msg, flush=True)


def dns_servers(dev):
    out = []
    r = sh(["resolvectl", "dns", dev]) if have("resolvectl") else None
    if r is not None and r.returncode == 0 and ":" in r.stdout:
        out = r.stdout.split(":", 1)[1].split()
    if not out:
        for p in (ROOT + "/run/systemd/resolve/resolv.conf", ROOT + "/etc/resolv.conf"):
            if os.path.exists(p):
                out = [ln.split()[1] for ln in open(p) if ln.startswith("nameserver") and len(ln.split()) > 1]
                if out:
                    break
    return [d for d in out if not d.startswith("127.") and ":" not in d][:3]


def backend():
    if have("netplan") and glob.glob(NETPLAN_DIR + "/*.yaml"):
        return "netplan"
    if have("nmcli") and sh(["nmcli", "-t", "-f", "RUNNING", "general"]).stdout.strip() == "running":
        return "nm"
    return "unsupported"


def plan(ip, bridge, extra):
    addrs = json.loads(sh(["ip", "-j", "-4", "addr", "show"], check=True).stdout)
    links = {l["ifname"]: l for l in json.loads(sh(["ip", "-j", "-d", "link", "show"], check=True).stdout)}
    dev = prefix = None
    for d in addrs:
        for a in d.get("addr_info", []):
            if a.get("local") == ip:
                dev, prefix = d["ifname"], a["prefixlen"]
    p = {"ip": ip, "bridge": bridge, "backend": backend(), "extra": {}, "problems": []}
    if not dev:
        p.update(status="error")
        p["problems"].append(f"no interface on this host carries {ip}")
        return p
    kind = (links.get(dev, {}).get("linkinfo") or {}).get("info_kind", "")
    gw = None
    for r in json.loads(sh(["ip", "-j", "-4", "route", "show", "default"]).stdout or "[]"):
        if r.get("dev") == dev and r.get("gateway"):
            gw = r["gateway"]
            break
    p.update(dev=dev, prefix=prefix, kind=kind or "ethernet", gateway=gw, dns=dns_servers(dev),
             mac=links.get(dev, {}).get("address"), mtu=links.get(dev, {}).get("mtu"))
    if dev == bridge:
        p["status"] = "present"
    elif kind == "bridge":
        p["status"] = "error"
        p["problems"].append(f"{ip} is already on another bridge ({dev}); use '{dev}' as the KVM traffic label instead of '{bridge}'")
    elif bridge in links:
        p["status"] = "error"
        p["problems"].append(f"bridge {bridge} exists but does not carry {ip}; fix it by hand or choose another label")
    elif p["backend"] == "unsupported":
        p["status"] = "error"
        p["problems"].append("neither netplan nor NetworkManager manages this host's network; create the bridge by hand")
    elif kind not in ("", "bond", "vlan"):
        p["status"] = "error"
        p["problems"].append(f"{dev} is a '{kind}' device; automatic bridging supports ethernet, bond and vlan devices")
    else:
        p["status"] = "needed"
    for name, nic in (extra or {}).items():            # extra bridges without an IP (e.g. guest traffic on a 2nd NIC)
        if name in links:
            continue
        if nic not in links:
            p["problems"].append(f"extra bridge {name}: interface {nic} does not exist")
        elif nic == dev or any(d["ifname"] == nic and d.get("addr_info") for d in addrs):
            p["problems"].append(f"extra bridge {name}: interface {nic} carries an IP address; pick an unused NIC")
        else:
            p["extra"][name] = nic
    if p["problems"]:
        p["status"] = "error"
    elif p["status"] == "present" and p["extra"]:
        p["status"] = "needed"
    return p


# ------------------------------------------------------------------ netplan
def netplan_write(p):
    import yaml
    bdir = STATE + "/backup/netplan"
    shutil.rmtree(STATE + "/backup", ignore_errors=True)
    os.makedirs(bdir)
    dev, found, moved_routes = p.get("dev"), False, []
    for f in sorted(glob.glob(NETPLAN_DIR + "/*.yaml")):
        shutil.copy2(f, bdir)
        if p["bridge"] == dev:                     # only extra bridges are added
            continue
        doc = yaml.safe_load(open(f)) or {}
        net = doc.get("network") or {}
        changed = False
        for sec in ("ethernets", "bonds", "vlans"):
            d = (net.get(sec) or {}).get(dev)
            if isinstance(d, dict):
                found = True
                moved_routes += [r for r in d.get("routes") or [] if r.get("to") not in ("default", "0.0.0.0/0")]
                for k in IP_KEYS:
                    d.pop(k, None)
                d["dhcp4"], d["dhcp6"] = False, False
                changed = True
        if changed:
            with open(f, "w") as fh:
                yaml.safe_dump(doc, fh, default_flow_style=False, sort_keys=False)
            os.chmod(f, 0o600)
    net = {"version": 2, "bridges": {}}
    eth = {}
    if p["bridge"] != dev:
        if not found:
            if p["kind"] != "ethernet":
                netplan_restore()
                raise SystemExit(f"{dev} ({p['kind']}) is not defined in /etc/netplan; create the bridge by hand")
            eth[dev] = {"dhcp4": False, "dhcp6": False}
        br = {"interfaces": [dev], "addresses": [f"{p['ip']}/{p['prefix']}"], "dhcp4": False, "dhcp6": False,
              "parameters": {"stp": False, "forward-delay": 0}}
        if p.get("mac"):
            br["macaddress"] = p["mac"]
        if p.get("mtu") and p["mtu"] != 1500:
            br["mtu"] = p["mtu"]
        routes = ([{"to": "default", "via": p["gateway"]}] if p.get("gateway") else []) + moved_routes
        if routes:
            br["routes"] = routes
        if p.get("dns"):
            br["nameservers"] = {"addresses": p["dns"]}
        net["bridges"][p["bridge"]] = br
    for name, nic in p["extra"].items():
        eth.setdefault(nic, {"dhcp4": False, "dhcp6": False})
        net["bridges"][name] = {"interfaces": [nic], "dhcp4": False, "dhcp6": False, "parameters": {"stp": False, "forward-delay": 0}}
    if eth:
        net["ethernets"] = eth
    with open(OUR_FILE, "w") as fh:
        fh.write("# written by AutoInfra (autoinfra-bridge) - the old files are in /var/lib/autoinfra-bridge/backup\n")
        yaml.safe_dump({"network": net}, fh, default_flow_style=False, sort_keys=False)
    os.chmod(OUR_FILE, 0o600)
    if os.path.isdir(CLOUD_DIR):                    # cloud-init must not rewrite its netplan file at the next boot
        with open(CLOUD_FILE, "w") as fh:
            fh.write("network: {config: disabled}\n")
    r = sh(["netplan", "generate"] + (["--root-dir", ROOT] if ROOT else []))
    if r.returncode != 0:
        netplan_restore()
        raise SystemExit("netplan rejected the generated configuration (old files restored):\n" + r.stdout + r.stderr)


def netplan_restore():
    bdir = STATE + "/backup/netplan"
    if not os.path.isdir(bdir):
        return False
    for f in glob.glob(NETPLAN_DIR + "/*.yaml"):
        os.remove(f)
    for f in glob.glob(bdir + "/*.yaml"):
        shutil.copy2(f, NETPLAN_DIR)
    if os.path.exists(CLOUD_FILE):
        os.remove(CLOUD_FILE)
    return True


# ------------------------------------------------------------------ NetworkManager (RHEL family)
def nm_write(p):
    bdir = STATE + "/backup/nm"
    shutil.rmtree(STATE + "/backup", ignore_errors=True)
    os.makedirs(bdir)
    cmds, meta = [], {"bridges": [], "profiles": [], "old": None}
    dev = p.get("dev")

    def profile(nic):
        name = sh(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", nic]).stdout.strip()
        if not name:
            return None
        for ln in sh(["nmcli", "-t", "-f", "NAME,FILENAME", "connection", "show"]).stdout.splitlines():
            n, _, fn = ln.partition(":")
            if n.replace("\\:", ":") == name and fn and os.path.exists(fn):
                shutil.copy2(fn, bdir)
                meta["profiles"].append({"name": name, "file": fn})
        return name

    def bridge_cmd(name, ipcfg):
        meta["bridges"].append(name)
        return ["nmcli", "connection", "add", "type", "bridge", "ifname", name, "con-name", name, "bridge.stp", "no",
                "connection.autoconnect", "yes", "connection.autoconnect-slaves", "1", "ipv6.method", "disabled"] + ipcfg

    if p["bridge"] != dev:
        old = profile(dev)
        if not old:
            raise SystemExit(f"NetworkManager has no active connection on {dev}; create the bridge by hand")
        meta["old"] = old
        ipcfg = ["ipv4.method", "manual", "ipv4.addresses", f"{p['ip']}/{p['prefix']}"]
        if p.get("gateway"):
            ipcfg += ["ipv4.gateway", p["gateway"]]
        if p.get("dns"):
            ipcfg += ["ipv4.dns", ",".join(p["dns"])]
        if p.get("mac"):
            ipcfg += ["bridge.mac-address", p["mac"]]
        cmds.append(bridge_cmd(p["bridge"], ipcfg))
        cmds.append(["nmcli", "connection", "modify", old, "connection.master", p["bridge"], "connection.slave-type", "bridge",
                     "connection.autoconnect", "yes"])
        meta["up"] = [p["bridge"], old]
    else:
        meta["up"] = []
    for name, nic in p["extra"].items():
        old = profile(nic)
        cmds.append(bridge_cmd(name, ["ipv4.method", "disabled"]))
        if old:
            cmds.append(["nmcli", "connection", "modify", old, "connection.master", name, "connection.slave-type", "bridge",
                         "connection.autoconnect", "yes"])
            meta["up"] += [name, old]
        else:
            cmds.append(["nmcli", "connection", "add", "type", "bridge-slave", "ifname", nic, "master", name, "con-name", f"{name}-port-{nic}"])
            meta["bridges"].append(f"{name}-port-{nic}")
            meta["up"] += [name, f"{name}-port-{nic}"]
    meta["cmds"] = cmds
    json.dump(meta, open(STATE + "/nm.json", "w"))


def nm_apply():
    meta = json.load(open(STATE + "/nm.json"))
    for c in meta["cmds"]:
        sh(c, check=True)
    for name in meta["up"]:
        sh(["nmcli", "connection", "up", name])


def nm_restore():
    if not os.path.exists(STATE + "/nm.json"):
        return False
    meta = json.load(open(STATE + "/nm.json"))
    for b in meta["bridges"]:
        sh(["nmcli", "connection", "delete", b])
    for pr in meta["profiles"]:
        shutil.copy2(os.path.join(STATE, "backup/nm", os.path.basename(pr["file"])), pr["file"])
        os.chmod(pr["file"], 0o600)
    sh(["nmcli", "connection", "reload"])
    for pr in meta["profiles"]:
        sh(["nmcli", "connection", "up", pr["name"]])
    return True


# ------------------------------------------------------------------ commands
def load_plan():
    return json.load(open(STATE + "/plan.json"))


def cmd_write(a):
    p = plan(a.ip, a.bridge, json.loads(a.extra or "{}"))
    if p["status"] != "needed":
        print(json.dumps(p))
        return 0 if p["status"] == "present" else 1
    os.makedirs(STATE, exist_ok=True)
    (netplan_write if p["backend"] == "netplan" else nm_write)(p)
    json.dump(p, open(STATE + "/plan.json", "w"))
    log(f"prepared: {p['ip']}/{p['prefix']} moves from {p['dev']} to {p['bridge']} ({p['backend']}); extra bridges: {p['extra'] or 'none'}")
    return 0


def rollback(p):
    log("ROLLBACK: restoring the previous network configuration")
    if p["backend"] == "netplan":
        if netplan_restore():
            sh(["netplan", "apply"])
    else:
        nm_restore()
    log("ROLLBACK done")


def cmd_apply(a):
    p = load_plan()
    if os.path.exists(OKFILE):
        os.remove(OKFILE)
    log(f"applying ({p['backend']}); waiting up to {a.timeout}s for the master to confirm")
    try:
        if p["backend"] == "netplan":
            sh(["netplan", "apply"], check=True)
        else:
            nm_apply()
    except SystemExit as e:
        log(f"apply failed: {e}")
        rollback(p)
        return 1
    end = time.time() + a.timeout
    while time.time() < end:
        if os.path.exists(OKFILE):
            log("confirmed by the master - the bridge configuration stays")
            return 0
        time.sleep(0.2 if a.timeout < 10 else 1)
    log("no confirmation - the host was not reachable through the bridge")
    rollback(p)
    return 1


def cmd_confirm(a):
    p = plan(a.ip, a.bridge, {})
    if p.get("dev") != a.bridge:
        print(json.dumps({"ok": False, "detail": f"{a.ip} is on {p.get('dev')}, not on {a.bridge} (rolled back?)"}))
        return 1
    missing = [b for b in json.loads(a.extra or "{}") if sh(["ip", "link", "show", b]).returncode != 0]
    if missing:
        print(json.dumps({"ok": False, "detail": f"bridges missing: {', '.join(missing)}"}))
        return 1
    os.makedirs(os.path.dirname(OKFILE), exist_ok=True)
    open(OKFILE, "w").write(str(time.time()))
    print(json.dumps({"ok": True, "bridge": a.bridge, "ip": a.ip}))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "write", "apply", "confirm", "rollback"])
    ap.add_argument("--ip")
    ap.add_argument("--bridge", default="cloudbr0")
    ap.add_argument("--extra", default="{}", help='JSON {"cloudbr1": "eno2"}: bridges without an IP on other NICs')
    ap.add_argument("--timeout", type=int, default=120)
    a = ap.parse_args()
    if a.cmd == "plan":
        print(json.dumps(plan(a.ip, a.bridge, json.loads(a.extra or "{}"))))
        return 0
    if a.cmd == "write":
        return cmd_write(a)
    if a.cmd == "apply":
        return cmd_apply(a)
    if a.cmd == "confirm":
        return cmd_confirm(a)
    rollback(load_plan())
    return 0


if __name__ == "__main__":
    sys.exit(main())
