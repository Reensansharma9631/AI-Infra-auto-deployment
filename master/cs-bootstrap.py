#!/usr/bin/env python3
"""
Create an Advanced zone (VLAN isolation) on a CloudStack that is already running behind the management VIP:
zone, physical network + traffic types, providers, pod, public range, KVM cluster, hosts, primary + secondary NFS,
and the `host` global setting (= management VIP, so agents and system VMs talk to the load balancer).
Idempotent: every object is looked up by name first.

usage: cs-bootstrap.py spec.json            (spec example: docs/cs-zone-spec.example.json)
Passwords for KVM hosts come from the spec file only - delete it afterwards. No third-party libraries needed.

EXPERIMENTAL: written against the documented CloudStack API and tested only against a MOCK server
(tests/mock_cloudstack_api.py). Expect to adjust parameters on the first real run; the first failing call is printed.
"""
import base64, hashlib, hmac, json, sys, time, urllib.parse, urllib.request


class CS:
    def __init__(self, base, verbose=False):
        self.base = base.rstrip("/") + "/client/api"; self.api_key = self.secret = self.session = None; self.verbose = verbose

    def _call(self, params, post=False):
        p = dict(params, response="json")
        if self.session: p["sessionkey"] = self.session
        if self.api_key and not self.session:
            p["apikey"] = self.api_key
            q = "&".join(f"{k.lower()}={urllib.parse.quote(str(v), safe='')}" for k, v in sorted(p.items(), key=lambda kv: kv[0].lower())).lower()
            p["signature"] = base64.b64encode(hmac.new(self.secret.encode(), q.encode(), hashlib.sha1).digest()).decode()
        data = urllib.parse.urlencode(p).encode()
        req = urllib.request.Request(self.base, data=data) if post else urllib.request.Request(f"{self.base}?{data.decode()}")
        if self.verbose: print("   >", {k: v for k, v in params.items() if "pass" not in k.lower()})
        try:
            # addHost answers only after the agent is set up (minutes); NFS pool/store creation mounts first
            slow = str(params.get("command", "")).lower() in ("addhost", "createstoragepool", "addimagestore")
            with urllib.request.urlopen(req, timeout=1800 if slow else 60) as r: return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise SystemExit(f"API {params.get('command')} failed: HTTP {e.code} {body[:400]}")

    def login(self, user, password):
        r = self._call({"command": "login", "username": user, "password": password, "domain": ""}, post=True)["loginresponse"]
        if "sessionkey" not in r:
            raise SystemExit(f"login failed: {r.get('errortext', r)}")
        self.session = r["sessionkey"]
        keys = self._call({"command": "registerUserKeys", "id": r["userid"]})["registeruserkeysresponse"]["userkeys"]
        self.api_key, self.secret, self.session = keys["apikey"], keys["secretkey"], None   # use signed requests from now on

    def run(self, command, **params):
        r = self._call({"command": command, **params}); key = next(iter(r))
        body = r[key]
        if "errorcode" in body: raise SystemExit(f"{command} failed: {body.get('errortext')} (code {body['errorcode']}) params={ {k: v for k, v in params.items() if 'pass' not in k} }")
        if "jobid" in body and "jobstatus" not in body:       # asynchronous command: poll
            for _ in range(180):
                j = self._call({"command": "queryAsyncJobResult", "jobid": body["jobid"]})["queryasyncjobresultresponse"]
                if j.get("jobstatus", 0) == 1: return j.get("jobresult", {})
                if j.get("jobstatus", 0) == 2: raise SystemExit(f"{command} job failed: {j.get('jobresult')}")
                time.sleep(2)
            raise SystemExit(f"{command}: job timed out")
        return body

    def find(self, list_cmd, key, **filt):
        r = self.run(list_cmd, **filt); items = r.get(key) or []
        return items[0] if items else None


ELEMENTS = {"VirtualRouter": ("listVirtualRouterElements", "configureVirtualRouterElement"),
            "VpcVirtualRouter": ("listVirtualRouterElements", "configureVirtualRouterElement"),
            "InternalLbVm": ("listInternalLoadBalancerElements", "configureInternalLoadBalancerElement")}


def main(spec):
    cs = CS(spec["mgmt_url"], verbose=spec.get("verbose", False)); cs.login("admin", spec["admin_password"])
    z, pod, pub, lab = spec["zone"], spec["pod"], spec["public"], spec.get("kvm_labels", {})
    say = lambda m: print(f"[zone] {m}", flush=True)

    zone = cs.find("listZones", "zone", name=z["name"])
    if zone: say(f"zone '{z['name']}' exists ({zone['id']})")
    else:
        zone = cs.run("createZone", name=z["name"], networktype="Advanced", dns1=z["dns1"], internaldns1=z.get("internal_dns1", z["dns1"]),
                      guestcidraddress=z.get("guest_cidr", "10.1.1.0/24"))["zone"]; say(f"created zone {zone['id']}")
    zid = zone["id"]

    pn = cs.find("listPhysicalNetworks", "physicalnetwork", zoneid=zid, name="Physical Network 1")
    if not pn:
        pn = cs.run("createPhysicalNetwork", zoneid=zid, name="Physical Network 1", isolationmethods="VLAN", vlan=z["vlan_range"])["physicalnetwork"]
        for tt, label in (("Management", lab.get("management", "cloudbr0")), ("Guest", lab.get("guest", "cloudbr0")), ("Public", lab.get("public", "cloudbr0"))):
            cs.run("addTrafficType", physicalnetworkid=pn["id"], traffictype=tt, kvmnetworklabel=label)
        cs.run("updatePhysicalNetwork", id=pn["id"], state="Enabled"); say("physical network + traffic types enabled")
    for prov in ("VirtualRouter", "VpcVirtualRouter", "InternalLbVm"):
        sp = cs.find("listNetworkServiceProviders", "networkserviceprovider", physicalnetworkid=pn["id"], name=prov)
        if sp and sp["state"] != "Enabled":
            # the provider's element must be enabled first (CloudStack 4.22: "Provider is not ready", errorcode 530)
            lst, conf = ELEMENTS[prov]
            els = next((v for v in cs.run(lst, nspid=sp["id"]).values() if isinstance(v, list)), [])
            for el in els:
                if str(el.get("enabled")).lower() != "true": cs.run(conf, id=el["id"], enabled="true"); say(f"{prov} element enabled")
            cs.run("updateNetworkServiceProvider", id=sp["id"], state="Enabled"); say(f"provider {prov} enabled")

    p = cs.find("listPods", "pod", zoneid=zid, name=pod["name"])
    if not p:
        p = cs.run("createPod", zoneid=zid, name=pod["name"], startip=pod["start"], endip=pod["end"], netmask=pod["netmask"], gateway=pod["gateway"])["pod"]; say(f"created pod {p['id']}")
    if not (cs.run("listVlanIpRanges", zoneid=zid, forvirtualnetwork="true").get("vlaniprange")):
        cs.run("createVlanIpRange", zoneid=zid, vlan=pub.get("vlan", "untagged"), gateway=pub["gateway"], netmask=pub["netmask"],
               startip=pub["start"], endip=pub["end"], forvirtualnetwork="true"); say("public IP range created")

    cl = cs.find("listClusters", "cluster", zoneid=zid, name=spec["cluster"]["name"])
    if not cl:
        cl = cs.run("addCluster", zoneid=zid, podid=p["id"], clustername=spec["cluster"]["name"], clustertype="CloudManaged", hypervisor="KVM")["cluster"][0]; say(f"created cluster {cl['id']}")
    for h in spec.get("kvm_hosts", []):
        if cs.find("listHosts", "host", zoneid=zid, name=h["ip"]) or cs.find("listHosts", "host", zoneid=zid, keyword=h["ip"]): say(f"host {h['ip']} exists"); continue
        cs.run("addHost", zoneid=zid, podid=p["id"], clusterid=cl["id"], hypervisor="KVM",
               username=h.get("user", "root"), password=h["password"], url=f"http://{h['ip']}"); say(f"host {h['ip']} added")

    if spec.get("primary_nfs") and not cs.find("listStoragePools", "storagepool", zoneid=zid, name="primary-nfs"):
        cs.run("createStoragePool", zoneid=zid, podid=p["id"], clusterid=cl["id"], name="primary-nfs", scope="cluster", hypervisor="KVM",
               url=f"nfs://{spec['primary_nfs']}"); say("primary storage added")
    if spec.get("secondary_nfs") and not cs.find("listImageStores", "imagestore", zoneid=zid, name="secondary-nfs"):
        cs.run("addImageStore", zoneid=zid, name="secondary-nfs", provider="NFS", url=f"nfs://{spec['secondary_nfs']}"); say("secondary storage added")

    cs.run("updateConfiguration", name="host", value=spec["management_vip"]); say(f"global setting host = {spec['management_vip']}")
    if zone.get("allocationstate") != "Enabled": cs.run("updateZone", id=zid, allocationstate="Enabled"); say("zone enabled")
    say("DONE")


if __name__ == "__main__":
    if len(sys.argv) != 2: sys.exit(__doc__)
    main(json.load(open(sys.argv[1])))
