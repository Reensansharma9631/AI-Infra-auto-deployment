#!/usr/bin/env python3
"""CloudStack smoke test, run on the AutoInfra master after a deployment (idempotent; uses AutoInfra's SAVED API keys, so
it never registers new keys). Proves the cloud really works:

  1. zone Enabled, every KVM host Up, every primary pool Up, SSVM + CPVM Running with their agents Up
  2. a guest template registered from the master's own repository and downloaded by the SSVM (offline path)
  3. an isolated guest network (virtual router, source NAT on a public IP) and a VM that gets an IP
  4. the VM is reachable: SSH banner through a port forward on the network's public IP

usage: sudo /opt/autoinfra/venv/bin/python /opt/autoinfra/master/cs-smoketest.py [--template-file NAME] [--from HOST] [--cleanup]
  --template-file  file in the 'templates' repository (default: the first *.qcow2 there)
  --from           host that can reach the public network (default: the internal-network gateway = first KVM host);
                   the check runs there over SSH with AutoInfra's key. 'local' = run it on the master.
  --cleanup        delete the smoke-test VM, network and port forward afterwards
Exit code 0 = PASS. Every check is printed as PASS/FAIL.
"""
import argparse, json, os, shlex, sqlite3, subprocess, sys, time

sys.path.insert(0, "/opt/autoinfra/ui")
from autoinfra import csapi  # noqa: E402

NAME = "autoinfra-smoke"
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' - ' + str(detail)) if detail else ''}", flush=True)
    return ok


def env():
    e = {}
    for l in open("/etc/autoinfra/autoinfra.env"):
        if "=" in l and not l.startswith("#"):
            k, v = l.strip().split("=", 1)
            e[k] = v
    return e


def settings(data):
    c = sqlite3.connect(os.path.join(data, "autoinfra.db"))
    get = lambda k: (lambda r: json.loads(r[0]) if r else None)(c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone())
    return get


def wait(what, fn, timeout, every=10):
    end = time.time() + timeout
    while True:
        v = fn()
        if v:
            return v
        if time.time() > end:
            return None
        time.sleep(every)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template-file")
    ap.add_argument("--from", dest="src")
    ap.add_argument("--cleanup", action="store_true")
    a = ap.parse_args()
    e = env()
    data = e.get("AUTOINFRA_DATA", "/var/lib/autoinfra")
    get = settings(data)
    sec = json.load(open(os.path.join(data, "secrets.json"))).get("cs_secret_key")
    if not (get("cs_api_url") and get("cs_api_key") and sec):
        sys.exit("CloudStack API is not configured in AutoInfra (deploy with the wizard first)")
    cs = csapi.CS(get("cs_api_url"), get("cs_api_key"), sec, poll_max=900)
    repo_base = get("repo_base_url") or f"http://{e.get('AUTOINFRA_MASTER_IP', '127.0.0.1')}:8081/repo"

    # 1. infrastructure
    zones = cs.items("listZones", "zone")
    check("zone exists and is Enabled", zones and all(z.get("allocationstate") == "Enabled" for z in zones), [z["name"] for z in zones])
    if not zones:
        return 1
    zid = zones[0]["id"]
    hosts = cs.items("listHosts", "host", zoneid=zid, type="Routing")
    check("every KVM host Up", hosts and all(h["state"] == "Up" for h in hosts), {h["name"]: h["state"] for h in hosts})
    pools = cs.items("listStoragePools", "storagepool", zoneid=zid)
    check("every primary storage pool Up", pools and all(p["state"] == "Up" for p in pools), {p["name"]: p["state"] for p in pools})
    stores = cs.items("listImageStores", "imagestore", zoneid=zid)
    check("secondary storage present", stores, [s["name"] for s in stores])
    svms = cs.items("listSystemVms", "systemvm", zoneid=zid)
    want_cp = int(((get("cs_wizard") or {}).get("systemvm") or {}).get("console_proxies") or 1)
    cps = [v for v in svms if v.get("systemvmtype") == "consoleproxy"]
    if want_cp > 1:     # extra proxies are started by CloudStack's capacity scan: give it time
        wait("cpvms", lambda: len([v for v in cs.items("listSystemVms", "systemvm", zoneid=zid, systemvmtype="consoleproxy")
                                   if v["state"] == "Running" and v.get("agentstate") == "Up"]) >= want_cp, 900, 15)
        svms = cs.items("listSystemVms", "systemvm", zoneid=zid)
        cps = [v for v in svms if v.get("systemvmtype") == "consoleproxy"]
    check(f"console proxies Running/Up >= {want_cp} (wizard setting)",
          len([v for v in cps if v["state"] == "Running" and v.get("agentstate") == "Up"]) >= want_cp, [v["name"] for v in cps])
    # CloudStack scales SSVMs by load and leaves a surplus one Stopped (e.g. after its host was lent to OpenStack): required are
    # >=1 SSVM and every RUNNING system VM with its agent Up
    run = [v for v in svms if v["state"] == "Running"]
    check("SSVM and CPVM Running, agents Up",
          any(v.get("systemvmtype") == "secondarystoragevm" for v in run) and any(v.get("systemvmtype") == "consoleproxy" for v in run)
          and all(v.get("agentstate") == "Up" for v in run),
          {v["name"]: f"{v['state']}/{v.get('agentstate')}" for v in svms})

    # 2. guest template from the master's repository
    tfile = a.template_file
    if not tfile:
        idx = os.path.join(data, "repo", "templates", "files")
        cands = sorted(f for f in os.listdir(idx) if f.endswith((".qcow2", ".qcow2.bz2", ".img"))) if os.path.isdir(idx) else []
        if not cands:
            check("guest template file in the 'templates' repository", False, "upload a qcow2 (Package Repository > templates)")
            return 1
        tfile = cands[0]
    turl = f"{repo_base}/templates/files/{tfile}"
    tname = f"{NAME}-{os.path.splitext(tfile)[0]}"[:60]
    t = cs.find("listTemplates", "template", templatefilter="all", name=tname)
    if not t:
        ost = next((o for o in cs.items("listOsTypes", "ostype", keyword="Ubuntu") if "24.04" in o["description"]), None) \
            or cs.items("listOsTypes", "ostype", keyword="Other Linux")[0]
        t = cs.run("registerTemplate", name=tname, displaytext=f"AutoInfra smoke test ({tfile})", url=turl, zoneid=zid, format="QCOW2",
                   hypervisor="KVM", ostypeid=ost["id"], ispublic="true", isextractable="false", passwordenabled="false")["template"][0]
        print(f"      registered template {tname} from {turl}")
    ready = wait("template", lambda: (cs.find("listTemplates", "template", templatefilter="all", id=t["id"]) or {}).get("isready"), 1800, 15)
    tt = cs.find("listTemplates", "template", templatefilter="all", id=t["id"]) or {}
    if not check("guest template downloaded by the SSVM from the master", ready, tt.get("status")):
        return 1

    # 3. network + VM
    off = cs.find("listServiceOfferings", "serviceoffering", name="Medium Instance") or cs.items("listServiceOfferings", "serviceoffering")[0]
    nof = cs.find("listNetworkOfferings", "networkoffering", name="DefaultIsolatedNetworkOfferingWithSourceNatService", state="Enabled")
    net = cs.find("listNetworks", "network", zoneid=zid, name=f"{NAME}-net", listall="true")
    if not net:
        net = cs.run("createNetwork", zoneid=zid, name=f"{NAME}-net", displaytext="AutoInfra smoke test", networkofferingid=nof["id"])["network"]
    vm = cs.find("listVirtualMachines", "virtualmachine", name=NAME, listall="true")
    if not vm:
        print("      deploying the test VM (the virtual router starts first - a few minutes)")
        vm = cs.run("deployVirtualMachine", zoneid=zid, templateid=t["id"], serviceofferingid=off["id"], networkids=net["id"], name=NAME,
                    displayname=NAME)["virtualmachine"]
    if vm.get("state") == "Stopped":     # e.g. after the hosts were lent to OpenStack - start it like an operator would
        print("      test VM was Stopped - starting it")
        cs.run("startVirtualMachine", id=vm["id"])
    vm = wait("vm", lambda: (lambda v: v if v and v.get("state") == "Running" else None)(
        cs.find("listVirtualMachines", "virtualmachine", id=vm["id"], listall="true")), 900, 10)
    ip = (vm or {}).get("nic", [{}])[0].get("ipaddress")
    check("test VM Running with an IP", vm and ip, f"{(vm or {}).get('hostname')} {ip}")
    routers = cs.items("listRouters", "router", networkid=net["id"], listall="true")
    check("virtual router Running", routers and all(r["state"] == "Running" for r in routers), [(r["name"], r["state"], r.get("publicip")) for r in routers])
    if not (vm and ip):
        return 1

    # 4. reachability: SSH banner through a port forward on the source-NAT public IP
    pub = cs.find("listPublicIpAddresses", "publicipaddress", associatednetworkid=net["id"], issourcenat="true", listall="true")
    if not cs.items("listPortForwardingRules", "portforwardingrule", ipaddressid=pub["id"], listall="true"):
        cs.run("createPortForwardingRule", ipaddressid=pub["id"], privateport=22, publicport=2222, protocol="TCP", virtualmachineid=vm["id"],
               openfirewall="false")
    if not cs.items("listFirewallRules", "firewallrule", ipaddressid=pub["id"], listall="true"):
        cs.run("createFirewallRule", ipaddressid=pub["id"], protocol="TCP", startport=2222, endport=2222, cidrlist="0.0.0.0/0")
    # each attempt in a subshell (a failed 'exec 3<>/dev/tcp/..' ends a non-interactive bash); read ONE line - the SSH server
    # sends ~40 bytes and then waits, so 'head -c 64' never returned
    probe = ("bash -c 'for i in $(seq 24); do b=$( (exec 3<>/dev/tcp/" + pub["ipaddress"] + "/2222; IFS= read -r -t 10 l <&3; echo \"$l\") 2>/dev/null ); "
             "case \"$b\" in SSH-*) echo \"$b\"; exit 0;; esac; sleep 5; done; echo ERR'")
    db = sqlite3.connect(os.path.join(data, "autoinfra.db"))
    if a.src == "local":
        cands = ["local"]
    elif a.src:
        cands = [db.execute("SELECT ip, ssh_user, ssh_port FROM hosts WHERE hostname=? OR ip=?", (a.src, a.src)).fetchone() or (a.src, "root", 22)]
    else:   # every KVM host in turn (the internal-network gateway answers first)
        cands = [r for h in hosts for r in [db.execute("SELECT ip, ssh_user, ssh_port FROM hosts WHERE hostname=?", (h["name"],)).fetchone()] if r]
    out, used = "", None
    for c in cands:
        if c == "local":
            out = subprocess.run(["sh", "-c", probe], capture_output=True, text=True, timeout=330).stdout.strip()
        else:
            ip_, user, port = c
            out = subprocess.run(["ssh", "-i", os.path.join(data, "ssh", "id_ed25519"), "-p", str(port or 22), "-o", "BatchMode=yes",
                                  "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                                  f"{user or 'root'}@{ip_}", probe], capture_output=True, text=True, timeout=330).stdout.strip()
        used = c if c == "local" else c[0]
        if out.startswith("SSH-"):
            break
    check("test VM reachable: SSH banner via public IP port forward", out.startswith("SSH-"), f"{pub['ipaddress']}:2222 -> {ip}:22 from {used}: {out[:60]}")

    if a.cleanup:
        cs.run("destroyVirtualMachine", id=vm["id"], expunge="true")
        cs.run("deleteNetwork", id=net["id"])
        print("      cleaned up")
    ok = all(r for _, r in results)
    print(f"\nSMOKE TEST: {'PASS' if ok else 'FAIL'} ({sum(r for _, r in results)}/{len(results)} checks)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
