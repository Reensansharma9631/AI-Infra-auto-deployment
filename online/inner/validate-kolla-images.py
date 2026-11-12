#!/usr/bin/env python3
"""Fail if an image in openstack-images.txt is not a real Kolla image name of the installed kolla-ansible.
usage: validate-kolla-images.py <share/kolla-ansible dir> <images.txt>"""
import pathlib, re, sys
share, lst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
known = set()
for f in (share / "ansible").rglob("*.yml"):
    for m in re.finditer(r'^\s*[a-z0-9_]+_image:\s*"\{\{\s*docker_image_url\s*\}\}([a-z0-9][a-z0-9._-]*)"', f.read_text(errors="ignore"), re.M):
        known.add(m.group(1))
want = [l.strip() for l in lst.read_text().splitlines() if l.strip() and not l.startswith("#")]
bad = [w for w in want if w not in known]
print(f"kolla-ansible knows {len(known)} image names; list has {len(want)}; unknown: {len(bad)}")
for b in bad:
    close = sorted(k for k in known if b.split('-')[0] in k)[:5]
    print(f"  UNKNOWN image '{b}'  (similar: {', '.join(close) or 'none'})")
sys.exit(1 if bad else 0)
