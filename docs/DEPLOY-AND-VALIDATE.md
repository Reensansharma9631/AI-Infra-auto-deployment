# Deploy AutoInfra and validate it on your own lab - copy/paste runbook

Every stage ends with a **CHECK**. Go to the next stage only when the check passes. If a check fails, stop. In the UI
open **Deployment Jobs › the failed job › Download log** and send that log, so the failure is fixed from real output,
not guessed.

The real CloudStack/OpenStack steps (Stages 6-7) were **not run** in the build sandbox, because it has no
hypervisor and no access to download.cloudstack.org. Your first run of these stages is the real test, so do it in a
**lab first, not in production**.

## Lab you need (VMs are fine, nested virtualization ON for the KVM host)

| VM | OS | Size | Role |
|---|---|---|---|
| master | Ubuntu 24.04 | 2 vCPU, 4 GB, 120 GB | AutoInfra |
| cs-mgmt-01 | Ubuntu 24.04 | 4 vCPU, 8 GB, 60 GB | management + database (single) + storage (NFS) |
| kvm-01 | Ubuntu 24.04 | 4+ vCPU, 8+ GB, 100 GB, **VT-x passed through** | compute (KVM) |

The examples use one flat network: `10.0.0.0/24`, gateway `10.0.0.1`. Replace the IPs with your own.

---------------------------------------------------------------------------------------------------------------
## Stage 1 - on an INTERNET machine (Ubuntu 24.04 + Docker): build the bundles

```bash
sudo apt-get update && sudo apt-get install -y docker.io zip unzip curl python3-pip git
sudo usermod -aG docker $USER && newgrp docker
unzip airgap-installer.zip -d ~ && cd ~/airgap-cloud-installer

TARGETS=ubuntu bash online/build-autoinfra-master.sh     # -> dist/autoinfra-master-YYYYMMDD.zip  (~100 MB)
bash online/build-cloudstack.sh                          # -> dist/cloudstack-bundle.zip  (CloudStack 4.22 debs, MySQL, KVM, NFS,
                                                         #    HAProxy/keepalived, system VM template; several GB)
ls -lh dist/ && cat dist/*.sha256
```

**CHECK 1:** both zips and their `.sha256` files exist, and `unzip -l dist/cloudstack-bundle.zip | grep cloudstack-management_` shows a package.

Copy `dist/*` to the master (USB or transfer host).

---------------------------------------------------------------------------------------------------------------
## Stage 2 - on the MASTER (offline): install AutoInfra

```bash
cd /path/to/usb && sha256sum -c autoinfra-master-*.zip.sha256
sudo unzip -q autoinfra-master-*.zip -d /opt
sudo MASTER_IP=10.0.0.5 bash /opt/autoinfra/master/install-autoinfra.sh      # prints the admin password ONCE
```

**CHECK 2:**
```bash
systemctl is-active autoinfra nginx                                   # active active
curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1/login     # 200
curl -s  -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/repo/ # 200
cd /opt/autoinfra/ui && sudo -u autoinfra /opt/autoinfra/venv/bin/python -c "import autoinfra, ansible; print('python ok')"
```

Then open `https://10.0.0.5/` in a browser, log in as `admin` and change the password (**Settings › My account**).

---------------------------------------------------------------------------------------------------------------
## Stage 3 - upload the CloudStack bundle

UI: **Package Repository › Upload Packages**. Choose repository **Apache CloudStack**, drop `cloudstack-bundle.zip`
and its `cloudstack-bundle.zip.sha256` together, then wait for "Uploaded · Extracted".

**CHECK 3:**
- **Package Repository › Verification**: product *Apache CloudStack*, *Ubuntu (apt)* shows green for Management Server, Agent, MySQL and System VM template.
- On the master: `curl -s http://10.0.0.5:8081/repo/cloudstack/deb/Packages | grep -c ^Package:` prints more than 100.

---------------------------------------------------------------------------------------------------------------
## Stage 4 - KVM host network (automatic)

CloudStack's KVM agent needs a Linux bridge (`cloudbr0`) that carries the host's IP. **You no longer create it by hand.**
In the wizard, step *Clusters & Hosts*, keep **"Create the KVM bridge automatically on each host"** ticked. During
deployment, for every KVM host, AutoInfra:

1. detects the NIC that carries the host's IP, its prefix, gateway, DNS servers and MAC (job log line
   `kvm-01: 10.0.0.21/24 on ens3 (ethernet), gateway 10.0.0.1, ... -> bridge cloudbr0: needed`);
2. backs up the network config to `/var/lib/autoinfra-bridge/backup` and writes the bridge config
   (netplan on Ubuntu, NetworkManager on RHEL/Rocky/Alma), validated before use;
3. switches over, reconnects, and confirms. **If the master cannot reach the host within 120 s, the host restores the old
   config by itself** - no console visit.

A host that already has `cloudbr0` with its IP is left alone, so hosts prepared by hand keep working.

Limits (the job stops with a clear message instead of guessing):
- supported: IP on an ethernet NIC, a bond or a VLAN device, managed by netplan or NetworkManager;
- one NIC: keep all three traffic labels (Management, Guest, Public) on `cloudbr0` (the default);
- guest/public traffic on a second NIC: Host Management > Edit > Ansible variables: `{"kvm_extra_bridges": {"cloudbr1": "eno2"}}`
  and set the Guest label to `cloudbr1`.

> Not yet proven on real hardware: the detection, the generated netplan config (checked with the real `netplan generate`)
> and the rollback logic are tested; the live switch on a running host is not. **Do the first run on a host where you
> have console access**, and look at `/var/lib/autoinfra-bridge/last.log` on the host if anything is unclear.
> Manual undo at any time: `sudo autoinfra-bridge rollback`.

```bash
ls -l /dev/kvm                   # CHECK on kvm-01: exists (nested virtualization enabled if kvm-01 is a VM)
# after the deployment job:
ip -br addr show cloudbr0        # CHECK: cloudbr0 UP with 10.0.0.21/24
```

Root SSH login with a password must be allowed on the nodes **once**, so the master can install its key
(`PermitRootLogin yes` until Stage 5 is done; afterwards you can set `prohibit-password`).

---------------------------------------------------------------------------------------------------------------
## Stage 5 - add hosts and run the pre-deployment checks

UI: **Pre-Deployment › Bulk Import (CSV)**, then paste:
```
ip,hostname,ssh_user,ssh_port,roles,group,password
10.0.0.20,cs-mgmt-01,root,22,management;database;storage,cloudstack,<root password of cs-mgmt-01>
10.0.0.21,kvm-01,root,22,compute,cloudstack,<root password of kvm-01>
```
Then click **Run All Checks**.

**CHECK 5:** both hosts show **Ready** or **Ready (warnings)**. In **Details**, *OS compatibility*, *Root / sudo access*
and *Master repository reachable* show **Pass**, and on kvm-01 *CPU virtualization (KVM)* shows **Pass**. If kvm-01 shows
Fail for KVM, fix nested virtualization before continuing.

Optional quick smoke test of the deployment engine:
1. **Settings › General › Global Ansible variables**: leave empty.
2. **Components › Nginx (SSL) › Deploy** to cs-mgmt-01.
3. **CHECK:** the job ends with `JOB SUCCESS`, and `curl -kI https://10.0.0.20/` answers. You can then reset the card.

---------------------------------------------------------------------------------------------------------------
## Stage 6 - deploy CloudStack with the wizard (the first REAL run)

UI: **CloudStack › Deployment Wizard**

| Step | Values for this lab |
|---|---|
| Requirements | Offline, 4.22.0, Ubuntu 24.04 LTS; Include database ✓, message queue ✗, monitoring ✗ (no Zabbix packages uploaded) |
| Management Server | select cs-mgmt-01, hostname `cloudstack-mgmt`, VIP empty, network `10.0.0.0/24`, gateway `10.0.0.1`, timezone `Asia/Kolkata`, SSL ✗ (first run), firewall ✗, usage ✗ |
| Database | strategy **Single MySQL 8 server**, host cs-mgmt-01, port 3306 |
| Message Queue | off |
| System VMs | KVM; template: leave empty (CloudStack 4.22 registers the template shipped in the package) or choose the uploaded `systemvmtemplate-4.22.0-x86_64-kvm.qcow2.bz2` |
| Zones & Pods | zone `Zone-1`, DNS1 `10.0.0.1`, internal DNS `10.0.0.1`, guest VLAN `100-200`, guest CIDR `10.1.1.0/24`; pod gateway `10.0.0.1`, netmask `255.255.255.0`, start `10.0.0.100`, end `10.0.0.130`; public gateway `10.0.0.1`, netmask `255.255.255.0`, start `10.0.0.150`, end `10.0.0.180`, VLAN `untagged`; labels: management `cloudbr0`, guest `cloudbr0`, public `cloudbr0` |
| Clusters & Hosts | Cluster-1 = kvm-01; primary `10.0.0.20` `/export/primary`; secondary `10.0.0.20` `/export/secondary`; KVM auth **Management server SSH key** |
| Review & Deploy | admin password: `password` (CloudStack's default on a fresh install) → **Start CloudStack Deployment** |

Note: a public range inside the pod network is allowed only if the two ranges do not overlap. They don't here (100-130 and 150-180).

**CHECK 6 (each one is real CloudStack, not AutoInfra):**
```bash
# on cs-mgmt-01
systemctl is-active cloudstack-management                       # active
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/client/   # 200
# on kvm-01
systemctl is-active cloudstack-agent libvirtd                   # active active
```
1. In the CloudStack UI (`http://10.0.0.20:8080/client`, admin / password), go to **Infrastructure**: Zone-1 is
   *Enabled*, kvm-01 is *Up*, and primary + secondary storage are *Up*.
2. **Infrastructure › System VMs**: the SSVM and CPVM reach **Running** within 10-20 minutes. **This is the real proof
   that the zone works.**
3. In AutoInfra, **CloudStack › Zones & Pods / KVM Hosts / Storage / System VMs** show the same objects through the API.
4. Change the CloudStack admin password afterwards.

If the last job step fails with "missing parameter password" on addHost, your CloudStack version still requires a
password for KVM hosts: set **Clusters & Hosts › KVM host authentication = Root password** and deploy again. The
password is asked for at deploy time and is never stored. Every step is idempotent, so a re-run only does what is missing.

If the system VMs don't start: on cs-mgmt-01 read `/var/log/cloudstack/management/management-server.log`, and on kvm-01
read `/var/log/cloudstack/agent/agent.log`. Send those together with the AutoInfra job log.

User VM templates: upload a qcow2 image to the **Templates & ISOs** repository. Then in CloudStack, **Register
template** from the URL `http://10.0.0.5:8081/repo/templates/files/<file>.qcow2`. The SSVM downloads it from the master.

---------------------------------------------------------------------------------------------------------------
## Stage 7 - HA (after Stage 6 works)

Repeat with:
- 3 database hosts (strategy GTID + auto-heal, VIP `10.0.0.40`);
- 2 management hosts (VIP `10.0.0.25`, which turns on HAProxy + keepalived);
- **Load balancer host(s)**: 1 or 2 hosts that are NOT management servers (the database hosts are fine). HAProxy listens on
  VIP:8080/8250, the same ports CloudStack uses, so the wizard refuses to put both on one machine;
- optionally SSL and Zabbix monitoring.

With a VIP the deployment job also sets the CloudStack global setting `host` to the VIP and restarts the management
servers before any KVM host is added, so agents and system VMs connect through the VIP.

Host firewalls: every role opens the ports it needs when firewalld (default on RHEL/Rocky/Alma) or ufw is active. With ufw
on an Ubuntu NFS server also allow the storage subnet (`ufw allow from 10.0.0.0/24`): the NFSv3 mountd port is dynamic.

**CHECK 7:**
1. `curl http://10.0.0.25:8080/client/` answers.
1a. On a KVM host: `grep ^host= /etc/cloudstack/agent/agent.properties` shows the VIP.
2. Power off the database primary: the VIP moves within ~30 s, and the old primary rejoins by itself when it is powered on again.
3. Power off one management server: the UI is still reachable on the VIP.

---------------------------------------------------------------------------------------------------------------
## Stage 8 - OpenStack (separate lab)

```bash
# on the online machine
bash online/build-openstack.sh                      # dist/openstack-bundle.zip (Kolla images, tens of GB)
# on the master
sudo unzip -q openstack-bundle.zip -d /opt          # -> /opt/airgap/www/openstack
sudo bash /opt/autoinfra/master/install-kolla-tools.sh
sudo bash /opt/autoinfra/master/setup-registry.sh
curl -s http://10.0.0.5:5000/v2/_catalog | head -c 300        # CHECK: kolla images listed
```
Then run **OpenStack › wizard** (1 controller + 1 compute first) → **Start OpenStack Deployment**.

**CHECK 8:** on the master, run:
```bash
sudo bash -c 'export PATH=/opt/airgap/venv-kolla/bin:$PATH; source /var/lib/autoinfra/openstack/etc/admin-openrc.sh \
  && openstack service list && openstack compute service list'     # keystone, nova, neutron... ; nova-compute "up"
```

---------------------------------------------------------------------------------------------------------------
## What to send back when something fails

1. The AutoInfra job log (Deployment Jobs › Download log).
2. The output of the CHECK command that failed.
3. For CloudStack: `management-server.log` / `agent.log` (last 200 lines).
