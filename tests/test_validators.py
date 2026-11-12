"""Input validation used by every API - each case is a real call."""
import pytest

from autoinfra import validators as V

GOOD_IPV4 = ["10.0.0.1", "192.168.1.254", "172.16.0.10", "8.8.8.8", "0.0.0.0", "255.255.255.255", "10.79.0.21", "100.64.0.1"]
BAD_IPV4 = ["", "10.0.0", "10.0.0.256", "abc", "10.0.0.1/24", "1.2.3.4.5", "::1", "10.0.0.-1", " ", "10..0.1", "999.1.1.1", "1.2.3.04x"]
GOOD_CIDR = ["10.0.0.0/24", "192.168.0.0/16", "10.1.1.0/24", "172.16.0.0/12", "10.0.0.0/8", "10.0.0.128/25", "0.0.0.0/0", "fd00::/64"]
BAD_CIDR = ["10.0.0.1/24", "10.0.0.0", "10.0.0.0/33", "abc/24", "", "10.0.0.0/", "/24", "10.0.0.0/-1"]
GOOD_MASK = ["255.255.255.0", "255.255.0.0", "255.0.0.0", "255.255.255.128", "255.255.255.252", "255.255.254.0", "0.0.0.0"]
BAD_MASK = ["255.0.255.0", "255.255.255.1", "24", "abc", "", "256.0.0.0", "255.255.255"]
GOOD_HOST = ["cs-mgmt-01", "db01", "node.example.com", "a", "k8s-node-1.lab.local", "x" * 63, "UPPER-case"]
BAD_HOST = ["", "-bad", "bad-", "has space", "under_score", "x" * 64, "a..b", "10.0.0.1", "ex!ample", "dot.", ".dot"]
GOOD_PORT = [1, 22, 80, 443, 3306, 8080, 65535, "22", "8443"]
BAD_PORT = [0, -1, 65536, 100000, "abc", None, "", "22.5"]
GOOD_VLAN = [1, 100, 4094, "200"]
BAD_VLAN = [0, 4095, -5, "x", None]
GOOD_VR = ["100-200", "1-4094", "300 - 399", "500-500", "150"]
BAD_VR = ["200-100", "0-10", "100-5000", "a-b", "", "100-", "-100", "10-20-30"]
GOOD_USER = ["root", "ubuntu", "cloud-user", "admin_1", "a", "ec2-user"]
BAD_USER = ["", "Root", "1user", "has space", "x" * 40, "user;rm", "-dash"]
GOOD_PATH = ["/export/primary", "/", "/var/lib/mysql", "/u01/app/oracle", "/data-1/x_y.z"]
BAD_PATH = ["export", "", "/a/../b", "/a b", "/a;b", "relative/path", "/a$(x)"]
GOOD_PW = ["Abcdefghij1!", "LongPassword2026", "pass-WORD-2026", "Zz9!Zz9!Zz9!"]
BAD_PW = ["short1A!", "alllowercaseonly", "ALLUPPER1234", "", "abcdefghijkl", "12345678901234"]
GOOD_NFS = ["nfs://10.0.0.31/export/primary", "nfs://storage01/export/sec", "nfs://a.b.c/x"]
BAD_NFS = ["10.0.0.31/export", "nfs://", "nfs://10.0.0.31", "http://x/y", "nfs://bad host/x", "nfs://h/a/../b"]
GOOD_TZ = ["UTC", "Asia/Kolkata", "Europe/Berlin", "America/New_York", "America/Argentina/Buenos_Aires"]
BAD_TZ = ["", "kolkata", "Asia/", "123", "Asia Kolkata"]
GOOD_VER = ["4.22.0", "4.20.1", "2026.1", "8.0", "19", "4.19.3-1", "1.2.3.4"]
BAD_VER = ["", "v4.22", "4..2", "abc", "4.22.0.1.2", "4.22 0"]
GOOD_IFACE = ["eth0", "ens192", "bond0.100", "cloudbr0", "enp3s0f1", "br-ex"]
BAD_IFACE = ["", "x" * 16, "eth 0", "eth0;", "eth/0"]
GOOD_NAME = ["Zone-1", "Pod 1", "cluster_a", "Primary.Storage", "z"]
BAD_NAME = ["", " ", "-zone", "zone<script>", "x" * 70, "zone;drop"]
GOOD_MAIL = ["ops@example.com", "a.b@c.io", "x+y@d.co.in"]
BAD_MAIL = ["", "a@b", "@x.com", "a b@c.com", "noat.com"]


@pytest.mark.parametrize("v", GOOD_IPV4)
def test_ipv4_good(v): assert V.ipv4(v) is None


@pytest.mark.parametrize("v", BAD_IPV4)
def test_ipv4_bad(v): assert V.ipv4(v)


@pytest.mark.parametrize("v", GOOD_CIDR)
def test_cidr_good(v): assert V.cidr(v) is None


@pytest.mark.parametrize("v", BAD_CIDR)
def test_cidr_bad(v): assert V.cidr(v)


@pytest.mark.parametrize("v", GOOD_MASK)
def test_mask_good(v): assert V.netmask(v) is None


@pytest.mark.parametrize("v", BAD_MASK)
def test_mask_bad(v): assert V.netmask(v)


@pytest.mark.parametrize("v", GOOD_HOST)
def test_hostname_good(v): assert V.hostname(v) is None


@pytest.mark.parametrize("v", BAD_HOST)
def test_hostname_bad(v): assert V.hostname(v)


@pytest.mark.parametrize("v", GOOD_HOST[:4] + GOOD_IPV4[:4])
def test_host_or_ip_good(v): assert V.host_or_ip(v) is None


@pytest.mark.parametrize("v", GOOD_PORT)
def test_port_good(v): assert V.port(v) is None


@pytest.mark.parametrize("v", BAD_PORT)
def test_port_bad(v): assert V.port(v)


@pytest.mark.parametrize("v", GOOD_VLAN)
def test_vlan_good(v): assert V.vlan(v) is None


@pytest.mark.parametrize("v", BAD_VLAN)
def test_vlan_bad(v): assert V.vlan(v)


@pytest.mark.parametrize("v", GOOD_VR)
def test_vlan_range_good(v): assert V.vlan_range(v) is None


@pytest.mark.parametrize("v", BAD_VR)
def test_vlan_range_bad(v): assert V.vlan_range(v)


@pytest.mark.parametrize("v", GOOD_USER)
def test_user_good(v): assert V.username(v) is None


@pytest.mark.parametrize("v", BAD_USER)
def test_user_bad(v): assert V.username(v)


@pytest.mark.parametrize("v", GOOD_PATH)
def test_path_good(v): assert V.abspath(v) is None


@pytest.mark.parametrize("v", BAD_PATH)
def test_path_bad(v): assert V.abspath(v)


@pytest.mark.parametrize("v", GOOD_PW)
def test_password_good(v): assert V.password(v) is None


@pytest.mark.parametrize("v", BAD_PW)
def test_password_bad(v): assert V.password(v)


@pytest.mark.parametrize("v", GOOD_NFS)
def test_nfs_good(v): assert V.nfs_url(v) is None


@pytest.mark.parametrize("v", BAD_NFS)
def test_nfs_bad(v): assert V.nfs_url(v)


@pytest.mark.parametrize("v", GOOD_TZ)
def test_tz_good(v): assert V.timezone(v) is None


@pytest.mark.parametrize("v", BAD_TZ)
def test_tz_bad(v): assert V.timezone(v)


@pytest.mark.parametrize("v", GOOD_VER)
def test_version_good(v): assert V.version(v) is None


@pytest.mark.parametrize("v", BAD_VER)
def test_version_bad(v): assert V.version(v)


@pytest.mark.parametrize("v", GOOD_IFACE)
def test_iface_good(v): assert V.iface(v) is None


@pytest.mark.parametrize("v", BAD_IFACE)
def test_iface_bad(v): assert V.iface(v)


@pytest.mark.parametrize("v", GOOD_NAME)
def test_name_good(v): assert V.name(v) is None


@pytest.mark.parametrize("v", BAD_NAME)
def test_name_bad(v): assert V.name(v)


@pytest.mark.parametrize("v", GOOD_MAIL)
def test_mail_good(v): assert V.email(v) is None


@pytest.mark.parametrize("v", BAD_MAIL)
def test_mail_bad(v): assert V.email(v)


@pytest.mark.parametrize("start,end,gw,mask,ok", [
    ("10.0.0.100", "10.0.0.130", "10.0.0.1", "255.255.255.0", True),
    ("10.0.0.2", "10.0.0.254", "10.0.0.1", "255.255.255.0", True),
    ("10.0.0.130", "10.0.0.100", "10.0.0.1", "255.255.255.0", False),
    ("10.0.0.100", "10.0.1.130", "10.0.0.1", "255.255.255.0", False),
    ("10.0.0.1", "10.0.0.50", "10.0.0.1", "255.255.255.0", False),
    ("10.0.0.100", "10.0.0.130", "10.0.0.120", "255.255.255.0", False),
    ("10.0.0.100", "10.0.1.130", "10.0.0.1", "255.255.254.0", True),
    ("bad", "10.0.0.1", "10.0.0.1", "255.255.255.0", False),
    ("10.0.0.10", "10.0.0.20", "10.0.0.1", "255.0.255.0", False),
    ("10.0.0.10", "10.0.0.10", "10.0.0.1", "255.255.255.0", True),
])
def test_ip_range(start, end, gw, mask, ok):
    assert (V.ip_range(start, end, gw, mask) is None) == ok


@pytest.mark.parametrize("ip,net,ok", [("10.0.0.5", "10.0.0.0/24", True), ("10.0.1.5", "10.0.0.0/24", False),
                                       ("x", "10.0.0.0/24", False), ("10.0.0.5", "bad", False), ("192.168.5.5", "192.168.0.0/16", True)])
def test_ip_in(ip, net, ok):
    assert (V.ip_in(ip, net) is None) == ok


@pytest.mark.parametrize("v,lo,hi,ok", [(5, 1, 10, True), (0, 1, 10, False), (11, 1, 10, False), ("7", 1, 10, True), ("x", 1, 10, False), (None, 1, 10, False)])
def test_int_between(v, lo, hi, ok):
    assert (V.int_between(v, lo, hi) is None) == ok


@pytest.mark.parametrize("v,ok", [(0.5, True), ("2.5", True), (-1, False), ("abc", False), (1e10, False)])
def test_number_between(v, ok):
    assert (V.number_between(v, 0, 1e9) is None) == ok


@pytest.mark.parametrize("v,ok", [("a", True), ("b", True), ("z", False), (None, False)])
def test_choice(v, ok):
    assert (V.choice(v, ["a", "b"]) is None) == ok


def test_collect_keeps_only_errors():
    assert V.collect(("a", None), ("b", "bad"), ("c", "")) == {"b": "bad"}
