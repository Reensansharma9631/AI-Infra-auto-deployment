"""Apache CloudStack API client (signed requests, async-job polling) and the idempotent zone bootstrap.
Tested against tests/mock_cloudstack_api.py (a documented-API simulator), not against a live CloudStack."""
import base64, hashlib, hmac, http.cookiejar, json, time, urllib.error, urllib.parse, urllib.request


class CSError(Exception):
    pass


def is_auth_error(e):
    """Credentials refused - CloudStack answers HTTP 401/531, or an errorcode 401/531 inside a 200 body."""
    t = str(e)
    return any(x in t for x in ("HTTP 401", "HTTP 531", "(code 401)", "(code 531)"))


class CS:
    def __init__(self, url, api_key=None, secret=None, timeout=30, poll_interval=2, poll_max=300):
        self.base = url.rstrip("/")
        if not self.base.endswith("/client/api"):
            self.base += "/client/api"
        self.api_key, self.secret, self.session = api_key, secret, None
        self.timeout, self.poll_interval, self.poll_max = timeout, poll_interval, poll_max
        # the login session is the sessionkey PLUS the JSESSIONID cookie; no proxy for a LAN address
        self._jar = http.cookiejar.CookieJar()
        self._op = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self._jar))

    def _sign(self, p):
        q = "&".join(f"{k.lower()}={urllib.parse.quote(str(v), safe='')}" for k, v in sorted(p.items(), key=lambda kv: kv[0].lower())).lower()
        return base64.b64encode(hmac.new(self.secret.encode(), q.encode(), hashlib.sha1).digest()).decode()

    def _call(self, params, post=False):
        p = {k: v for k, v in dict(params, response="json").items() if v is not None}
        if self.session:
            p["sessionkey"] = self.session
        elif self.api_key:
            p["apikey"] = self.api_key
            p["signature"] = self._sign(p)
        data = urllib.parse.urlencode(p)
        req = urllib.request.Request(self.base, data=data.encode()) if post else urllib.request.Request(f"{self.base}?{data}")
        try:
            with self._op.open(req, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                j = json.loads(body)
                inner = next(iter(j.values()))
                raise CSError(f"{params.get('command')}: {inner.get('errortext', body[:300])} (HTTP {e.code})") from None
            except (ValueError, StopIteration, AttributeError):
                raise CSError(f"{params.get('command')}: HTTP {e.code} {body[:300]}") from None
        except urllib.error.URLError as e:
            raise CSError(f"cannot reach CloudStack at {self.base}: {e.reason}") from None
        except (OSError, ValueError) as e:          # reset while starting, or a non-JSON answer from a proxy
            raise CSError(f"cannot reach CloudStack at {self.base}: {e}") from None

    def _retry(self, what, fn, wait, log):
        end, n = time.time() + max(0, wait), 0
        while True:
            try:
                return fn()
            except CSError as e:
                n += 1
                if time.time() >= end or "login failed" in str(e) or is_auth_error(e):   # wrong password / keys: no retry
                    raise
                if log and n % 6 == 1:
                    log(f"[api] {what}: not ready yet ({e}); retrying")
                time.sleep(min(10, max(0.05, wait / 60)))

    def login_retry(self, user, password, wait=900, log=None):
        return self._retry("login", lambda: self.login(user, password), wait, log)

    def wait_ready(self, wait=900, log=None):
        return self._retry("API", lambda: self.run("listCapabilities"), wait, log)

    def login(self, user, password, domain=""):
        r = self._call({"command": "login", "username": user, "password": password, "domain": domain}, post=True)
        r = r.get("loginresponse", {})
        if "sessionkey" not in r:
            raise CSError(f"login failed: {r.get('errortext', r)}")
        self.session = r["sessionkey"]
        keys = self.run("registerUserKeys", id=r["userid"])["userkeys"]
        self.api_key, self.secret, self.session = keys["apikey"], keys["secretkey"], None
        # from now on: signed requests only. CloudStack rejects a signed request that still carries the login's
        # JSESSIONID/sessionkey cookies ("unable to verify user credentials", HTTP 401 - seen on 4.22.1.1)
        self._jar.clear()
        return self.api_key, self.secret

    def run(self, command, **params):
        r = self._call({"command": command, **params})
        body = r.get(command.lower() + "response", next(iter(r.values())) if r else {})
        if isinstance(body, dict) and "errorcode" in body:
            raise CSError(f"{command} failed: {body.get('errortext')} (code {body['errorcode']})")
        if isinstance(body, dict) and "jobid" in body and "jobstatus" not in body and command.lower() != "queryasyncjobresult":
            for _ in range(self.poll_max):
                j = self._call({"command": "queryAsyncJobResult", "jobid": body["jobid"]}).get("queryasyncjobresultresponse", {})
                if j.get("jobstatus", 0) == 1:
                    return j.get("jobresult", {})
                if j.get("jobstatus", 0) == 2:
                    raise CSError(f"{command} job failed: {j.get('jobresult')}")
                time.sleep(self.poll_interval)
            raise CSError(f"{command}: async job timed out")
        return body

    def items(self, list_cmd, key, **filt):
        return (self.run(list_cmd, **filt) or {}).get(key) or []

    def find(self, list_cmd, key, **filt):
        it = self.items(list_cmd, key, **filt)
        return it[0] if it else None


# UI resource -> (list command, response key, columns)
RESOURCES = {
    "zones": ("listZones", "zone"), "pods": ("listPods", "pod"), "clusters": ("listClusters", "cluster"),
    "hosts": ("listHosts", "host"), "storagepools": ("listStoragePools", "storagepool"), "imagestores": ("listImageStores", "imagestore"),
    "networks": ("listNetworks", "network"), "physicalnetworks": ("listPhysicalNetworks", "physicalnetwork"),
    "vlanipranges": ("listVlanIpRanges", "vlaniprange"), "systemvms": ("listSystemVms", "systemvm"),
    "templates": ("listTemplates", "template"), "serviceofferings": ("listServiceOfferings", "serviceoffering"),
    "diskofferings": ("listDiskOfferings", "diskoffering"), "virtualmachines": ("listVirtualMachines", "virtualmachine"),
    "volumes": ("listVolumes", "volume"), "capacity": ("listCapacity", "capacity"), "alerts": ("listAlerts", "alert"),
    "routers": ("listRouters", "router"),
}
LIST_DEFAULTS = {"templates": {"templatefilter": "all"}, "virtualmachines": {"listall": "true"}, "volumes": {"listall": "true"},
                 "networks": {"listall": "true"}, "routers": {"listall": "true"}, "hosts": {"type": "Routing"}}

ACTIONS = {   # (resource, action) -> (command, id-param, extra)
    ("zones", "enable"): ("updateZone", "id", {"allocationstate": "Enabled"}),
    ("zones", "disable"): ("updateZone", "id", {"allocationstate": "Disabled"}),
    ("zones", "delete"): ("deleteZone", "id", {}),
    ("pods", "enable"): ("updatePod", "id", {"allocationstate": "Enabled"}),
    ("pods", "disable"): ("updatePod", "id", {"allocationstate": "Disabled"}),
    ("pods", "delete"): ("deletePod", "id", {}),
    ("clusters", "enable"): ("updateCluster", "id", {"allocationstate": "Enabled"}),
    ("clusters", "disable"): ("updateCluster", "id", {"allocationstate": "Disabled"}),
    ("clusters", "delete"): ("deleteCluster", "id", {}),
    ("hosts", "maintenance"): ("prepareHostForMaintenance", "id", {}),
    ("hosts", "cancel-maintenance"): ("cancelHostMaintenance", "id", {}),
    ("hosts", "enable"): ("updateHost", "id", {"allocationstate": "Enable"}),
    ("hosts", "disable"): ("updateHost", "id", {"allocationstate": "Disable"}),
    ("hosts", "delete"): ("deleteHost", "id", {"forced": "true"}),
    ("storagepools", "maintenance"): ("enableStorageMaintenance", "id", {}),
    ("storagepools", "cancel-maintenance"): ("cancelStorageMaintenance", "id", {}),
    ("storagepools", "delete"): ("deleteStoragePool", "id", {}),
    ("imagestores", "delete"): ("deleteImageStore", "id", {}),
    ("systemvms", "start"): ("startSystemVm", "id", {}),
    ("systemvms", "stop"): ("stopSystemVm", "id", {}),
    ("systemvms", "reboot"): ("rebootSystemVm", "id", {}),
    ("systemvms", "destroy"): ("destroySystemVm", "id", {}),
    ("serviceofferings", "delete"): ("deleteServiceOffering", "id", {}),
    ("templates", "delete"): ("deleteTemplate", "id", {}),
    ("virtualmachines", "start"): ("startVirtualMachine", "id", {}),
    ("virtualmachines", "stop"): ("stopVirtualMachine", "id", {}),
    ("virtualmachines", "reboot"): ("rebootVirtualMachine", "id", {}),
}


def bootstrap_zone(cs, spec, log=print):
    """Advanced zone with VLAN isolation. Every object is looked up by name first (safe to re-run)."""
    z, pod, pub = spec["zone"], spec["pod"], spec["public"]
    lab = spec.get("kvm_labels") or {}
    zone = cs.find("listZones", "zone", name=z["name"])
    if zone:
        log(f"[zone] zone '{z['name']}' exists ({zone['id']})")
    else:
        zone = cs.run("createZone", name=z["name"], networktype="Advanced", dns1=z["dns1"], dns2=z.get("dns2") or None,
                      internaldns1=z.get("internal_dns1") or z["dns1"], guestcidraddress=z.get("guest_cidr", "10.1.1.0/24"),
                      localstorageenabled=str(bool(z.get("local_storage", False))).lower())["zone"]
        log(f"[zone] created zone {z['name']} ({zone['id']})")
    zid = zone["id"]
    pn = cs.find("listPhysicalNetworks", "physicalnetwork", zoneid=zid, name="Physical Network 1")
    if not pn:
        pn = cs.run("createPhysicalNetwork", zoneid=zid, name="Physical Network 1", isolationmethods="VLAN", vlan=z["vlan_range"])["physicalnetwork"]
        for tt, label in (("Management", lab.get("management", "cloudbr0")), ("Guest", lab.get("guest", "cloudbr1")),
                          ("Public", lab.get("public", "cloudbr0")), ("Storage", lab.get("storage"))):
            if label:
                cs.run("addTrafficType", physicalnetworkid=pn["id"], traffictype=tt, kvmnetworklabel=label)
        cs.run("updatePhysicalNetwork", id=pn["id"], state="Enabled")
        log("[zone] physical network + traffic types enabled")
    for prov in ("VirtualRouter", "VpcVirtualRouter", "InternalLbVm"):
        sp = cs.find("listNetworkServiceProviders", "networkserviceprovider", physicalnetworkid=pn["id"], name=prov)
        if sp and sp.get("state") != "Enabled":
            cs.run("updateNetworkServiceProvider", id=sp["id"], state="Enabled")
            log(f"[zone] provider {prov} enabled")
    p = cs.find("listPods", "pod", zoneid=zid, name=pod["name"])
    if not p:
        p = cs.run("createPod", zoneid=zid, name=pod["name"], startip=pod["start"], endip=pod["end"], netmask=pod["netmask"],
                   gateway=pod["gateway"])["pod"]
        log(f"[zone] created pod {pod['name']}")
    if not cs.items("listVlanIpRanges", "vlaniprange", zoneid=zid, forvirtualnetwork="true"):
        cs.run("createVlanIpRange", zoneid=zid, vlan=pub.get("vlan") or "untagged", gateway=pub["gateway"], netmask=pub["netmask"],
               startip=pub["start"], endip=pub["end"], forvirtualnetwork="true")
        log("[zone] public IP range created")
    clusters = spec.get("clusters") or [{"name": spec["cluster"]["name"], "hosts": spec.get("kvm_hosts", [])}]
    first_cluster = None
    for c in clusters:
        cl = cs.find("listClusters", "cluster", zoneid=zid, name=c["name"])
        if not cl:
            cl = cs.run("addCluster", zoneid=zid, podid=p["id"], clustername=c["name"], clustertype="CloudManaged", hypervisor="KVM")["cluster"][0]
            log(f"[zone] created cluster {c['name']}")
        first_cluster = first_cluster or cl
        for h in c.get("hosts", []):
            if cs.find("listHosts", "host", zoneid=zid, name=h.get("hostname") or h["ip"]) or cs.find("listHosts", "host", zoneid=zid, keyword=h["ip"]):
                log(f"[zone] host {h['ip']} exists")
                continue
            cs.run("addHost", zoneid=zid, podid=p["id"], clusterid=cl["id"], hypervisor="KVM", clustertype="CloudManaged",
                   username=h.get("user", "root"), password=h.get("password") or None, url=f"http://{h['ip']}")
            log(f"[zone] host {h['ip']} added to {c['name']}")
        if c.get("primary_nfs") and not cs.find("listStoragePools", "storagepool", zoneid=zid, name=c.get("primary_name") or f"primary-{c['name']}"):
            cs.run("createStoragePool", zoneid=zid, podid=p["id"], clusterid=cl["id"], name=c.get("primary_name") or f"primary-{c['name']}",
                   scope="cluster", hypervisor="KVM", url=f"nfs://{c['primary_nfs']}")
            log(f"[zone] primary storage for {c['name']} added")
    if spec.get("primary_nfs") and first_cluster and not cs.find("listStoragePools", "storagepool", zoneid=zid, name="primary-nfs"):
        cs.run("createStoragePool", zoneid=zid, podid=p["id"], clusterid=first_cluster["id"], name="primary-nfs", scope="cluster",
               hypervisor="KVM", url=f"nfs://{spec['primary_nfs']}")
        log("[zone] primary storage added")
    if spec.get("secondary_nfs") and not cs.find("listImageStores", "imagestore", zoneid=zid, name="secondary-nfs"):
        cs.run("addImageStore", zoneid=zid, name="secondary-nfs", provider="NFS", url=f"nfs://{spec['secondary_nfs']}")
        log("[zone] secondary storage added")
    if spec.get("management_vip"):
        cs.run("updateConfiguration", name="host", value=spec["management_vip"])
        log(f"[zone] global setting host = {spec['management_vip']}")
    if zone.get("allocationstate") != "Enabled":
        cs.run("updateZone", id=zid, allocationstate="Enabled")
        log("[zone] zone enabled")
    log("[zone] DONE")
    return zid
