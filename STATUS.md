# STATUS.md - what is proven, and how (updated 2026-11-12)

Lab: master 172.27.15.238 + nodes .239 / .240 / .241, **all Ubuntu 24.04 LTS** (VMware, 1 NIC, nested virtualization on
.240/.241, **no spare IPs** on 172.27.15.0/24). CloudStack 4.22.1.1 from the offline set. Fixes: FIXES.md. Automated suite:
`bash tests/run-all.sh` (last full run 1517 passed, 0 failed) + root-only real tests (`sudo bash tests/run-all.sh`).

Layout deployed through the UI API (the same calls the wizard makes):
- management servers: .239 (cs-mgmt-01) + .241 (nfs-01), no VIP - agent-side load balancing (`host = .239,.241`)
- MySQL 8 single on .239; KVM hosts .240 (kvm-01) + .241 (nfs-01) in Cluster-1
- internal network on the KVM hosts (no spare LAN IPs): pod 10.200.0.0/24, public 10.201.0.0/24, gateway on kvm-01,
  bridge cloudbr1 spans both KVM hosts over VXLAN; guest VLANs 100-200
- storage: primary-nfs + secondary-nfs on .241, primary-2 + secondary-2 on the master .238 (master/setup-nfs.sh)

## VERIFIED ON REAL LAB
- Offline package repository on the master: 4 product zips uploaded through the chunked upload API, extracted, indexed (jobs 11-14).
- Pre-deployment checks on the three nodes (ssh as `ubuntu` with passwordless sudo, key installed with the password once).
- Nodes use **only** the master's apt repositories after `01-common` (`apt-get indextargets` lists only 172.27.15.238).
- Root filesystem growing (xfs): .240 49 -> 299 G, .241 49 -> 149 G.
- **Live KVM bridge switch** (`cloudbr0`) on .240 and on .241: switch, reconnect, confirm.
- Routes unit and NFS server survive a reboot of .241 (vSphere power cycle).
- **CloudStack management server under systemd** (4.22.1.1) on .239 and .241; UI/API `:8080/client` = 200 on both; cluster formed.
- Zone bootstrap through the real API: zone, physical network, providers (with element enablement), pod, public range,
  cluster, **2 KVM hosts Up**, **2 primary pools Up**, **2 secondary stores**, zone Enabled.
- **System VMs**: CPVM + 2 SSVMs Running, agents Up (certificate verification against their real addresses).
- **Guest path, offline**: template registered from `http://172.27.15.238:8081/repo/templates/...`, downloaded by the SSVM;
  isolated network with a virtual router (public 10.201.0.12); **test VM Running with IP 10.1.1.194**; **reachable**: SSH
  banner through a port forward. VM on kvm-01, router on nfs-01: guest VLAN across hosts over VXLAN.
  `master/cs-smoketest.py`: **PASS 9/9**.
- **Re-run safety**: full deployment re-run (job 31) = 0 changed tasks on every host; resume of a failed deployment (jobs 17-22).
- 2 management servers without a VIP (agent LB), restarts only when the `host` setting changes.
- Master as NFS server with packages only from its own AutoInfra repositories (apt history shows the temporary source list).
- Phase 4 log review: every ERROR/WARN type in management-server.log and agent.log classified; the one real air-gap
  issue (internet sample template) fixed; the rest are documented as harmless (FIXES.md).

## VERIFIED BY TESTS ONLY (simulators, rendered configs, network namespaces - not on the lab)
- MySQL HA (GTID + auto-heal, PXC), management VIP with HAProxy + keepalived (configs validated with `keepalived -t`/`haproxy -c`).
- RHEL / Rocky / Alma 9 nodes (vars + rendered templates for the RedHat family; the NetworkManager bridge path vs a stand-in nmcli).
- Internal network: built for real in network namespaces (bridge, dummy/VXLAN, NAT exemptions, 1500-byte frames over VXLAN).
- Root growing on a loop-device xfs disk; LVM path is code-reviewed only.
- OpenStack wizard / Kolla config generation; Zabbix, MongoDB, PostgreSQL, Oracle, Ollama roles (rendered only).

## NOT VERIFIED
- **Deployment with outbound internet blocked on the nodes** (goal 3) - needs your OK to add a temporary egress block.
  Today the nodes still have internet and DNS 8.8.8.8; their apt uses only the master, but nothing else is proven.
- **The one-zip installer** (does not exist yet) and a **clean rebuild from it** (Phase 5).
- DNS service on the master for the zone (zone DNS points at the master, which has no DNS server; system VMs work, names don't resolve).
- The master's own route to the internal subnets (console proxy / public IPs from the master or your desktop).
- The Components page "Management Server" card alone (FIXES #13, known broken).
- Browser e2e suite (Playwright not installed on this master).
- Usage server, SSL (nginx) in front of the management UI, Zabbix monitoring on CloudStack.
- OpenStack, OpenShift, Ollama with models, HA on real machines, RHEL-family nodes on real machines.
- Lab caveat: all three node VMs share one /etc/machine-id (cloned). The automation no longer depends on it (explicit MACs),
  and the pre-deployment check now warns; regenerating it on each node is recommended (needs a reboot - not done).
