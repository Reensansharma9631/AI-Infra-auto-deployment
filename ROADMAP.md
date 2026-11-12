# ROADMAP.md - what was asked, in order, and when each item counts as done

Rules for every item (owner's requirements):
- **No Docker anywhere** - not on the nodes, not on the master, not for building the offline set (Podman/containerd/native packages).
- Every product: **offline (air-gapped, from the one combined zip) AND online**, deployed **from the UI** (real browser,
  screenshots as evidence), **live-tested on the 4 lab VMs** (172.27.15.238-.241, Ubuntu 24.04 LTS).
- **HA** where it makes sense (2 nodes + keepalived VIP). The LAN has no free IPs: **VIPs live on an internal subnet**
  reachable by the lab machines (decided 2026-11-12).
- "Done" = in STATUS.md under VERIFIED ON REAL LAB, with a FIXES.md entry for every bug found on the way.
- Products take turns on the 4 VMs (32 GB each); a product is stopped/released and restored, never left broken.

| # | Item | Mode / notes | State |
|---|---|---|---|
| 1 | CloudStack 4.22 multi-node, internal network, 2 mgmt, 2 KVM, 4 storages, optional 2nd CPVM | offline proven (egress blocked) | **verified** - online run pending |
| 2 | OpenStack 2026.1 - **Kolla on Podman** | offline + online | in progress |
| 3 | OpenStack 2026.1 - **native Ubuntu packages** (no containers) | offline + online | planned |
| 4 | **Database layout option** in the CloudStack and the OpenStack wizards: (a) the CloudStack/OpenStack server IP(s); (b) the MySQL server their services use - IP, port, user, password, SSH (existing or deployed by AutoInfra); (c) the MySQL **replication nodes ip1, ip2, ip3 ...** - replication must never stay broken: tested by killing the primary, cutting the network, rebooting nodes, filling a disk | offline + online | planned |
| 5 | **OKD** and **Red Hat OpenShift** (CoreOS VM needed; pull secret for OpenShift) | offline (mirror) + online | design first, needs a VM decision |
| 6 | **Rancher** (on RKE2, containerd) | offline + online, HA | planned |
| 7 | **Harbor** (Helm on RKE2 - its own installer needs Docker Compose) | offline + online | planned |
| 8 | **Keycloak** (native) | offline + online, HA | planned |
| 9 | **Argo CD** (on RKE2) | offline + online | planned |
| 10 | **OpenClaw** - standalone AI agent working with Ollama and the other tools | offline + online | planned |
| 11 | **GitLab** (Omnibus) | offline + online | planned |
| 12 | **Zabbix** server + **its MySQL on a different VM** | offline + online | planned |
| 13 | **Metabase** (native) | offline + online | planned |
| 14 | **Ollama** + qwen3:8b, gemma4, gpt-oss:20b, devstral:24b, qwen3-coder:30b, llama4 (67 GB - cannot run on 31 GB RAM, shipped only); knowledge base from this project's troubleshooting; auto-heal | offline + online | planned |
| 15 | **One combined offline zip** (all packages + UI) + clean rebuild proof | - | after the items above |
| 17 | **Version matrix**: CloudStack 4.19, 4.20, 4.21, 4.22 (LTS lines) with packages from **ShapeBlue** or **Apache official** (option per deployment); OpenStack 2025.2 Flamingo, 2026.1 Gazpacho, 2026.2, 2027.1 Indri - each version deployed + tested on the 4 VMs. 2027.1 is still in development: tested as the snapshot that exists, not guaranteed | offline + online | planned |
| 16 | UI test harness: Playwright + Chromium on the master, every deployment driven through the browser | - | planned (before items 6-14) |
