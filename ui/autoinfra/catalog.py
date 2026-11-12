"""What each product needs: packages per OS family, file artifacts, approximate sizes (for the UI only)."""

REPOS = {
    "cloudstack": "Apache CloudStack", "openstack": "OpenStack (Kolla images + tools)", "kvm": "KVM / Libvirt",
    "databases": "Databases", "monitoring": "Monitoring (Zabbix)", "ollama": "Ollama (AI)", "core": "Core / master tools",
    "os": "Offline OS repository", "templates": "Templates & ISOs", "apps": "Applications", "custom": "Custom",
}

# product -> list of components: id, label, deb names, rpm names, size_mb (approx), kind (pkg|file)
PRODUCTS = {
    "cloudstack": {
        "label": "Apache CloudStack", "versions": ["4.22.0", "4.20.1", "4.19.3"], "repo": "cloudstack",
        "components": [
            {"id": "cloudstack-management", "label": "CloudStack Management Server", "deb": ["cloudstack-management", "cloudstack-common"], "rpm": ["cloudstack-management", "cloudstack-common"], "size_mb": 450},
            {"id": "cloudstack-agent", "label": "CloudStack Agent", "deb": ["cloudstack-agent"], "rpm": ["cloudstack-agent"], "size_mb": 220},
            {"id": "cloudstack-usage", "label": "CloudStack Usage Server", "deb": ["cloudstack-usage"], "rpm": ["cloudstack-usage"], "size_mb": 60},
            {"id": "systemvm-template", "label": "System VM Templates (KVM)", "file": "systemvmtemplate-*-kvm.qcow2.bz2", "size_mb": 1200, "kind": "file"},
            {"id": "mysql", "label": "MySQL 8", "deb": ["mysql-server", "mysql-client"], "rpm": ["mysql-server", "mysql"], "size_mb": 350},
            {"id": "rabbitmq", "label": "RabbitMQ", "deb": ["rabbitmq-server"], "rpm": ["rabbitmq-server"], "size_mb": 120},
            {"id": "nginx", "label": "Nginx", "deb": ["nginx"], "rpm": ["nginx"], "size_mb": 60},
            {"id": "haproxy", "label": "HAProxy + Keepalived", "deb": ["haproxy", "keepalived"], "rpm": ["haproxy", "keepalived"], "size_mb": 15},
            {"id": "python3", "label": "Python3", "deb": ["python3", "python3-apt", "python3-pymysql"], "rpm": ["python3", "python3-PyMySQL"], "size_mb": 90},
            {"id": "nfs", "label": "NFS Utils", "deb": ["nfs-kernel-server", "nfs-common"], "rpm": ["nfs-utils"], "size_mb": 40},
            {"id": "kvm", "label": "KVM / Libvirt", "deb": ["qemu-system-x86", "libvirt-daemon-system", "libvirt-clients", "bridge-utils"], "rpm": ["qemu-kvm", "libvirt", "virt-install"], "size_mb": 180},
            {"id": "zabbix-agent", "label": "Zabbix Agent", "deb": ["zabbix-agent2"], "rpm": ["zabbix-agent2"], "size_mb": 50},
        ]},
    "openstack": {
        "label": "OpenStack", "versions": ["2026.1", "2025.2", "2024.1"], "repo": "openstack",
        "components": [
            {"id": "kolla-ansible", "label": "Kolla-Ansible + Python wheels", "file": "wheels/*.whl", "size_mb": 300, "kind": "file"},
            {"id": "kolla-images", "label": "Kolla container images (registry)", "file": "images/*.tar", "size_mb": 6500, "kind": "file"},
            {"id": "docker", "label": "Docker engine", "deb": ["docker.io"], "rpm": ["docker-ce", "containerd.io"], "size_mb": 120},
            {"id": "mariadb", "label": "MariaDB (in Kolla images)", "kind": "file", "file": "images/*mariadb*.tar", "size_mb": 300},
            {"id": "rabbitmq", "label": "RabbitMQ (in Kolla images)", "kind": "file", "file": "images/*rabbitmq*.tar", "size_mb": 120},
            {"id": "python3", "label": "Python3 + venv", "deb": ["python3-venv", "python3-pip"], "rpm": ["python3", "python3-pip"], "size_mb": 90},
            {"id": "lvm2", "label": "LVM2 (Cinder)", "deb": ["lvm2", "thin-provisioning-tools"], "rpm": ["lvm2", "device-mapper-persistent-data"], "size_mb": 20},
        ]},
    "kvm": {
        "label": "KVM / Libvirt", "versions": ["distro"], "repo": "kvm",
        "components": [
            {"id": "qemu", "label": "QEMU/KVM", "deb": ["qemu-system-x86", "qemu-utils", "ovmf", "cpu-checker"], "rpm": ["qemu-kvm", "qemu-img", "edk2-ovmf"], "size_mb": 140},
            {"id": "libvirt", "label": "Libvirt", "deb": ["libvirt-daemon-system", "libvirt-clients", "virtinst"], "rpm": ["libvirt", "virt-install"], "size_mb": 60},
            {"id": "bridge", "label": "Bridge/VLAN/OVS tools", "deb": ["bridge-utils", "vlan", "openvswitch-switch"], "rpm": ["openvswitch3.1", "NetworkManager-ovs"], "size_mb": 30},
            {"id": "netfilter", "label": "iptables (internal network NAT)", "deb": ["iptables"], "rpm": ["iptables-nft"], "size_mb": 3},
        ]},
    "databases": {
        "label": "Databases", "versions": ["8.0", "8.4"], "repo": "databases",
        "components": [
            {"id": "mysql", "label": "MySQL 8 (+ HA agent deps)", "deb": ["mysql-server", "mysql-client", "python3-pymysql", "python3-cryptography", "keepalived"],
             "rpm": ["mysql-server", "mysql", "python3-PyMySQL", "python3-cryptography", "keepalived"], "size_mb": 350},
            {"id": "pxc", "label": "Percona XtraDB Cluster 8", "deb": ["percona-xtradb-cluster"], "rpm": ["percona-xtradb-cluster"], "size_mb": 400},
            {"id": "postgresql", "label": "PostgreSQL 16", "deb": ["postgresql", "postgresql-contrib", "python3-psycopg2"], "rpm": ["postgresql-server", "postgresql-contrib", "python3-psycopg2"], "size_mb": 70},
            {"id": "mongodb", "label": "MongoDB 7", "deb": ["mongodb-org"], "rpm": ["mongodb-org"], "size_mb": 200},
            {"id": "oracle-prereq", "label": "Oracle 19c prerequisites (installer not redistributable)", "deb": ["libaio1t64", "unzip", "ksh", "bc", "binutils", "make", "sysstat"], "rpm": ["oracle-database-preinstall-19c", "unzip"], "size_mb": 20},
        ]},
    "monitoring": {
        "label": "Monitoring", "versions": ["7.0"], "repo": "monitoring",
        "components": [
            {"id": "zabbix-server", "label": "Zabbix server + frontend", "deb": ["zabbix-server-mysql", "zabbix-frontend-php", "zabbix-nginx-conf", "zabbix-sql-scripts", "zabbix-agent2", "mysql-server", "php8.3-fpm"],
             "rpm": ["zabbix-server-mysql", "zabbix-web-mysql", "zabbix-nginx-conf", "zabbix-sql-scripts", "zabbix-agent2", "mysql-server", "php-fpm"], "size_mb": 160},
            {"id": "zabbix-agent", "label": "Zabbix agent 2", "deb": ["zabbix-agent2"], "rpm": ["zabbix-agent2"], "size_mb": 50},
        ]},
    "core": {
        "label": "Node base packages (every node)", "versions": ["distro"], "repo": "core",
        "components": [
            {"id": "base", "label": "chrony, curl, jq, rsync, ethtool, python3", "deb": ["chrony", "python3-apt", "curl", "jq", "rsync", "ethtool", "ca-certificates", "iproute2"],
             "rpm": ["chrony", "curl", "jq", "rsync", "ethtool", "ca-certificates", "python3", "iproute"], "size_mb": 40},
            {"id": "tools", "label": "openssl, procps, NFS client", "deb": ["openssl", "procps", "nfs-common"], "rpm": ["openssl", "procps-ng", "nfs-utils"], "size_mb": 15},
            {"id": "disk", "label": "growpart, xfs/ext4 tools (grow the root disk)", "deb": ["cloud-guest-utils", "xfsprogs", "e2fsprogs"],
             "rpm": ["cloud-utils-growpart", "xfsprogs", "e2fsprogs"], "size_mb": 6},
        ]},
    "ollama": {
        "label": "Ollama (AI)", "versions": ["0.3"], "repo": "ollama",
        "components": [
            {"id": "ollama-bin", "label": "Ollama binary (linux-amd64)", "file": "ollama-linux-amd64.tgz", "size_mb": 1600, "kind": "file"},
            {"id": "model", "label": "Model blobs (e.g. llama3.1:8b)", "file": "models/*", "size_mb": 4700, "kind": "file"},
        ]},
}


# OS base packages every node needs (put in the 'core' product kit, see PRODUCTS['core'])
BASE_EXTRA = {"deb": [], "rpm": []}


def packages_for(product, family):
    p = PRODUCTS[product]
    out = []
    for c in p["components"]:
        if c.get("kind") == "file":
            continue
        out += c.get("rpm" if family == "rhel" else "deb", [])
    return out


def required(product, family):
    """[{component, label, names|file, kind, size_mb}] used by the wizards' 'Required packages' panels."""
    p = PRODUCTS[product]
    out = []
    for c in p["components"]:
        if c.get("kind") == "file":
            out.append({"id": c["id"], "label": c["label"], "kind": "file", "pattern": c["file"], "size_mb": c["size_mb"]})
        else:
            out.append({"id": c["id"], "label": c["label"], "kind": "pkg", "names": c.get("rpm" if family == "rhel" else "deb", []),
                        "size_mb": c["size_mb"]})
    return out
