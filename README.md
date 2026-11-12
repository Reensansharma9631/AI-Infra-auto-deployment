# AutoInfra – air-gapped Apache CloudStack / OpenStack deployment, management and monitoring

AutoInfra is a web UI that runs on one **master automation machine** inside the air-gapped network. You upload offline
package bundles to it, add your nodes (IP, user, password), and it deploys and manages everything over SSH with Ansible:
CloudStack, OpenStack (Kolla), MySQL HA, PostgreSQL, MongoDB, Oracle prerequisites, RabbitMQ, Nginx, HAProxy/keepalived,
NFS, KVM, Zabbix and Ollama. It also includes monitoring, an Ollama AI assistant, PDF/Excel reports and billing.

- **Master OS:** Ubuntu 24.04, or RHEL / Rocky / AlmaLinux 9 (needs `python3.12`).
- **Node OS:** Ubuntu 24.04 / 22.04, or RHEL / Rocky / Alma / Oracle Linux 9 / 8.
- **Nothing is downloaded at install or deploy time.**

Screenshots of every page, taken on a master installed with these steps, are in `docs/screenshots/`.

**First deployment: follow `docs/DEPLOY-AND-VALIDATE.md`.** It is a lab runbook in which every stage ends with a
CHECK command, so the parts that could not be run in the build sandbox (a real CloudStack zone, a real OpenStack) are
proven on your hardware before production.

---------------------------------------------------------------------------------------------------------------------

## 0. What was tested, and how

| Level | What ran | Result |
|---|---|---|
| Unit / API / Ansible content | `pytest`: every API route × every role, CSRF on every write, input validation, upload pipeline on real `.deb`/`.rpm` files, CloudStack wizard + API against a CloudStack API simulator, OpenStack config generation, 16 components × 2 OS families, monitoring, AI (Ollama simulator), PDF/Excel, billing, settings. Also every Ansible template rendered by real Ansible for **Ubuntu and RHEL** facts and checked with `haproxy -c`, `keepalived -t` and `nginx -t`. | **1503 passed**, 5 skipped (mandatory services cannot be switched off) |
| Browser end-to-end | Real Chromium (Playwright) driving every page: add host, CSV import, live check log, upload through the file chooser, download kit, CloudStack wizard with validation errors, add zone, deploy a component, OpenStack preview, AI chat, report download, settings, users, viewer role, phone layout. Every page loads with no JavaScript errors under a strict CSP. | **58 passed** |
| Real two-OS lab | Real **Ubuntu 24.04** and **AlmaLinux 9.3** nodes with **no internet**. Everything goes through the API: a real 150 MB offline bundle (127 Ubuntu packages) is uploaded in chunks and indexed; hosts added by password; real SSH checks; then **real `ansible-playbook`** runs `common` on both OSes (dnf and apt switched to the master repo only). MySQL 8, PostgreSQL 16, Nginx+TLS, NFS, RabbitMQ, HAProxy+keepalived and KVM/libvirt are deployed on Ubuntu, verified on the node, and Nginx is checked live. The KVM packages came from a bundle produced by the UI's own **download kit** (168 packages). Second runs report `changed=0`. | **37/37** – `docs/test-reports/lab-e2e-ubuntu24-alma9.log` |
| Master installer | `install-autoinfra.sh` on a **fresh offline Ubuntu 24.04** from the master bundle. Then the *installed* service (nginx TLS → gunicorn as user `autoinfra`) uploads 150 MB through nginx, bootstraps a node by password, runs real Ansible and deploys Nginx. | **8/8** – `docs/test-reports/installed-master-ubuntu24.log`, `install-autoinfra-ubuntu24-offline.log` |

**Not tested here, and why** (do these on your first lab run):

1. **A real CloudStack or OpenStack cloud.** That needs real hypervisors and the multi-GB CloudStack/Kolla packages. The wizards were tested against a CloudStack API simulator and a stand-in `kolla-ansible`; the Ansible roles were syntax-checked and rendered.
2. **Services starting under systemd.** The lab nodes have no systemd, so role steps that start services ran with `autoinfra_services=false`. Configs were validated with the real tools, and Nginx was started by hand and answered.
3. **Distribution RPMs on RHEL nodes.** AlmaLinux mirrors are not reachable from this sandbox. On AlmaLinux, the repository switch and an RPM install from the master were tested (the same code path dnf uses for every package); MySQL/Nginx/etc. RPMs were not installed there.
4. **The master installer on RHEL 9.** The script supports it (dnf + SELinux contexts + firewalld) but it ran only on Ubuntu 24.04.
5. **Zabbix server, MongoDB, Oracle, Ollama roles, and Percona XtraDB Cluster.** These were syntax-checked and their templates rendered for both OS families, but not installed (their packages come from vendor repositories you add to the bundle).
6. **The Phase 1–2 integration scripts** (`dbha_cluster_test.py`, `role_netns_test.py`, `cs_lb_mgmt_netns_test.py`) were **not re-run** after the roles became OS-aware.

### Production readiness - read this before the first real deployment

**Status: ready for a lab/pilot run, not yet proven for production.** The UI, package repository, host checks, job engine and
the Ubuntu node path are tested for real. No real CloudStack or OpenStack cloud has ever been built with this code. Run
`docs/DEPLOY-AND-VALIDATE.md` on spare hardware first; go to production only after every CHECK there passes.

A code audit of the roles that could never run in the sandbox found and fixed these defects (each has a regression test;
items marked * are covered only by simulators or code review, because they need a real CloudStack):

| # | Defect | Fix |
|---|---|---|
| 1 | CloudStack keys were written to `/var/lib/airgap-ui/cs-keys`, which the service user cannot write - the management role would fail | keys go to `<data dir>/cs-keys` |
| 2 | MySQL HA delegated to the primary by IP (not an inventory name); `mysqld` path was wrong on RHEL (`/usr/libexec/mysqld`) | delegate by inventory name; path per OS family |
| 3 | HAProxy was placed on the management servers, where CloudStack already owns ports 8080/8250 | the wizard asks for separate load balancer hosts (DB hosts are fine) and refuses a clash |
| 4* | Management SSH public key looked up in one fixed path | home of user `cloud` plus both known paths; warning if none |
| 5* | With a VIP, global setting `host` was set after the KVM hosts were added and without a restart | set before hosts are added, management servers restarted one by one |
| 6* | API login had no retry (a new management server needs minutes) and ignored the session cookie real CloudStack requires | retry up to 15 min (not on a wrong password), cookie kept |
| 7 | The wizard ran `hosts: all` plays on every machine in the inventory | only hosts chosen in the wizard are in the job inventory |
| 8 | Roles did not open firewall ports (firewalld is active by default on RHEL family) | every role opens its ports when firewalld or ufw is active |

Known limits that remain: SSH host keys are not verified (`host_key_checking=False`); job work directories keep a
`vars.json` (mode 0600) with generated passwords; interrupted uploads leave `.part` files until removed by hand.

---------------------------------------------------------------------------------------------------------------------

## 1. Screens (matching the mockups)

| Mockup screen | Page in AutoInfra |
|---|---|
| Package Management / Repository & Upload (Upload, Online Download, Repository, Extraction Jobs, Verification, Disk Usage) | **Package Repository** `/packages` |
| Pre-Deployment Checks & Host Preparation (Add Hosts → Connectivity → System Checks → Update & Upgrade → Package Validation → Ready) | **Pre-Deployment** `/predeploy`, **Host Management** `/hosts` |
| Deploy Apache CloudStack – Full Deployment Wizard (Requirements, Management Server, Database, Message Queue, System VMs, Zones & Pods, Clusters & Hosts, Review & Deploy) | **CloudStack › Deployment Wizard** `/cloudstack/wizard` |
| Zones, Pods, Clusters, KVM Hosts, Storage, Networks, System VMs, Templates, Service Offerings | **CloudStack › Zones & Pods …** `/cloudstack/infra` (live CloudStack API) |
| Component Deployment & Management (Management Server, Usage, MySQL, PostgreSQL, Oracle, MongoDB, RabbitMQ, Nginx, HAProxy, KVM, Zabbix, Ollama …) | **Components** `/components` |
| Deploy OpenStack Infrastructure (Controller/Compute/Storage nodes, services, network & storage, deploy & validate) | **OpenStack** `/openstack/wizard` |
| Monitoring, Logs & AI Insights (tiles, topology, gauges, alerts, logs, Zabbix, Ollama assistant) | **Monitoring** `/monitoring`, **AI Insights** `/ai` |
| Reports (PDF / Excel), Billing & Cost, Audit Logs, Settings | `/reports`, `/billing`, `/audit`, `/settings` |

Brand logos (MySQL dolphin, etc.) are replaced by neutral icons.

---------------------------------------------------------------------------------------------------------------------

## 2. How it fits together

```
 INTERNET SIDE (once)                         AIR-GAPPED NETWORK
 ┌──────────────────────────┐   USB/transfer  ┌──────────────────────────────────────────────┐
 │ online/build-autoinfra-  │ ──────────────► │ MASTER  (Ubuntu 24.04 or RHEL 9)             │
 │   master.sh → master zip │                 │  nginx :443  → AutoInfra (gunicorn, 1 worker)│
 │ download kits (.sh) from │                 │  nginx :8081 → /repo/<repo>/{deb,rpm,files}  │
 │   the UI → product zips  │                 │  ansible-playbook  ── SSH (key) ──┐          │
 └──────────────────────────┘                 │  chrony (NTP for all nodes)       │          │
                                              └───────────────────────────────────┼──────────┘
                                         ┌────────────────────┬───────────────────┴───────┐
                                      Ubuntu / RHEL nodes: mgmt, db (HA + VIP), kvm, nfs, lb, mq, ...
                                      (apt/dnf point ONLY at http://MASTER:8081/repo/...)
```

Data lives on the master in `/var/lib/autoinfra`: the SQLite DB, repositories, job logs, reports, the SSH key, and
`secrets.json` (generated service passwords, mode 0600). Node passwords are used **once** to install the master's
SSH key and are **never stored**.

---------------------------------------------------------------------------------------------------------------------

## 3. On an INTERNET-connected machine: build the bundles

Use a throw-away Ubuntu 24.04 VM with Docker, internet, and about 20 GB of free disk.

```bash
# 3.1  get the code (this zip) onto the online machine
unzip airgap-installer.zip -d ~/ && cd ~/airgap-cloud-installer

# 3.2  tools
sudo apt-get update && sudo apt-get install -y zip unzip curl python3-pip docker.io
sudo usermod -aG docker $USER && newgrp docker

# 3.3  MASTER bundle = AutoInfra code + Python 3.12 wheels + the master's own OS packages
#      TARGETS="ubuntu" for an Ubuntu master, "rhel" for a RHEL/Rocky/Alma 9 master, both by default
TARGETS="ubuntu rhel" bash online/build-autoinfra-master.sh
ls -lh dist/            # autoinfra-master-YYYYMMDD.zip + .sha256
```

**Everything in one file, without Docker.** Nothing in the CloudStack path needs Docker, online or offline. On an
Ubuntu 24.04 online machine (same OS as the master and nodes):

```bash
sudo apt-get install -y zip unzip curl python3-pip
sudo NO_DOCKER=1 bash online/build-all.sh          # -> dist/autoinfra-offline-<date>.zip
# options: PRODUCTS="core cloudstack kvm databases monitoring"  NODE_OS=ubuntu-24.04  CLOUDSTACK_VERSION=4.22.0
```

The one zip holds the master bundle, one package zip per product, `SHA256SUMS`, `README-OFFLINE.txt` (the steps on the
master) and `MISSING.txt`. The script ends with `complete: nothing missing`, or exit code 2 and the list of what could
not be downloaded (vendor packages such as Percona XtraDB Cluster or MongoDB need their vendor repository added on the
online machine first). Packages are resolved against an empty package database, so **every** package is downloaded, also
the basic ones already installed on the online machine. The machine's apt sources are restored afterwards.

Tested for real on a used Ubuntu 24.04 without Docker: master bundle (156 packages), core, kvm and databases kits, the
one-file zip, and an offline master install from that bundle. Not tested: the CloudStack kit (download.cloudstack.org was
not reachable from the test environment) and RHEL (needs a RHEL/Rocky/Alma 9 online machine). **OpenStack is the
exception:** Kolla runs OpenStack in containers, so Docker is required on OpenStack nodes and to download its images.

**Product bundles.** Choose one way:

- **(a) Download kits.** On the air-gapped master, open **Package Repository › Online Download**. Choose the product
  (CloudStack / OpenStack / KVM / Databases / Monitoring / Ollama), the version and the node OS (Ubuntu 24.04 or
  Rocky/Alma 9), then click **Download kit (.sh)**. Carry the `.sh` to the online machine and run it:
  ```bash
  bash download-cloudstack.sh          # uses docker ubuntu:24.04 / rockylinux:9; creates cloudstack-<ver>-<os>-packages.zip
  ```
- **(b) The existing builders.** These produce the same `airgap/www/<repo>/...` layout:
  ```bash
  bash online/build-cloudstack.sh      # CloudStack + MySQL + KVM + HAProxy/keepalived + NFS debs + system VM template
  bash online/build-openstack.sh       # Kolla images + kolla-ansible wheels (tens of GB)
  ```

**Vendor packages.** These are not in the OS repositories: Zabbix 7 (`repo.zabbix.com`), MongoDB (`repo.mongodb.org`),
Percona XtraDB Cluster (`repo.percona.com`), the Ollama binary and model blobs, and the Oracle 19c installer (licensed,
you supply it). Download them on the online machine, put them in a zip, and upload it to the matching repository
(`monitoring`, `databases`, `ollama`). Ollama expects `ollama-linux-amd64.tgz`, plus optionally `ollama-models.tar`
containing the `models/` directory.

Copy everything in `dist/`, the kit zips and their `.sha256` files to the master (USB or transfer host).

---------------------------------------------------------------------------------------------------------------------

## Quick start: run it directly on 0.0.0.0 (no nginx, no systemd, no root)

For trying it out or a lab. The production install is section 4 (TLS, service user, starts at boot).

```bash
# from the source zip (internet: Python packages come from PyPI)
unzip airgap-installer.zip -d ~ && cd ~/airgap-cloud-installer
bash run-local.sh                         # -> http://<this-ip>:8000/ ; prints admin password on first start

# from the master bundle (offline: uses ./wheels)
sudo unzip -q autoinfra-master-*.zip -d /opt && cd /opt/autoinfra && bash run-local.sh

# options
PORT=9000 bash run-local.sh               # other port
MASTER_IP=10.0.0.5 bash run-local.sh      # IP the nodes use to reach this machine (default: first IP of `hostname -I`)
TLS=1 PORT=8443 bash run-local.sh         # https with a self-signed certificate
HOST=127.0.0.1 bash run-local.sh          # only this machine
```

- It listens on `0.0.0.0:8000` and also serves the package repository to the nodes at `http://MASTER_IP:8000/repo/`, so
  there is no separate :8081.
- Data goes to `./.data` (DB, repositories, job logs, SSH key). Stop it with Ctrl+C; the next start keeps everything.
- Open the port if a firewall is on: `sudo ufw allow 8000/tcp`, or
  `sudo firewall-cmd --add-port=8000/tcp --permanent && sudo firewall-cmd --reload`.
- Plain HTTP sends the login password unencrypted, so use it only on a trusted network, or with `TLS=1`.
- Ansible needs Python on this machine (3.10+, e.g. `python3.12` on RHEL 9). On Ubuntu: `sudo apt-get install -y python3-venv`.

Tested: started from a clean copy with the offline wheels, bound to 0.0.0.0:8000. It was reached from another
machine, took a 150 MB upload, served `/repo/` to the node, bootstrapped the node by password, and ran real Ansible
(`01-common.yml` and the Nginx component): 8/8. `TLS=1` also answered on https.

---------------------------------------------------------------------------------------------------------------------

## 4. On the AIR-GAPPED master: install AutoInfra

The master needs a fixed IP, at least 2 vCPU / 4 GB RAM, and 60 GB+ disk (plus the size of your bundles).

```bash
# 4.1  verify and unpack (creates /opt/autoinfra)
cd /path/to/usb
sha256sum -c autoinfra-master-*.zip.sha256
sudo unzip -q autoinfra-master-*.zip -d /opt

# 4.2  install (Ubuntu 24.04 or RHEL/Rocky/Alma 9) - offline, takes ~1-2 minutes
sudo MASTER_IP=10.0.0.5 bash /opt/autoinfra/master/install-autoinfra.sh
#   optional: ADMIN_PASSWORD='...'  UI_PORT=443  REPO_PORT=8081  NTP_ALLOW=10.0.0.0/16
#   the script prints:  UI https://10.0.0.5/   admin / <password shown ONCE>

# 4.3  check
systemctl status autoinfra nginx --no-pager
curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1/login      # 200
```

What the installer does:

- installs the master packages from the bundle only (all online sources are ignored);
- creates user `autoinfra` and `/var/lib/autoinfra`, and builds `/opt/autoinfra/venv` from the wheelhouse (`--no-index`);
- writes `/etc/autoinfra/autoinfra.env` (secret key, admin password hash) and a self-signed TLS certificate;
- configures nginx (`:443` UI, `:8081/repo/` repositories), SELinux contexts on RHEL, ufw/firewalld ports and chrony;
- creates the `autoinfra.service` systemd unit.

Re-running the script is safe: it keeps the admin password and data, and is the way to apply an updated bundle.

RHEL 9 master: `python3.12` must be in the bundle (`TARGETS=rhel`); it is in AppStream since 9.4.

---------------------------------------------------------------------------------------------------------------------

## 5. Use the UI (step by step)

Open `https://MASTER_IP/` and log in as `admin`. Change the password at **Settings › My account**.

**5.1 Settings › General.** Check the master IP and the repository URL (default `http://MASTER_IP:8081/repo`).
**Settings › Users:** add users with the role *viewer* (read only), *operator* (deploy) or *admin* (everything).

**5.2 Package Repository › Upload Packages.** Choose the target repository and drag in your zip/tar/iso/deb/rpm files
(put `<file>.sha256` in the same selection to have it verified). Uploads go in 8 MB chunks, and an interrupted upload
resumes where it stopped. Each upload then runs: **SHA256 verify → safe extract** (path traversal, links and devices
are refused) → sort into `deb/ rpm/ wheels/ files/` → **apt `Packages` + dnf `repodata`** → dependency scan.
- **Verification** shows, per product and OS family, what is present or missing, and re-checks the SHA256 of every stored file.
- **Disk Usage** shows the free space.

**5.3 Pre-Deployment.**
- **Add Host**, or **Bulk Import (CSV)** with the header `ip,hostname,ssh_user,ssh_port,roles,group,password`. Separate
  roles with `;`. The roles are management, database, compute, storage, lb, mq, monitoring, controller, network,
  os-compute, os-storage, ai and app. Passwords are used once and never stored.
- **Run All Checks** (live log) tests: ping, SSH, root/sudo, OS compatibility, CPU/RAM/disk, KVM, clock skew, package
  manager, master repo reachable, updates, Python, and security. Thresholds are in **Settings**.
- **Update & Upgrade** runs apt or dnf against the master repo. **Required Packages** shows per role what is missing in the repo.
- Hosts with a non-root user need passwordless sudo.

**5.4 CloudStack › Deployment Wizard.** Fill in the 8 steps; each is validated on the server:
- the DB strategy: **GTID + semi-sync + auto-heal agent** (2-3 nodes, VIP), **Percona XtraDB Cluster** (3 nodes) or **single**;
- RabbitMQ (optional), the system VM template from the repository, the zone/pod/public ranges and VLANs;
- the clusters with their KVM hosts, and primary/secondary NFS.

**Review & Deploy** shows the plan and runs it as one job: preflight → common → DB → management → LB/VIP → Nginx TLS →
usage → NFS → KVM agent (the management server's SSH key is installed on the KVM hosts, so no root password is needed)
→ system VM template → zone creation through the CloudStack API, which also saves the API keys. Afterwards, **CloudStack › Zones & Pods …**
manages zones, pods, clusters, hosts, storage, networks, system VMs, templates and offerings live.

**5.5 Components.** Each card: **Configure** (validated settings) → choose hosts → **Deploy** (the Ansible job with live log;
**Dry run** = `--check --diff`) → **Check status** (systemd state over SSH). Runs are idempotent: re-deploying applies
configuration changes only.

**5.6 OpenStack.** Choose controllers (1 or 3), compute/storage/network nodes, services, interfaces, VIP and storage backend.
**Deploy** writes `globals.yml`, `multinode` and `passwords.yml` (generated once, 0600) and runs `kolla-ansible`
bootstrap-servers → prechecks → pull (from the master registry, `master/setup-registry.sh`) → deploy → post-deploy.
Install kolla-ansible on the master first with `master/install-kolla-tools.sh`, from the OpenStack bundle.

**5.7 Monitoring.**
- Metrics are collected over SSH every `monitor_interval` seconds (default 300).
- Alerts follow the thresholds and also cover host down, failed deployments and degraded components.
- Also here: the topology, logs & events timeline, and Zabbix problems (set the Zabbix URL and API token in Settings).

**5.8 AI Insights (Ollama).** Deploy **Ollama AI Platform** (or use an existing Ollama), then set **Settings › Ollama URL**.
The assistant answers using live hosts, metrics, alerts, components, failed job logs and your **Knowledge Base**
documents. Asking for a "PDF report" or "Excel report" generates one.

**5.9 Reports / Billing.**
- Reports: PDF and Excel (infrastructure, hosts & checks, cost, audit).
- Billing: a rate card (per vCPU-hour, GB-RAM-hour, GB-month, public IP, host-month) applied to CloudStack usage per account. Export as CSV or Excel.

**5.10 Advanced.**
- **Host Management › Edit › Ansible variables** (admin) sets per-host variables, e.g. `{"kvm_extra_bridges": {"cloudbr1": "eno2"}}`.
- **KVM bridge is automatic**: the wizard creates `cloudbr0` on each KVM host (NIC, IP, gateway, DNS detected; automatic rollback
  if the host becomes unreachable). Tested: detection, config generation with the real `netplan generate`, rollback logic.
  Not tested: the live switch on real hardware and the NetworkManager (RHEL) path, which ran only against a stand-in `nmcli`.
- **Settings › Global Ansible variables** applies to every run.
- **Deployment Jobs** keeps every job's full log (download or cancel).

---------------------------------------------------------------------------------------------------------------------

## 6. Operations

```bash
journalctl -u autoinfra -f                 # application log
ls /var/lib/autoinfra/jobs/<id>/log        # any job's full output
# backup (DB, repos, keys, generated passwords)
sudo systemctl stop autoinfra && sudo tar -C /var/lib -czf /root/autoinfra-backup-$(date +%F).tgz autoinfra /etc/autoinfra \
  && sudo systemctl start autoinfra
# upgrade: unzip the new master bundle over /opt/autoinfra and re-run install-autoinfra.sh (data is kept)
```

Troubleshooting:

- **A host is "Auth failed":** add it again once with its password, or append the key from **Host Management › Master SSH key** to `~/.ssh/authorized_keys`.
- **apt or dnf fails on a node:** the needed packages are not in the master repo. Check **Verification**.
- **Package repo unreachable from nodes:** allow TCP 8081 to the master. On RHEL also run `semanage port -l | grep 8081`.

---------------------------------------------------------------------------------------------------------------------

## 7. Run the tests yourself

```bash
cd /opt/autoinfra          # or the source tree
sudo apt-get install -y haproxy keepalived nginx rpm dpkg-dev nodejs     # validators used by the tests
/opt/autoinfra/venv/bin/pip install --no-index --find-links wheels -r ui/requirements.txt   # or online: pip install -r ui/requirements-test.txt
bash tests/run-all.sh          # ~1400 unit/API/Ansible tests
bash tests/run-all.sh e2e      # + 58 browser tests (needs: pip install playwright && playwright install chromium)
sudo bash tests/run-all.sh lab # + real Ubuntu 24.04 + AlmaLinux 9 lab (see tests/integration/os_lab.sh header)
```

---------------------------------------------------------------------------------------------------------------------

## 8. Security notes

- Every write needs a CSRF token. Roles are enforced on every API (tested route by route).
- Logins are throttled (5 failures per 5 minutes per IP). Every action goes to an audit log, with no passwords in it.
- Pages run under a strict CSP (no inline scripts). Cookies are HttpOnly, Secure and SameSite=Lax.
- Node passwords are never stored. Generated service passwords stay in `/var/lib/autoinfra/secrets.json` (0600, user autoinfra), and job logs redact them.
- Replace the self-signed UI certificate with your CA's: `/etc/autoinfra/tls/ui.crt` and `ui.key`.
