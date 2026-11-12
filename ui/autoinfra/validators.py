"""Input validation shared by every API. Each function returns None when the value is valid, else an error string."""
import ipaddress, re

HOST_RE = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
IFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
VERSION_RE = re.compile(r"^[0-9]+(\.[0-9A-Za-z]+){0,3}(-[0-9A-Za-z.]+)?$")
TZ_RE = re.compile(r"^(UTC|[A-Z][A-Za-z_]+(/[A-Z][A-Za-z_+-]+){1,2})$")


def ipv4(v):
    try:
        ipaddress.IPv4Address(str(v).strip())
        return None
    except (ipaddress.AddressValueError, ValueError):
        return f"'{v}' is not a valid IPv4 address"


def ip(v):
    try:
        ipaddress.ip_address(str(v).strip())
        return None
    except ValueError:
        return f"'{v}' is not a valid IP address"


def cidr(v):
    s = str(v).strip()
    if "/" not in s:
        return f"'{v}' is not a CIDR (expected a.b.c.d/nn)"
    try:
        ipaddress.ip_network(s, strict=True)
        return None
    except ValueError as e:
        return f"'{v}' is not a valid network: {e}"


def netmask(v):
    try:
        n = ipaddress.IPv4Network(f"0.0.0.0/{str(v).strip()}")
        if str(n.netmask) != str(v).strip():
            return f"'{v}' is not a dotted netmask"
        return None
    except ValueError:
        return f"'{v}' is not a valid netmask"


def hostname(v):
    s = str(v or "").strip()
    if not s:
        return "hostname is required"
    if not HOST_RE.match(s) or s.replace(".", "").isdigit():
        return f"'{v}' is not a valid hostname"
    return None


def host_or_ip(v):
    return None if ip(v) is None else hostname(v)


def port(v):
    try:
        p = int(v)
    except (TypeError, ValueError):
        return f"'{v}' is not a port number"
    return None if 1 <= p <= 65535 else f"port {p} is out of range 1-65535"


def vlan(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return f"'{v}' is not a VLAN id"
    return None if 1 <= n <= 4094 else f"VLAN {n} is out of range 1-4094"


def vlan_range(v):
    s = str(v or "").strip()
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", s)
    if not m:
        return vlan(s) and f"'{v}' is not a VLAN range (expected 100-200)"
    a, b = int(m.group(1)), int(m.group(2))
    if vlan(a) or vlan(b):
        return f"VLAN range '{v}' must stay within 1-4094"
    return None if a <= b else f"VLAN range '{v}' starts after it ends"


def name(v, what="name"):
    s = str(v or "").strip()
    if not s:
        return f"{what} is required"
    return None if NAME_RE.match(s) else f"{what} '{v}' may only contain letters, digits, space, . _ - (max 63)"


def username(v):
    return None if USER_RE.match(str(v or "")) else f"'{v}' is not a valid user name"


def abspath(v):
    s = str(v or "")
    if not s.startswith("/"):
        return f"'{v}' must be an absolute path"
    if ".." in s.split("/") or not PATH_RE.match(s):
        return f"'{v}' contains characters that are not allowed"
    return None


def email(v):
    return None if EMAIL_RE.match(str(v or "")) else f"'{v}' is not a valid e-mail address"


def iface(v):
    return None if IFACE_RE.match(str(v or "")) else f"'{v}' is not a valid interface name"


def version(v):
    return None if VERSION_RE.match(str(v or "")) else f"'{v}' is not a version number"


def timezone(v):
    return None if TZ_RE.match(str(v or "")) else f"'{v}' is not a timezone like Asia/Kolkata"


def password(v, min_len=12):
    s = str(v or "")
    if len(s) < min_len:
        return f"password must be at least {min_len} characters"
    classes = sum(bool(re.search(p, s)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    if classes < 3:
        return "password needs 3 of: lower case, upper case, digit, symbol"
    return None


def nfs_url(v):
    s = str(v or "")
    m = re.match(r"^nfs://([^/]+)(/.*)$", s)
    if not m:
        return f"'{v}' is not an NFS URL (nfs://server/path)"
    return host_or_ip(m.group(1)) or abspath(m.group(2))


def ip_in(v, net):
    e = ipv4(v) or cidr(net)
    if e:
        return e
    if ipaddress.IPv4Address(v) not in ipaddress.ip_network(net):
        return f"{v} is not inside {net}"
    return None


def ip_range(start, end, gateway=None, mask=None):
    e = ipv4(start) or ipv4(end)
    if e:
        return e
    a, b = ipaddress.IPv4Address(start), ipaddress.IPv4Address(end)
    if a > b:
        return f"range {start} - {end} starts after it ends"
    if gateway is not None:
        e = ipv4(gateway) or (netmask(mask) if mask is not None else None)
        if e:
            return e
        if mask is not None:
            net = ipaddress.IPv4Network(f"{gateway}/{mask}", strict=False)
            if a not in net or b not in net:
                return f"range {start} - {end} is not inside {net}"
        if a <= ipaddress.IPv4Address(gateway) <= b:
            return f"gateway {gateway} must not be inside the range {start} - {end}"
    return None


def choice(v, options, what="value"):
    return None if v in options else f"{what} must be one of: {', '.join(map(str, options))}"


def int_between(v, lo, hi, what="value"):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return f"{what} must be a whole number"
    return None if lo <= n <= hi else f"{what} must be between {lo} and {hi}"


def number_between(v, lo, hi, what="value"):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return f"{what} must be a number"
    return None if lo <= n <= hi else f"{what} must be between {lo} and {hi}"


def collect(*pairs):
    """collect(("field", err_or_None), ...) -> {"field": "error"} for the failing ones."""
    return {k: e for k, e in pairs if e}
