#!/usr/bin/env python3
"""Minimal systemctl for TEST chroots (no systemd there). Really starts mysql (supervised like systemd's
Restart=on-failure, MYSQLD_PARENT_PID set) and any unit with an ExecStart (e.g. dbha-agent); other units only keep state."""
import os, shlex, signal, subprocess, sys, time

D = "/var/lib/fakesd"; os.makedirs(D, exist_ok=True)
UDIRS = ["/etc/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system"]


def f(u, k): return f"{D}/{u}.{k}"
def get(u, k, d=""):
    try: return open(f(u, k)).read().strip()
    except OSError: return d
def put(u, k, v): open(f(u, k), "w").write(str(v))
def alive(pid):
    try:
        os.kill(int(pid), 0)
        return open(f"/proc/{int(pid)}/stat").read().split(")")[-1].split()[0] != "Z"
    except Exception: return False
def active(u):
    pid = get(u, "pid")
    if pid: return alive(pid)
    return get(u, "flag") == "1"


def unit_lines(u):
    for d in UDIRS:
        p = f"{d}/{u}"
        if os.path.exists(p):
            return [l.strip() for l in open(p)]
    return []


def start_mysql(u):
    subprocess.run("mkdir -p /var/run/mysqld && chown mysql:mysql /var/run/mysqld", shell=True)
    if not os.path.isdir("/var/lib/mysql/mysql"):
        subprocess.run("/usr/sbin/mysqld --initialize-insecure --user=mysql >/var/log/mysql-init.log 2>&1", shell=True, check=True)
    sup = f"{D}/mysql-supervise.sh"
    open(sup, "w").write("""#!/bin/bash
export MYSQLD_PARENT_PID=$$
echo $$ > /var/lib/fakesd/mysql.sup
while true; do
  /usr/sbin/mysqld --user=mysql >> /var/log/mysqld-stdout.log 2>&1
  rc=$?
  [ $rc -eq 0 ] && exit 0
  sleep 1
done
""")
    os.chmod(sup, 0o755)
    p = subprocess.Popen([sup], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    put(u, "pid", p.pid)
    for _ in range(120):   # like Type=notify: wait for the socket
        if os.path.exists("/var/run/mysqld/mysqld.sock"):
            try:
                if subprocess.run(["mysqladmin", "-uroot", "ping"], capture_output=True).returncode == 0: return
            except Exception: pass
        time.sleep(0.5)
    sys.exit("mysql did not come up")


def start(u):
    if active(u): return
    name = u[:-8]
    if name == "mysql": return start_mysql(u)
    ex = [l.split("=", 1)[1] for l in unit_lines(u) if l.startswith("ExecStart=")]
    if name == "keepalived":   # run it for real (VRRP) - needs its own netns
        p = subprocess.Popen(["/usr/sbin/keepalived", "--dont-fork", "--log-console", "--log-detail"], start_new_session=True,
                             stdout=open("/var/log/keepalived.log", "ab"), stderr=subprocess.STDOUT)
        put(u, "pid", p.pid); return
    if name == "haproxy":
        subprocess.run("mkdir -p /run/haproxy && chown haproxy:haproxy /run/haproxy", shell=True)
        p = subprocess.Popen(["/usr/sbin/haproxy", "-f", "/etc/haproxy/haproxy.cfg", "-db"], start_new_session=True,
                             stdout=open("/var/log/haproxy-stdout.log", "ab"), stderr=subprocess.STDOUT)
        put(u, "pid", p.pid); return
    if name in ("chrony", "ssh", "nginx") or not ex:
        put(u, "flag", "1"); return
    p = subprocess.Popen(shlex.split(ex[0]), start_new_session=True,
                         stdout=open(f"/var/log/{name}.log", "ab"), stderr=subprocess.STDOUT)
    put(u, "pid", p.pid)


def stop(u):
    name = u[:-8]
    pid = get(u, "pid")
    if name == "mysql":
        # Ubuntu's mysqld writes no pid file (systemd uses the cgroup): find it as the child of our supervisor
        try:
            sup = int(open(f"{D}/mysql.sup").read())
            kids = [int(x) for x in subprocess.run(["pgrep", "-P", str(sup)], capture_output=True, text=True).stdout.split()]
            os.kill(sup, signal.SIGKILL)
            for k in kids:
                try: os.kill(k, signal.SIGTERM)
                except OSError: pass
            for _ in range(120):
                if not any(alive(k) for k in kids): break
                time.sleep(0.5)
        except Exception: pass
    elif pid and alive(pid):
        try: os.killpg(int(pid), signal.SIGTERM)
        except Exception: pass
        time.sleep(0.5)
    for k in ("pid", "flag"):
        try: os.remove(f(u, k))
        except OSError: pass


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-") or a == "--now"]
    now = "--now" in args; args = [a for a in args if a != "--now"]
    if not args: return 0
    cmd, units = args[0], [x if "." in x else x + ".service" for x in args[1:]]
    if cmd == "show":
        for u in units:
            a = active(u)
            print(f"Id={u}\nLoadState=loaded\nActiveState={'active' if a else 'inactive'}\nSubState={'running' if a else 'dead'}\n"
                  f"FragmentPath=/etc/systemd/system/{u}\nUnitFileState={get(u, 'enabled', 'disabled')}\nMainPID={get(u, 'pid', '0')}\nNames={u}")
    elif cmd in ("start", "try-restart"):
        for u in units: start(u)
    elif cmd in ("restart", "reload-or-restart"):
        for u in units: stop(u); start(u)
    elif cmd == "reload":
        pass
    elif cmd == "stop":
        for u in units: stop(u)
    elif cmd == "enable":
        for u in units:
            put(u, "enabled", "enabled")
            if now: start(u)
    elif cmd == "disable":
        for u in units: put(u, "enabled", "disabled")
    elif cmd == "is-active": return 0 if all(active(u) for u in units) else 3
    elif cmd == "is-enabled": return 0 if all(get(u, "enabled") == "enabled" for u in units) else 1
    elif cmd == "is-failed": return 1
    return 0


sys.exit(main())
