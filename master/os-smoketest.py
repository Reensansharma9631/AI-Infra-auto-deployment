#!/usr/bin/env python3
"""OpenStack smoke test, run on the AutoInfra master after a Kolla deployment (idempotent, offline):

  1. Keystone answers; every nova-compute service is up; every network agent is alive
  2. a guest image uploaded to Glance from the master's 'templates' repository (no internet)
  3. flavor + tenant (Geneve) network + VM -> ACTIVE with an IP
  4. the VM is reachable: SSH banner read from the OVN metadata namespace on the compute node hosting it

usage: sudo /opt/autoinfra/venv/bin/python /opt/autoinfra/master/os-smoketest.py [--cleanup]
Exit 0 = PASS. Uses /opt/airgap/venv-kolla/bin/openstack and the admin-openrc.sh written by post-deploy."""
import argparse, json, os, re, sqlite3, subprocess, sys, time

NAME = "autoinfra-smoke"
VENV = "/opt/airgap/venv-kolla/bin"
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' - ' + str(detail)) if detail else ''}", flush=True)
    return ok


def creds(data):
    env = dict(os.environ)
    for rc in (os.path.join(data, "openstack/etc/admin-openrc.sh"), "/etc/kolla/admin-openrc.sh"):
        if os.path.exists(rc):
            out = subprocess.run(["bash", "-c", f"set -a; . {rc}; env"], capture_output=True, text=True).stdout
            env.update(dict(l.split("=", 1) for l in out.splitlines() if l.startswith("OS_") and "=" in l))
            return env, rc
    sys.exit("admin-openrc.sh not found - did post-deploy run?")


def os_(env, *args, fmt=True):
    cmd = [f"{VENV}/openstack", *args] + (["-f", "json"] if fmt else [])
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"openstack {' '.join(args)}: {(r.stderr or r.stdout).strip()[-400:]}")
    return json.loads(r.stdout) if fmt and r.stdout.strip() else r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cleanup", action="store_true")
    a = ap.parse_args()
    envf = dict(l.strip().split("=", 1) for l in open("/etc/autoinfra/autoinfra.env") if "=" in l and not l.startswith("#"))
    data = envf.get("AUTOINFRA_DATA", "/var/lib/autoinfra")
    env, rc = creds(data)

    # 1. control plane
    try:
        tok = os_(env, "token", "issue")
        check("Keystone issues a token", tok.get("id"), env.get("OS_AUTH_URL"))
    except RuntimeError as e:
        check("Keystone issues a token", False, e)
        return 1
    comp = [c for c in os_(env, "compute", "service", "list") if c["Binary"] == "nova-compute"]
    check("every nova-compute service up", comp and all(c["State"] == "up" for c in comp), {c["Host"]: c["State"] for c in comp})
    agents = os_(env, "network", "agent", "list")
    check("every network agent alive", agents and all(x["Alive"] in (True, ":-)") for x in agents),
          sorted({(x["Agent Type"], x["Host"]) for x in agents if x["Alive"] not in (True, ":-)")}) or f"{len(agents)} agents")

    # 2. image from the master's repository
    img = next((i for i in os_(env, "image", "list") if i["Name"] == NAME), None)
    if not img:
        idx = os.path.join(data, "repo", "templates", "files")
        files = sorted(f for f in os.listdir(idx) if f.endswith((".qcow2", ".img"))) if os.path.isdir(idx) else []
        if not check("guest image in the 'templates' repository", files, "upload a qcow2 first"):
            return 1
        os_(env, "image", "create", "--disk-format", "qcow2", "--container-format", "bare", "--public", "--file",
            os.path.join(idx, files[0]), NAME)
    img = wait(lambda: next((i for i in os_(env, "image", "list") if i["Name"] == NAME and i["Status"] == "active"), None), 600)
    check("guest image active in Glance (uploaded from the master)", img, (img or {}).get("ID"))

    # 3. flavor, network, VM
    if not any(f["Name"] == NAME for f in os_(env, "flavor", "list")):
        os_(env, "flavor", "create", "--vcpus", "1", "--ram", "1024", "--disk", "5", NAME)
    if not any(n["Name"] == f"{NAME}-net" for n in os_(env, "network", "list")):
        os_(env, "network", "create", f"{NAME}-net")
        os_(env, "subnet", "create", "--network", f"{NAME}-net", "--subnet-range", "192.168.77.0/24", "--dns-nameserver",
            envf.get("AUTOINFRA_MASTER_IP", "172.27.15.238"), f"{NAME}-subnet")
    net = os_(env, "network", "show", f"{NAME}-net")
    old = next((x for x in os_(env, "server", "list") if x["Name"] == NAME), None)
    if old and old["Status"] == "ERROR":           # a failed earlier attempt: start over, do not report its old fault
        os_(env, "server", "delete", "--wait", old["ID"], fmt=False)
        old = None
    if not old:
        os_(env, "server", "create", "--image", NAME, "--flavor", NAME, "--network", f"{NAME}-net", NAME)
    srv = wait(lambda: (lambda s: s if s["status"] in ("ACTIVE", "ERROR") else None)(os_(env, "server", "show", NAME)), 900, 10)
    ip = None
    if srv:
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", json.dumps(srv.get("addresses")))
        ip = m.group(1) if m else None
    host = (srv or {}).get("OS-EXT-SRV-ATTR:host")
    check("VM ACTIVE with an IP", srv and srv["status"] == "ACTIVE" and ip, f"{(srv or {}).get('status')} {ip} on {host} "
          f"{(srv or {}).get('fault', {}).get('message', '') if srv and srv.get('status') == 'ERROR' else ''}")
    if not (srv and srv["status"] == "ACTIVE" and ip):
        return 1

    # 4. reachability from the OVN metadata namespace on the compute node
    db = sqlite3.connect(os.path.join(data, "autoinfra.db"))
    row = db.execute("SELECT ip, ssh_user, ssh_port FROM hosts WHERE hostname=? OR ip=?", (host, host)).fetchone()
    ns = f"ovnmeta-{net['id']}"
    probe = (f"sudo ip netns exec {ns} bash -c 'for i in $(seq 40); do b=$( (exec 3<>/dev/tcp/{ip}/22; IFS= read -r -t 10 l <&3; "
             f"echo \"$l\") 2>/dev/null ); case \"$b\" in SSH-*) echo \"$b\"; exit 0;; esac; sleep 6; done; echo ERR'")
    out = ""
    if row:
        out = subprocess.run(["ssh", "-i", os.path.join(data, "ssh", "id_ed25519"), "-p", str(row[2] or 22), "-o", "BatchMode=yes",
                              "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                              f"{row[1] or 'root'}@{row[0]}", probe], capture_output=True, text=True, timeout=420).stdout.strip()
    check("VM reachable: SSH banner from the OVN metadata namespace", out.startswith("SSH-"), f"{ip}:22 via {ns} on {host}: {out[:60]}")

    if a.cleanup:
        for args in (("server", "delete", "--wait", NAME), ("subnet", "delete", f"{NAME}-subnet"), ("network", "delete", f"{NAME}-net")):
            try:
                os_(env, *args, fmt=False)
            except RuntimeError:
                pass
        print("      cleaned up")
    ok = all(results)
    print(f"\nOPENSTACK SMOKE TEST: {'PASS' if ok else 'FAIL'} ({sum(results)}/{len(results)} checks)")
    return 0 if ok else 1


def wait(fn, timeout, every=5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


if __name__ == "__main__":
    sys.exit(main())
