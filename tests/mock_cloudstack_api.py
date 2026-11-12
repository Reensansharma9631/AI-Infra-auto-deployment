"""MOCK of the CloudStack API subset used by master/cs-bootstrap.py. Written from the documented API: it verifies the
request signature (HMAC-SHA1 over the sorted, lower-cased query), supports sessionkey login + registerUserKeys,
async jobs (the first poll answers 'pending'), required-parameter checks and parent/child consistency.
It proves OUR client's logic (signing, polling, ordering, idempotency) - NOT that real CloudStack accepts every parameter."""
import time, base64, hashlib, hmac, json, threading, urllib.parse, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

L = lambda s: s.lower()
REQUIRED = {L(k): v for k, v in {
    "createZone": ["name", "networktype", "dns1", "internaldns1"], "createPhysicalNetwork": ["zoneid", "name"],
    "addTrafficType": ["physicalnetworkid", "traffictype"], "updatePhysicalNetwork": ["id"], "updateNetworkServiceProvider": ["id", "state"],
    "createPod": ["zoneid", "name", "startip", "endip", "netmask", "gateway"], "createVlanIpRange": ["zoneid", "gateway", "netmask", "startip", "endip"],
    "addCluster": ["zoneid", "podid", "clustername", "clustertype", "hypervisor"],
    "addHost": ["zoneid", "podid", "clusterid", "hypervisor", "username", "url"],
    "createStoragePool": ["zoneid", "name", "url"], "addImageStore": ["zoneid", "name", "provider", "url"],
    "updateConfiguration": ["name", "value"], "updateZone": ["id"], "listNetworkServiceProviders": ["physicalnetworkid"]}.items()}
ASYNC = {L(c) for c in ("configureVirtualRouterElement", "configureInternalLoadBalancerElement", "createPhysicalNetwork", "addTrafficType", "updatePhysicalNetwork", "updateNetworkServiceProvider",
                         "prepareHostForMaintenance", "cancelHostMaintenance", "startSystemVm", "stopSystemVm", "rebootSystemVm",
                         "destroySystemVm", "enableStorageMaintenance", "cancelStorageMaintenance", "deleteHost",
                         "startVirtualMachine", "stopVirtualMachine", "rebootVirtualMachine")}
REQUIRED.update({L(k): v for k, v in {"createServiceOffering": ["name", "displaytext"], "updatePod": ["id"], "updateCluster": ["id"],
                 "updateHost": ["id"], "deleteZone": ["id"], "deletePod": ["id"], "deleteCluster": ["id"], "deleteHost": ["id"]}.items()})


GENERIC = {"listsystemvms": ("systemvms", "systemvm"), "listtemplates": ("templates", "template"),
           "listserviceofferings": ("offerings", "serviceoffering"), "listvirtualmachines": ("vms", "virtualmachine"),
           "listvolumes": ("volumes", "volume"), "listnetworks": ("networks", "network"), "listrouters": ("routers", "router"),
           "listpublicipaddresses": ("ips", "publicipaddress"), "listdiskofferings": ("offerings", "diskoffering"),
           "listcapacity": ("config", "capacity"), "listalerts": ("config", "alert")}
ACTIONS_MOCK = {"updatepod": ("pods", "allocationstate", "$state"), "updatecluster": ("clusters", "allocationstate", "$state"),
                "updatehost": ("hosts", "allocationstate", "$state"), "deletezone": ("zones", None, None), "deletepod": ("pods", None, None),
                "deletecluster": ("clusters", None, None), "deletehost": ("hosts", None, None),
                "preparehostformaintenance": ("hosts", "resourcestate", "Maintenance"), "cancelhostmaintenance": ("hosts", "resourcestate", "Enabled"),
                "startsystemvm": ("systemvms", "state", "Running"), "stopsystemvm": ("systemvms", "state", "Stopped"),
                "rebootsystemvm": ("systemvms", "state", "Running"), "destroysystemvm": ("systemvms", None, None),
                "enablestoragemaintenance": ("pools", "state", "Maintenance"), "cancelstoragemaintenance": ("pools", "state", "Up"),
                "deletestoragepool": ("pools", None, None), "deleteimagestore": ("stores", None, None),
                "deleteserviceoffering": ("offerings", None, None), "deletetemplate": ("templates", None, None),
                "startvirtualmachine": ("vms", "state", "Running"), "stopvirtualmachine": ("vms", "state", "Stopped"),
                "rebootvirtualmachine": ("vms", "state", "Running")}


class State:
    def __init__(self):
        for n in ("zones", "pns", "providers", "pods", "vlans", "clusters", "hosts", "pools", "stores", "config", "jobs", "sessions", "traffic",
                  "offerings", "systemvms", "templates", "vms", "volumes", "networks", "routers", "ips", "elements"):
            setattr(self, n, {})
        self.api_key, self.secret = uuid.uuid4().hex, uuid.uuid4().hex
        self.key_rotations = 0
        self.host_up_delay = 0.5
        self.config_updates = []
        self.calls, self.polls, self.bad_signatures, self.host_passwords = [], 0, 0, []

    def rotate(self):
        self.api_key, self.secret = uuid.uuid4().hex, uuid.uuid4().hex
        self.key_rotations += 1


def make_server(S, port=0):
    uid = uuid.uuid4().hex
    nid = lambda: str(uuid.uuid4())

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def out(self, key, body, cookies=()):
            b = json.dumps({key: body}).encode(); self.send_response(200); self.send_header("Content-Type", "application/json")
            for c in cookies: self.send_header("Set-Cookie", c)
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        def err(self, cmd, text, code=431): self.out(cmd + "response", {"errorcode": code, "cserrorcode": 9999, "errortext": text})

        def lst(self, key, store, name, q, *flt):
            m = [v for v in store.values() if all(v.get(f) == q[f] for f in flt if f in q)]
            self.out(key, {"count": len(m), name: m} if m else {})

        def do_GET(self): self.go(urllib.parse.urlparse(self.path).query)
        def do_POST(self): self.go(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())

        def go(self, raw):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
            cmd = q.get("command", "").lower(); key = cmd + "response"
            if cmd == "login":
                if q.get("password") != "password": return self.err(cmd, "Unable to find account by name admin", 531)
                sk = uuid.uuid4().hex; S.sessions[sk] = uid
                # like real CloudStack: the login also sets the JSESSIONID + sessionkey cookies
                return self.out("loginresponse", {"sessionkey": sk, "userid": uid, "username": "admin", "timeout": 1800},
                                cookies=(f"JSESSIONID={uuid.uuid4().hex}; Path=/client", f"sessionkey={sk}; Path=/client"))
            if cmd == "registeruserkeys":
                if S.sessions.get(q.get("sessionkey")) != uid: return self.err(cmd, "unauthorized", 401)
                S.rotate()      # like real CloudStack: every registerUserKeys issues a NEW pair, the old one stops working
                return self.out(key, {"userkeys": {"apikey": S.api_key, "secretkey": S.secret}})
            # real CloudStack (verified on 4.22.1.1): a request that carries the session cookie but no sessionkey parameter
            # is rejected even when its API-key signature is valid
            if "JSESSIONID=" in (self.headers.get("Cookie") or "") and "sessionkey" not in q:
                S.bad_signatures += 1; return self.err(cmd, "unable to verify user credentials", 401)
            sig = q.pop("signature", "")
            canon = "&".join(f"{k.lower()}={urllib.parse.quote(v, safe='')}" for k, v in sorted(q.items(), key=lambda kv: kv[0].lower())).lower()
            good = base64.b64encode(hmac.new(S.secret.encode(), canon.encode(), hashlib.sha1).digest()).decode()
            if q.get("apikey") != S.api_key or not hmac.compare_digest(sig, good):
                S.bad_signatures += 1; return self.err(cmd, "unable to verify user credentials and/or request signature", 401)
            S.calls.append(cmd)
            for h in S.hosts.values():
                if h.get("state") == "Connecting" and time.time() >= h.get("_up_at", 0): h["state"] = "Up"
            for r in REQUIRED.get(cmd, []):
                if r not in q: return self.err(cmd, f"Unable to execute API command {cmd} due to missing parameter {r}")
            fn = getattr(self, "c_" + cmd, None)
            if not fn and cmd in GENERIC:
                store, item = GENERIC[cmd]
                fn = lambda qq: self._list(getattr(S, store), item, qq, "zoneid")
            if not fn and cmd in ACTIONS_MOCK:
                store, field, value = ACTIONS_MOCK[cmd]
                def fn(qq, store=store, field=field, value=value):
                    d = getattr(S, store)
                    if qq.get("id") not in d: return "entity not found"
                    if value is None: obj = d.pop(qq["id"]); return {"success": True}
                    d[qq["id"]][field] = value if value != "$state" else qq.get("allocationstate")
                    return {"item": d[qq["id"]]}
            if not fn: return self.err(cmd, f"mock: unsupported command {cmd}", 432)
            res = fn(q)
            if isinstance(res, str): return self.err(cmd, res)
            if cmd in ASYNC:
                jid = uuid.uuid4().hex; S.jobs[jid] = [0, res]; return self.out(key, {"jobid": jid})
            return self.out(key, res)

        # ---- commands: return a dict (success) or a str (error text); list commands return the full body
        def c_queryasyncjobresult(self, q):
            S.polls += 1; j = S.jobs[q["jobid"]]
            if j[0] == 0: j[0] = 1; return {"jobstatus": 0}
            return {"jobstatus": 1, "jobresult": j[1]}

        def _list(self, store, name, q, *flt):
            m = [v for v in store.values() if all(v.get(f) == q[f] for f in flt if f in q)]
            return {"count": len(m), name: m} if m else {}

        def c_listcapabilities(self, q): return {"capability": {"cloudstackversion": "4.22.0.0"}}
        def c_listzones(self, q): return self._list(S.zones, "zone", q, "name")
        def c_createzone(self, q):
            z = {"id": nid(), "name": q["name"], "networktype": q["networktype"], "allocationstate": "Disabled"}; S.zones[z["id"]] = z; return {"zone": z}
        def c_updatezone(self, q): S.zones[q["id"]]["allocationstate"] = q.get("allocationstate", "Enabled"); return {"zone": S.zones[q["id"]]}
        def c_listphysicalnetworks(self, q): return self._list(S.pns, "physicalnetwork", q, "zoneid", "name")
        def c_createphysicalnetwork(self, q):
            if q["zoneid"] not in S.zones: return "zone not found"
            p = {"id": nid(), "name": q["name"], "zoneid": q["zoneid"], "state": "Disabled"}; S.pns[p["id"]] = p
            for n in ("VirtualRouter", "VpcVirtualRouter", "InternalLbVm"):
                sp = {"id": nid(), "name": n, "physicalnetworkid": p["id"], "state": "Disabled"}; S.providers[sp["id"]] = sp
                el = {"id": nid(), "nspid": sp["id"], "enabled": False, "provider": n}; S.elements[el["id"]] = el
            return {"physicalnetwork": p}
        def c_addtraffictype(self, q):
            if q["physicalnetworkid"] not in S.pns: return "physical network not found"
            S.traffic.setdefault(q["physicalnetworkid"], []).append(q["traffictype"]); return {"traffictype": {"traffictype": q["traffictype"]}}
        def c_updatephysicalnetwork(self, q):
            if {"Management", "Guest", "Public"} - set(S.traffic.get(q["id"], [])): return "Management, Guest and Public traffic types are required first"
            S.pns[q["id"]]["state"] = q.get("state", "Enabled"); return {"physicalnetwork": S.pns[q["id"]]}
        def c_listnetworkserviceproviders(self, q): return self._list(S.providers, "networkserviceprovider", q, "physicalnetworkid", "name")
        def c_updatenetworkserviceprovider(self, q):
            # like CloudStack 4.22: a provider with a not-yet-enabled element cannot be enabled
            if q["state"] == "Enabled" and any(e["nspid"] == q["id"] and not e["enabled"] for e in S.elements.values()):
                return "Provider is not ready, cannot Enable the provider, please configure the provider first"
            S.providers[q["id"]]["state"] = q["state"]; return {"networkserviceprovider": S.providers[q["id"]]}

        def _elements(self, q, kind, key):
            m = [e for e in S.elements.values() if e["nspid"] == q.get("nspid") and kind(e["provider"])]
            return {"count": len(m), key: m} if m else {}

        def c_listvirtualrouterelements(self, q): return self._elements(q, lambda p: p in ("VirtualRouter", "VpcVirtualRouter"), "virtualrouterelement")
        def c_listinternalloadbalancerelements(self, q): return self._elements(q, lambda p: p == "InternalLbVm", "internalloadbalancerelement")

        def _conf_el(self, q):
            if q.get("id") not in S.elements: return "entity not found"
            S.elements[q["id"]]["enabled"] = q.get("enabled") == "true"; return {"element": S.elements[q["id"]]}
        def c_configurevirtualrouterelement(self, q): return self._conf_el(q)
        def c_configureinternalloadbalancerelement(self, q): return self._conf_el(q)
        def c_listpods(self, q): return self._list(S.pods, "pod", q, "zoneid", "name")
        def c_createpod(self, q):
            if q["zoneid"] not in S.zones: return "zone not found"
            p = {"id": nid(), "name": q["name"], "zoneid": q["zoneid"]}; S.pods[p["id"]] = p; return {"pod": p}
        def c_listvlanipranges(self, q): return self._list(S.vlans, "vlaniprange", q, "zoneid", "forvirtualnetwork")
        def c_createvlaniprange(self, q):
            v = {"id": nid(), "zoneid": q["zoneid"], "forvirtualnetwork": q.get("forvirtualnetwork")}; S.vlans[v["id"]] = v; return {"vlan": v}
        def c_listclusters(self, q): return self._list(S.clusters, "cluster", q, "zoneid", "name")
        def c_addcluster(self, q):
            p = S.pods.get(q["podid"])
            if not p or p["zoneid"] != q["zoneid"]: return "pod does not belong to the zone"
            c = {"id": nid(), "name": q["clustername"], "zoneid": q["zoneid"], "podid": q["podid"], "hypervisortype": q["hypervisor"]}; S.clusters[c["id"]] = c; return {"cluster": [c]}
        def c_listhosts(self, q):
            m = [h for h in S.hosts.values() if (q.get("zoneid") in (None, h["zoneid"])) and (q.get("keyword") in (None, "") or q["keyword"] in h["name"]) and (q.get("name") in (None, h["name"]))]
            return {"count": len(m), "host": m} if m else {}
        def c_addhost(self, q):
            if q["clusterid"] not in S.clusters: return "cluster not found"
            S.host_passwords.append(q.get("password"))
            h = {"id": nid(), "name": q["url"].replace("http://", ""), "zoneid": q["zoneid"], "clusterid": q["clusterid"], "type": "Routing",
                 # like a real agent: Connecting first, Up a moment later
                 "state": "Connecting" if S.host_up_delay else "Up", "_up_at": time.time() + S.host_up_delay}; S.hosts[h["id"]] = h; return {"host": [h]}
        def c_liststoragepools(self, q): return self._list(S.pools, "storagepool", q, "zoneid", "name")
        def c_createstoragepool(self, q):
            if not q.get("clusterid") or q["clusterid"] not in S.clusters: return "cluster scope needs a valid clusterid"
            if not q["url"].startswith("nfs://"): return "invalid url"
            if not any(h["clusterid"] == q["clusterid"] and h["state"] == "Up" for h in S.hosts.values()):
                return f"Failed to add data store: No host up to associate a storage pool with in cluster {q['clusterid']}"
            p = {"id": nid(), "name": q["name"], "zoneid": q["zoneid"], "path": q["url"]}; S.pools[p["id"]] = p; return {"storagepool": p}
        def c_listimagestores(self, q): return self._list(S.stores, "imagestore", q, "zoneid", "name")
        def c_addimagestore(self, q):
            if not q["url"].startswith("nfs://"): return "invalid url"
            s = {"id": nid(), "name": q["name"], "zoneid": q["zoneid"], "url": q["url"]}; S.stores[s["id"]] = s; return {"imagestore": s}
        def c_createserviceoffering(self, q):
            o = {"id": nid(), "name": q["name"], "displaytext": q["displaytext"], "cpunumber": int(q.get("cpunumber", 1)),
                 "cpuspeed": int(q.get("cpuspeed", 1000)), "memory": int(q.get("memory", 512))}
            S.offerings[o["id"]] = o; return {"serviceoffering": o}
        def c_updateconfiguration(self, q):
            S.config_updates.append(q["name"]); S.config[q["name"]] = q["value"]; return {"configuration": {"name": q["name"], "value": q["value"]}}

        def c_listconfigurations(self, q):      # like CloudStack: 'name' is a substring match
            m = [{"name": k, "value": v} for k, v in S.config.items() if q.get("name", "") in k]
            return {"count": len(m), "configuration": m} if m else {}

    srv = ThreadingHTTPServer(("127.0.0.1", port), H); threading.Thread(target=srv.serve_forever, daemon=True).start(); return srv
