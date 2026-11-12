"""SSH operations: key bootstrap (the password is used once, never stored), fact collection, readiness checks, metrics."""
import io, os, shlex, socket, time

from . import drivers

try:
    import paramiko
except ImportError:          # tests that never touch ssh still import this module
    paramiko = None

# One POSIX-sh script that works on Ubuntu and RHEL families. Prints KEY=VALUE lines.
COLLECT = r"""
export LC_ALL=C
echo "UID=$(id -u)"
if [ "$(id -u)" = 0 ]; then echo SUDO=root; elif sudo -n true 2>/dev/null; then echo SUDO=nopasswd; else echo SUDO=no; fi
S=""; [ "$(id -u)" = 0 ] || S="sudo -n"
if [ -r /etc/os-release ]; then sed -n 's/^\(ID\|VERSION_ID\|PRETTY_NAME\|ID_LIKE\)=/OSR_\1=/p' /etc/os-release; fi
echo "KERNEL=$(uname -r)"
echo "HOSTNAME=$(hostname 2>/dev/null || cat /etc/hostname)"
echo "CPUS=$(grep -c ^processor /proc/cpuinfo)"
echo "MEM_MB=$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo)"
echo "DISK_FREE_GB=$(df -Pk / | awk 'NR==2{printf "%d", $4/1048576}')"
echo "DISK_TOTAL_GB=$(df -Pk / | awk 'NR==2{printf "%d", $2/1048576}')"
[ -e /dev/kvm ] && echo KVM_DEV=1 || echo KVM_DEV=0
echo "VIRT_FLAGS=$(grep -Eoc '(vmx|svm)' /proc/cpuinfo)"
echo "EPOCH=$(date +%s)"
if command -v timedatectl >/dev/null 2>&1; then echo "NTP_SYNC=$(timedatectl show -p NTPSynchronized --value 2>/dev/null)"; fi
if command -v apt-get >/dev/null 2>&1; then echo PKG=apt; echo "UPDATES=$(apt-get -s upgrade 2>/dev/null | grep -c '^Inst ' )";
elif command -v dnf >/dev/null 2>&1; then echo PKG=dnf; echo "UPDATES=$(dnf -q -C check-update 2>/dev/null | grep -cE '\.(x86_64|noarch|aarch64)')";
elif command -v yum >/dev/null 2>&1; then echo PKG=yum; else echo PKG=none; fi
for p in python3 chrony curl rsync openssh-server qemu-kvm libvirt-daemon-system nfs-common mysql-server; do
  if command -v dpkg-query >/dev/null 2>&1; then dpkg-query -W -f='${Status}' $p 2>/dev/null | grep -q 'install ok installed' && echo "HAS_$p=1" || echo "HAS_$p=0";
  elif command -v rpm >/dev/null 2>&1; then rpm -q $p >/dev/null 2>&1 && echo "HAS_$p=1" || echo "HAS_$p=0"; fi
done
command -v python3 >/dev/null 2>&1 && echo "PYTHON=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')" || echo PYTHON=none
if [ -n "$REPO_URL" ]; then
  if command -v curl >/dev/null 2>&1; then curl -s -o /dev/null -m 5 -w 'REPO_HTTP=%{http_code}\n' "$REPO_URL" || echo REPO_HTTP=000;
  elif command -v python3 >/dev/null 2>&1; then python3 -c "import urllib.request,sys
try: print('REPO_HTTP=%d'%urllib.request.urlopen(sys.argv[1],timeout=5).status)
except Exception as e: print('REPO_HTTP=000')" "$REPO_URL"; else echo REPO_HTTP=nocurl; fi
fi
if command -v sshd >/dev/null 2>&1; then
  echo "SSHD_ROOT=$($S sshd -T 2>/dev/null | awk '/^permitrootlogin/{print $2}')"
  echo "SSHD_PWAUTH=$($S sshd -T 2>/dev/null | awk '/^passwordauthentication/{print $2}')"
fi
if command -v ufw >/dev/null 2>&1; then echo "FW=ufw:$($S ufw status 2>/dev/null | head -1 | awk '{print $2}')";
elif command -v firewall-cmd >/dev/null 2>&1; then echo "FW=firewalld:$($S firewall-cmd --state 2>/dev/null)"; else echo FW=none; fi
command -v getenforce >/dev/null 2>&1 && echo "SELINUX=$(getenforce)"
echo "MACHINE_ID=$(cat /etc/machine-id 2>/dev/null)"
[ -d /sys/kernel/security/apparmor ] && echo APPARMOR=1
for s in chronyd chrony sshd ssh libvirtd mysql mysqld cloudstack-management cloudstack-agent nginx haproxy rabbitmq-server zabbix-agent2 ollama; do
  if command -v systemctl >/dev/null 2>&1; then echo "SVC_$s=$(systemctl is-active $s 2>/dev/null | head -1)"; fi
done
ip -o -4 addr show 2>/dev/null | awk '{print "IFACE=" $2 " " $4}'
"""

METRICS = r"""
export LC_ALL=C
read -r _ a b c d e f g h _ < /proc/stat; t1=$((a+b+c+d+e+f+g+h)); i1=$((d+e))
cat /proc/net/dev | awk 'NR>2 && $1 !~ /lo:/ {gsub(":"," ");rx+=$2;tx+=$10} END{print "NET1=" rx " " tx}'
sleep 1
read -r _ a b c d e f g h _ < /proc/stat; t2=$((a+b+c+d+e+f+g+h)); i2=$((d+e))
cat /proc/net/dev | awk 'NR>2 && $1 !~ /lo:/ {gsub(":"," ");rx+=$2;tx+=$10} END{print "NET2=" rx " " tx}'
echo "CPU=$(( (100*((t2-t1)-(i2-i1))) / ((t2-t1)>0 ? (t2-t1) : 1) ))"
awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{printf "MEM=%.1f\n", (t-a)*100/t}' /proc/meminfo
df -Pk / | awk 'NR==2{gsub("%","",$5); print "DISK=" $5}'
echo "LOAD=$(cut -d' ' -f1 /proc/loadavg)"
"""


def parse_kv(text):
    facts, ifaces = {}, []
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k == "IFACE":
            ifaces.append(v.strip())
        elif k and k.replace("_", "").replace("-", "").isalnum():
            facts[k] = v.strip()
    if ifaces:
        facts["IFACES"] = ifaces
    return facts


def parse_facts(text):
    f = parse_kv(text)
    osr = {k[4:]: v.strip('"') for k, v in f.items() if k.startswith("OSR_")}
    f["os"] = drivers.classify_os(osr)
    return f


def parse_metrics(text):
    f = parse_kv(text)
    out = {}
    for k in ("CPU", "MEM", "DISK", "LOAD"):
        try:
            out[k.lower()] = float(f.get(k, "nan"))
        except ValueError:
            out[k.lower()] = None
    try:
        r1, t1 = map(float, f["NET1"].split())
        r2, t2 = map(float, f["NET2"].split())
        out["net_rx"], out["net_tx"] = max(0.0, r2 - r1), max(0.0, t2 - t1)
    except (KeyError, ValueError):
        out["net_rx"] = out["net_tx"] = None
    return out


def tcp_ping(ip, port=22, timeout=3):
    t = time.time()
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True, int((time.time() - t) * 1000)
    except OSError as e:
        return False, str(e)


class AuthFailed(Exception):
    pass


def connect(host, key_path=None, password=None, timeout=10):
    if paramiko is None:
        raise RuntimeError("paramiko is not installed")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = dict(hostname=host["ip"], port=int(host.get("ssh_port") or 22), username=host.get("ssh_user") or "root",
              timeout=timeout, banner_timeout=timeout, auth_timeout=timeout, allow_agent=False, look_for_keys=False)
    if password:
        kw["password"] = password
    else:
        kw["pkey"] = paramiko.Ed25519Key.from_private_key_file(key_path)
    try:
        c.connect(**kw)
    except paramiko.AuthenticationException as e:
        c.close()
        raise AuthFailed(str(e) or "authentication failed")
    return c


def run(client, cmd, stdin_data=None, timeout=120, env_prefix=""):
    stdin, stdout, stderr = client.exec_command(env_prefix + cmd, timeout=timeout)
    if stdin_data is not None:
        stdin.write(stdin_data)
        stdin.channel.shutdown_write()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    return stdout.channel.recv_exit_status(), out, err


def run_script(client, script, env=None, timeout=120):
    pre = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in (env or {}).items())
    return run(client, (pre + " " if pre else "") + "sh -s", stdin_data=script, timeout=timeout)


def with_retries(fn, attempts=3, delay=2, auth_attempts=2, log=None):
    """Retry network errors `attempts` times; stop after `auth_attempts` authentication failures."""
    last, auth = None, 0
    for i in range(1, max(1, int(attempts)) + 1):
        try:
            return fn()
        except AuthFailed as e:
            auth += 1
            last = e
            if log:
                log(f"   attempt {i}: authentication failed")
            if auth >= auth_attempts:
                raise
        except Exception as e:  # noqa: BLE001 - every network error is retried
            last = e
            if log:
                log(f"   attempt {i}: {type(e).__name__}: {e}")
        if i < attempts:
            time.sleep(delay)
    raise last


def bootstrap_key(host, password, pub_key, timeout=10):
    """Log in once with the password and install our public key (and passwordless sudo check)."""
    c = connect(host, password=password, timeout=timeout)
    try:
        script = ("umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; "
                  f"grep -qxF {shlex.quote(pub_key.strip())} ~/.ssh/authorized_keys || echo {shlex.quote(pub_key.strip())} >> ~/.ssh/authorized_keys; "
                  "chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys; "
                  "command -v restorecon >/dev/null 2>&1 && restorecon -R ~/.ssh >/dev/null 2>&1; echo KEY_OK")
        rc, out, err = run(c, script, timeout=timeout * 3)
        if "KEY_OK" not in out:
            raise RuntimeError(f"could not install key: {err.strip()[:200]}")
        return True
    finally:
        c.close()


def summarize(facts):
    os_ = facts.get("os", {})
    return {"os_id": os_.get("id", ""), "os_version": os_.get("version", ""), "os_family": os_.get("family", ""),
            "os_pretty": os_.get("pretty", ""), "os_support": os_.get("support", ""), "kernel": facts.get("KERNEL", ""),
            "updates": int(facts["UPDATES"]) if str(facts.get("UPDATES", "")).isdigit() else -1}


def evaluate(facts, th, master_epoch=None, roles=()):
    """Turn facts into a list of {name, status(pass|warn|fail), detail}."""
    r = []
    add = lambda n, s, d: r.append({"name": n, "status": s, "detail": d})
    sudo = facts.get("SUDO", "no")
    add("Root / sudo access", "pass" if sudo in ("root", "nopasswd") else "fail",
        {"root": "logged in as root", "nopasswd": "passwordless sudo"}.get(sudo, "user has no passwordless sudo"))
    o = facts.get("os", {})
    st = {"supported": "pass", "compat": "warn"}.get(o.get("support"), "fail")
    add("OS compatibility", st, f"{o.get('pretty', 'unknown')} ({o.get('support', 'unsupported')})")
    cpus = int(facts.get("CPUS", 0) or 0)
    add("CPU cores", "pass" if cpus >= th["min_cpu"] else "fail", f"{cpus} cores (min {th['min_cpu']})")
    mem = int(facts.get("MEM_MB", 0) or 0)
    add("Memory", "pass" if mem >= th["min_mem_mb"] else "fail", f"{mem} MB (min {th['min_mem_mb']})")
    disk = int(facts.get("DISK_FREE_GB", 0) or 0)
    add("Disk free on /", "pass" if disk >= th["min_disk_gb"] else "fail", f"{disk} GB free (min {th['min_disk_gb']})")
    kvm_needed = bool(set(roles) & {"compute", "openstack-compute", "kvm"})
    if facts.get("KVM_DEV") == "1":
        add("CPU virtualization (KVM)", "pass", "/dev/kvm present")
    elif int(facts.get("VIRT_FLAGS", 0) or 0) > 0:
        add("CPU virtualization (KVM)", "warn", "vmx/svm flags present but /dev/kvm missing (kvm module not loaded)")
    else:
        add("CPU virtualization (KVM)", "fail" if kvm_needed else "warn", "no vmx/svm - hypervisor role impossible")
    if master_epoch and str(facts.get("EPOCH", "")).isdigit():
        skew = abs(int(facts["EPOCH"]) - int(master_epoch))
        add("Clock skew vs master", "pass" if skew <= 5 else "warn" if skew <= 60 else "fail", f"{skew} s")
    pkg = facts.get("PKG", "none")
    fam = o.get("family")
    ok = (fam == "debian" and pkg == "apt") or (fam == "rhel" and pkg == "dnf")
    add("Package manager", "pass" if ok else "fail", pkg)
    if "REPO_HTTP" in facts:
        code = facts["REPO_HTTP"]
        add("Master repository reachable", "pass" if code.startswith(("2", "3")) else "fail", f"HTTP {code}")
    up = facts.get("UPDATES", "")
    if str(up).isdigit():
        add("Updates available", "pass" if int(up) == 0 else "warn", f"{up} package(s) can be upgraded")
    missing = [k[4:] for k, v in facts.items() if k.startswith("HAS_") and v == "0" and k[4:] in ("python3", "curl")]
    add("Required packages", "pass" if not missing else "warn",
        "python3, curl present" if not missing else "missing: " + ", ".join(missing) + " (installed from the master repo)")
    sec = []
    if facts.get("SSHD_PWAUTH") == "yes":
        sec.append("ssh password login enabled")
    if facts.get("SELINUX") == "Enforcing":
        sec.append("SELinux enforcing (supported, policies applied by roles)")
    fw = facts.get("FW", "none")
    sec.append(f"firewall {fw}")
    add("Security checks", "warn" if facts.get("SSHD_PWAUTH") == "yes" else "pass", "; ".join(sec))
    py = facts.get("PYTHON", "none")
    add("Python for Ansible", "pass" if py not in ("none", "") else "fail", py)
    if facts.get("MACHINE_ID"):     # compared across hosts by the check job (cloned VMs share it)
        add("Machine ID", "pass", facts["MACHINE_ID"])
    return r


def overall(results):
    s = {c["status"] for c in results}
    return "not-ready" if "fail" in s else "ready-with-warnings" if "warn" in s else "ready"
