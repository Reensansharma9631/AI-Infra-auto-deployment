"""OS detection and readiness checks (pure logic, fed with real os-release / fact outputs)."""
import pytest

from autoinfra import drivers, sshops

OSR = {
    "ubuntu2404": ('ID=ubuntu\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04.1 LTS"\nID_LIKE=debian', "ubuntu", "debian", "supported"),
    "ubuntu2204": ('ID=ubuntu\nVERSION_ID="22.04"\nPRETTY_NAME="Ubuntu 22.04.4 LTS"', "ubuntu", "debian", "compat"),
    "ubuntu2004": ('ID=ubuntu\nVERSION_ID="20.04"', "ubuntu", "debian", "unsupported"),
    "ubuntu2410": ('ID=ubuntu\nVERSION_ID="24.10"', "ubuntu", "debian", "unsupported"),
    "debian12": ('ID=debian\nVERSION_ID="12"', "debian", "debian", "compat"),
    "rocky9": ('ID="rocky"\nVERSION_ID="9.4"\nID_LIKE="rhel centos fedora"\nPRETTY_NAME="Rocky Linux 9.4 (Blue Onyx)"', "rocky", "rhel", "supported"),
    "rocky8": ('ID="rocky"\nVERSION_ID="8.10"', "rocky", "rhel", "compat"),
    "alma9": ('ID="almalinux"\nVERSION_ID="9.3"\nPRETTY_NAME="AlmaLinux 9.3 (Shamrock Pampas Cat)"', "almalinux", "rhel", "supported"),
    "alma8": ('ID="almalinux"\nVERSION_ID="8.9"', "almalinux", "rhel", "compat"),
    "rhel9": ('ID="rhel"\nVERSION_ID="9.4"', "rhel", "rhel", "supported"),
    "rhel8": ('ID="rhel"\nVERSION_ID="8.10"', "rhel", "rhel", "compat"),
    "rhel7": ('ID="rhel"\nVERSION_ID="7.9"', "rhel", "rhel", "unsupported"),
    "centos7": ('ID="centos"\nVERSION_ID="7"', "centos", "rhel", "unsupported"),
    "centos9": ('ID="centos"\nVERSION_ID="9"', "centos", "rhel", "supported"),
    "ol8": ('ID="ol"\nVERSION_ID="8.9"', "ol", "rhel", "compat"),
    "ol9": ('ID="ol"\nVERSION_ID="9.3"', "ol", "rhel", "supported"),
    "eurolinux9": ('ID="eurolinux"\nVERSION_ID="9.2"\nID_LIKE="rhel fedora centos"', "eurolinux", "rhel", "compat"),
    "navy8": ('ID="navy"\nVERSION_ID="8.6"\nID_LIKE="rhel"', "navy", "rhel", "compat"),
    "fedora40": ('ID=fedora\nVERSION_ID=40', "fedora", "", "unsupported"),
    "mint": ('ID=linuxmint\nVERSION_ID="21"\nID_LIKE="ubuntu debian"', "linuxmint", "debian", "unsupported"),
    "sles": ('ID="sles"\nVERSION_ID="15.5"', "sles", "", "unsupported"),
    "empty": ("", "unknown", "", "unsupported"),
    "comments": ('# comment\nID=ubuntu\n\nVERSION_ID=24.04\n', "ubuntu", "debian", "supported"),
}


@pytest.mark.parametrize("name", list(OSR))
def test_classify(name):
    text, oid, fam, sup = OSR[name]
    c = drivers.classify_os(drivers.parse_os_release(text))
    assert (c["id"], c["family"], c["support"]) == (oid, fam, sup)


@pytest.mark.parametrize("fam", ["debian", "rhel"])
def test_package_managers_defined(fam):
    pm = drivers.PKG_MANAGERS[fam]
    for k in ("tool", "refresh", "upgrade", "count", "installed", "base_pkgs"):
        assert pm[k]
    assert ("apt" in pm["refresh"]) == (fam == "debian")


@pytest.mark.parametrize("sid", list(drivers.DB_STRATEGIES))
def test_db_strategy_shape(sid):
    st = drivers.DB_STRATEGIES[sid]
    assert st["playbook"].endswith(".yml") and st["nodes"] and set(st["families"]) == {"debian", "rhel"}


@pytest.mark.parametrize("oid,ver,expect", [("ubuntu", "24.04", "Ubuntu 24.04"), ("almalinux", "9.3", "AlmaLinux 9.3"), ("rocky", "9", "Rocky Linux 9"), ("zzz", "1", "zzz 1")])
def test_short_name(oid, ver, expect):
    assert drivers.short_name(oid, ver) == expect


def facts(**kw):
    base = ("UID=0\nSUDO=root\nOSR_ID=ubuntu\nOSR_VERSION_ID=\"24.04\"\nOSR_PRETTY_NAME=\"Ubuntu 24.04 LTS\"\nKERNEL=6.8.0\nHOSTNAME=n1\n"
            "CPUS=8\nMEM_MB=16000\nDISK_FREE_GB=100\nDISK_TOTAL_GB=200\nKVM_DEV=1\nVIRT_FLAGS=8\nEPOCH=1000\nPKG=apt\nUPDATES=0\n"
            "HAS_python3=1\nHAS_curl=1\nPYTHON=3.12\nREPO_HTTP=200\nSSHD_PWAUTH=no\nFW=ufw:inactive\nIFACE=eth0 10.0.0.5/24\n")
    lines = dict(l.split("=", 1) for l in base.strip().splitlines() if not l.startswith("IFACE"))
    lines.update({k: str(v) for k, v in kw.items()})
    return sshops.parse_facts("\n".join(f"{k}={v}" for k, v in lines.items()) + "\nIFACE=eth0 10.0.0.5/24\n")


TH = {"min_cpu": 2, "min_mem_mb": 3500, "min_disk_gb": 20}


def check(res, name):
    return next(c for c in res if c["name"] == name)["status"]


def test_all_pass():
    r = sshops.evaluate(facts(), TH, master_epoch=1000)
    assert sshops.overall(r) == "ready", r


@pytest.mark.parametrize("kw,name,status", [
    ({"SUDO": "nopasswd"}, "Root / sudo access", "pass"), ({"SUDO": "no"}, "Root / sudo access", "fail"),
    ({"CPUS": 1}, "CPU cores", "fail"), ({"CPUS": 2}, "CPU cores", "pass"),
    ({"MEM_MB": 2000}, "Memory", "fail"), ({"MEM_MB": 3500}, "Memory", "pass"),
    ({"DISK_FREE_GB": 5}, "Disk free on /", "fail"), ({"DISK_FREE_GB": 20}, "Disk free on /", "pass"),
    ({"KVM_DEV": 0, "VIRT_FLAGS": 4}, "CPU virtualization (KVM)", "warn"), ({"KVM_DEV": 0, "VIRT_FLAGS": 0}, "CPU virtualization (KVM)", "warn"),
    ({"EPOCH": 1003}, "Clock skew vs master", "pass"), ({"EPOCH": 1030}, "Clock skew vs master", "warn"), ({"EPOCH": 1200}, "Clock skew vs master", "fail"),
    ({"PKG": "dnf"}, "Package manager", "fail"), ({"REPO_HTTP": "404"}, "Master repository reachable", "fail"),
    ({"REPO_HTTP": "000"}, "Master repository reachable", "fail"), ({"REPO_HTTP": "301"}, "Master repository reachable", "pass"),
    ({"UPDATES": 12}, "Updates available", "warn"), ({"HAS_curl": 0}, "Required packages", "warn"),
    ({"SSHD_PWAUTH": "yes"}, "Security checks", "warn"), ({"PYTHON": "none"}, "Python for Ansible", "fail"),
    ({"OSR_VERSION_ID": "22.04"}, "OS compatibility", "warn"), ({"OSR_VERSION_ID": "20.04"}, "OS compatibility", "fail"),
])
def test_single_check(kw, name, status):
    assert check(sshops.evaluate(facts(**kw), TH, master_epoch=1000), name) == status


def test_kvm_required_for_compute_role():
    r = sshops.evaluate(facts(KVM_DEV=0, VIRT_FLAGS=0), TH, roles=["compute"])
    assert check(r, "CPU virtualization (KVM)") == "fail" and sshops.overall(r) == "not-ready"


def test_rhel_family_package_manager():
    f = facts(OSR_ID="almalinux", OSR_VERSION_ID="9.3", PKG="dnf")
    r = sshops.evaluate(f, TH)
    assert check(r, "Package manager") == "pass" and check(r, "OS compatibility") == "pass"


@pytest.mark.parametrize("statuses,expect", [(["pass"], "ready"), (["pass", "warn"], "ready-with-warnings"), (["warn", "fail"], "not-ready"), ([], "ready")])
def test_overall(statuses, expect):
    assert sshops.overall([{"status": s} for s in statuses]) == expect


def test_parse_facts_interfaces_and_summary():
    f = facts()
    assert f["IFACES"] == ["eth0 10.0.0.5/24"]
    s = sshops.summarize(f)
    assert s["os_family"] == "debian" and s["updates"] == 0 and s["kernel"] == "6.8.0"


def test_parse_kv_ignores_garbage():
    f = sshops.parse_kv("garbage line\nA=1\n=x\nB C=2\nOK_KEY=yes\n")
    assert f == {"A": "1", "OK_KEY": "yes"}


@pytest.mark.parametrize("text,cpu,mem,disk,rx", [
    ("NET1=100 200\nNET2=1100 1200\nCPU=12\nMEM=40.5\nDISK=33\nLOAD=0.42", 12, 40.5, 33, 1000),
    ("NET1=0 0\nNET2=0 0\nCPU=0\nMEM=0\nDISK=0\nLOAD=0", 0, 0, 0, 0),
    ("CPU=99\nMEM=99.9\nDISK=100\nLOAD=12.5", 99, 99.9, 100, None),
    ("NET1=500 500\nNET2=100 100\nCPU=5\nMEM=5\nDISK=5\nLOAD=1", 5, 5, 5, 0),
])
def test_parse_metrics(text, cpu, mem, disk, rx):
    m = sshops.parse_metrics(text)
    assert (m["cpu"], m["mem"], m["disk"], m["net_rx"]) == (cpu, mem, disk, rx)


def test_parse_metrics_bad_numbers():
    assert sshops.parse_metrics("CPU=abc")["cpu"] is None


def test_with_retries_succeeds_after_errors():
    n = {"i": 0}

    def fn():
        n["i"] += 1
        if n["i"] < 3:
            raise OSError("timeout")
        return "ok"
    assert sshops.with_retries(fn, attempts=4, delay=0) == "ok" and n["i"] == 3


def test_with_retries_stops_on_auth_failures():
    n = {"i": 0}

    def fn():
        n["i"] += 1
        raise sshops.AuthFailed("no")
    with pytest.raises(sshops.AuthFailed):
        sshops.with_retries(fn, attempts=5, delay=0, auth_attempts=2)
    assert n["i"] == 2


def test_with_retries_gives_up():
    with pytest.raises(OSError):
        sshops.with_retries(lambda: (_ for _ in ()).throw(OSError("x")), attempts=2, delay=0)


def test_tcp_ping_closed_port():
    ok, info = sshops.tcp_ping("127.0.0.1", 1, timeout=1)
    assert ok is False


def test_collect_script_is_posix_sh():
    import shutil, subprocess
    sh = shutil.which("dash") or shutil.which("sh")
    r = subprocess.run([sh, "-n"], input=sshops.COLLECT, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([sh, "-n"], input=sshops.METRICS, text=True, capture_output=True)
    assert r.returncode == 0, r.stderr


def test_collect_script_runs_locally():
    """Run the real fact script on this machine and parse it."""
    import subprocess
    r = subprocess.run(["sh", "-s"], input=sshops.COLLECT, text=True, capture_output=True, timeout=120)
    f = sshops.parse_facts(r.stdout)
    assert f["os"]["family"] in ("debian", "rhel") and int(f["CPUS"]) >= 1 and int(f["MEM_MB"]) > 0


def test_metrics_script_runs_locally():
    import subprocess
    r = subprocess.run(["sh", "-s"], input=sshops.METRICS, text=True, capture_output=True, timeout=30)
    m = sshops.parse_metrics(r.stdout)
    assert 0 <= m["cpu"] <= 100 and 0 <= m["mem"] <= 100 and 0 <= m["disk"] <= 100



def test_duplicate_machine_id_warns(app, operator):
    """Cloned VMs share /etc/machine-id (the lab did) - the check job must say so."""
    from conftest import add_host
    from autoinfra import db, hosts
    a, b, c = (add_host(operator, ip, hostname=n) for ip, n in (("10.9.0.1", "h1"), ("10.9.0.2", "h2"), ("10.9.0.3", "h3")))
    logs = []
    with app.app_context():
        for hid, mid in ((a, "same"), (b, "same"), (c, "other")):
            db.ex("INSERT INTO checks(run_id,host_id,name,status,detail) VALUES(?,?,?,?,?)", (1, hid, "Machine ID", "pass", mid))
        summary = {a: "ready", b: "ready", c: "ready"}
        hs = [db.one("SELECT * FROM hosts WHERE id=?", (i,)) for i in (a, b, c)]
        hosts._duplicate_machine_ids(logs.append, hs, 1, summary)
        warn = {r["host_id"] for r in db.q("SELECT host_id FROM checks WHERE name='Unique machine ID' AND status='warn'")}
    assert warn == {a, b} and summary == {a: "ready-with-warnings", b: "ready-with-warnings", c: "ready"}
    assert any("same /etc/machine-id as h2" in l for l in logs)


def test_machine_id_reported():
    r = sshops.evaluate(facts(MACHINE_ID="c092735d68eb4e01a129d03eefe52b7f"), TH, master_epoch=1000)
    assert check(r, "Machine ID") == "pass" and any(c["detail"] == "c092735d68eb4e01a129d03eefe52b7f" for c in r)
    assert all(c["name"] != "Machine ID" for c in sshops.evaluate(facts(), TH, master_epoch=1000))



@pytest.mark.parametrize("val,status", [("yes", "pass"), ("no", "warn")])
def test_ntp_synchronized_check(val, status):
    assert check(sshops.evaluate(facts(NTP_SYNC=val), TH, master_epoch=1000), "Time synchronized (NTP)") == status
