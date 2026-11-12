#!/usr/bin/env python3
"""Creates a valid `docker save`-format archive (several images, shared base layer) WITHOUT docker, for testing the loader.
usage: make_fake_image_tar.py out.tar images.txt   (one RepoTag per line, e.g. quay.io/openstack.kolla/nova-api:2026.1-ubuntu-noble)"""
import hashlib, io, json, sys, tarfile

out, lst = sys.argv[1], sys.argv[2]
refs = [l.strip() for l in open(lst) if l.strip() and not l.startswith("#")]

def layer(name, text):
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as t:
        data = text.encode(); ti = tarfile.TarInfo(name); ti.size = len(data); ti.mtime = 0; t.addfile(ti, io.BytesIO(data))
    return b.getvalue()

base = layer("etc/base-release", "fake kolla base\n")
files, manifest = {}, []
def add(name, data): files[name] = data
base_id = hashlib.sha256(base).hexdigest()
add(f"{base_id}/layer.tar", base)
for ref in refs:
    top = layer(f"etc/{ref.split('/')[-1].split(':')[0]}.txt", f"image {ref}\n")
    top_id = hashlib.sha256(top).hexdigest()
    cfg = {"architecture": "amd64", "os": "linux", "config": {"Cmd": ["/bin/true"]},
           "rootfs": {"type": "layers", "diff_ids": [f"sha256:{base_id}", f"sha256:{top_id}"]},
           "history": [{"created": "1970-01-01T00:00:00Z", "created_by": "base"}, {"created": "1970-01-01T00:00:00Z", "created_by": ref}]}
    cb = json.dumps(cfg).encode(); cid = hashlib.sha256(cb).hexdigest()
    add(f"{cid}.json", cb); add(f"{top_id}/layer.tar", top)
    manifest.append({"Config": f"{cid}.json", "RepoTags": [ref], "Layers": [f"{base_id}/layer.tar", f"{top_id}/layer.tar"]})
add("manifest.json", json.dumps(manifest).encode())
with tarfile.open(out, "w") as t:
    for n, d in files.items():
        ti = tarfile.TarInfo(n); ti.size = len(d); ti.mtime = 0; t.addfile(ti, io.BytesIO(d))
print(f"wrote {out}: {len(refs)} images")
