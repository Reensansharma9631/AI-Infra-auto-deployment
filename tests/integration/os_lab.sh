#!/usr/bin/env bash
# Two real OS nodes for integration tests, with NO internet (only the lab bridge):
#   10.79.0.21  AlmaLinux 9.3   rootfs /srv/alma9 - host sshd (in netns 'al1') chroots every session into it
#   10.79.0.22  Ubuntu 24.04    rootfs /srv/ubu24 - the node's own sshd runs chrooted in netns 'ub1'
#   10.79.0.1   the "master" (this machine) on bridge br-lab
# usage: sudo bash os_lab.sh up|down|snapshot|reset          (no systemd inside the nodes - services are not started)
set -Eeuo pipefail
ALMA=/srv/alma9 UBU=/srv/ubu24 PRISTINE=/srv/pristine
UBU_PW="${UBU_PW:-LabRootPw-2026}"
KEYDIR=/etc/ssh/autoinfra_keys

mounts() { local r=$1; for d in proc sys dev dev/pts; do mountpoint -q "$r/$d" || mount --bind "/$d" "$r/$d"; done
  mountpoint -q "$r/dev/shm" || mount -t tmpfs -o mode=1777 tmpfs "$r/dev/shm"; }   # a bind of /dev does not carry /dev/shm
umounts() { local r=$1; for d in dev/shm dev/pts dev sys proc; do mountpoint -q "$r/$d" && umount -l "$r/$d" || true; done; }

down() {
  for ns in al1 ub1; do
    if ip netns list | grep -qw $ns; then
      for p in $(ip netns pids $ns 2>/dev/null); do kill -9 "$p" 2>/dev/null || true; done
      ip netns del $ns || true
    fi
  done
  for l in v-al1 v-ub1 br-lab; do ip link del $l 2>/dev/null || true; done
  umounts $ALMA; umounts $UBU
}

node_net() {  # ns veth ip
  local ns=$1 veth=$2 ip=$3
  ip netns add $ns
  ip link add $veth type veth peer name eth0 netns $ns
  ip link set $veth master br-lab up
  ip netns exec $ns ip addr add $ip/24 dev eth0
  ip netns exec $ns ip link set eth0 up
  ip netns exec $ns ip link set lo up
  ip netns exec $ns ip route add default via 10.79.0.1
}

up() {
  down
  [ -x $ALMA/usr/bin/python3 ] || { echo "missing $ALMA rootfs"; exit 1; }
  [ -x $UBU/usr/sbin/sshd ] || { echo "missing $UBU rootfs"; exit 1; }
  ip link add br-lab type bridge; ip addr add 10.79.0.1/24 dev br-lab; ip link set br-lab up
  node_net al1 v-al1 10.79.0.21
  node_net ub1 v-ub1 10.79.0.22
  mounts $ALMA; mounts $UBU
  # ---- AlmaLinux: host sshd, sessions chrooted into the Alma rootfs; keys kept outside the chroot
  mkdir -p $KEYDIR /run/sshd
  cat > /etc/ssh/sshd_lab_alma.conf <<EOF
Port 22
ListenAddress 10.79.0.21
HostKey /etc/ssh/ssh_host_ed25519_key
PermitRootLogin prohibit-password
PasswordAuthentication no
AuthorizedKeysFile $KEYDIR/%u
ChrootDirectory $ALMA
Subsystem sftp internal-sftp
PidFile /run/sshd-lab-alma.pid
UsePAM no
EOF
  [ -f /etc/ssh/ssh_host_ed25519_key ] || ssh-keygen -q -A
  ip netns exec al1 /usr/sbin/sshd -f /etc/ssh/sshd_lab_alma.conf
  # ---- Ubuntu: its own sshd inside the chroot (password login allowed for the bootstrap test)
  chroot $UBU sh -c "mkdir -p /run/sshd; [ -f /etc/ssh/ssh_host_ed25519_key ] || ssh-keygen -q -A; echo 'root:$UBU_PW' | chpasswd"
  mkdir -p $UBU/etc/ssh/sshd_config.d
  printf 'PermitRootLogin yes\nPasswordAuthentication yes\nUsePAM no\nListenAddress 10.79.0.22\n' > $UBU/etc/ssh/sshd_config.d/90-lab.conf
  printf '#!/bin/sh\nexit 101\n' > $UBU/usr/sbin/policy-rc.d; chmod +x $UBU/usr/sbin/policy-rc.d     # no daemons started by postinst
  ip netns exec ub1 chroot $UBU /usr/sbin/sshd
  for ip in 10.79.0.21 10.79.0.22; do
    for i in $(seq 1 20); do (exec 3<>/dev/tcp/$ip/22) 2>/dev/null && break; sleep 0.3; done
  done
  echo "lab up: alma 10.79.0.21, ubuntu 10.79.0.22 (root pw $UBU_PW), master 10.79.0.1"
}

snapshot() {
  down; mkdir -p $PRISTINE
  rsync -aHAX --delete $ALMA/ $PRISTINE/alma9/
  rsync -aHAX --delete $UBU/ $PRISTINE/ubu24/
  echo "snapshot saved in $PRISTINE"
}

reset() {
  down
  [ -d $PRISTINE/alma9 ] || { echo "no snapshot - run: $0 snapshot"; exit 1; }
  rsync -aHAX --delete $PRISTINE/alma9/ $ALMA/
  rsync -aHAX --delete $PRISTINE/ubu24/ $UBU/
  rm -f $KEYDIR/root
  up
}

"${1:-up}"
