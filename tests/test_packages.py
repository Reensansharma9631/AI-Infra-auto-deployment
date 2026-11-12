"""Package repository: real .deb/.rpm built here, chunked upload incl. resume, SHA256, safe extraction, apt + dnf indexes."""
import gzip, hashlib, io, json, os, shutil, subprocess, tarfile, zipfile

import pytest

from conftest import wait_job

needs_rpm = pytest.mark.skipif(not shutil.which("rpmbuild"), reason="rpmbuild not installed")


def make_deb(tmp, name="autoinfra-probe", version="1.0", depends=""):
    d = tmp / f"build-{name}-{version}"
    (d / "DEBIAN").mkdir(parents=True)
    (d / "usr/share/doc" / name).mkdir(parents=True)
    (d / "usr/share/doc" / name / "README").write_text("probe\n")
    ctl = f"Package: {name}\nVersion: {version}\nArchitecture: all\nMaintainer: test <t@example.com>\nDescription: probe package\n"
    if depends:
        ctl += f"Depends: {depends}\n"
    (d / "DEBIAN/control").write_text(ctl)
    out = tmp / f"{name}_{version}_all.deb"
    subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(d), str(out)], check=True, capture_output=True)
    return out


def make_rpm(tmp, name="autoinfra-probe"):
    top = tmp / "rpmbuild"
    for s in ("SPECS", "BUILD", "RPMS", "SOURCES", "SRPMS", "BUILDROOT"):
        (top / s).mkdir(parents=True, exist_ok=True)
    spec = top / "SPECS" / f"{name}.spec"
    spec.write_text(f"Name: {name}\nVersion: 1.0\nRelease: 1.el9\nSummary: probe\nLicense: MIT\nBuildArch: noarch\n%description\nprobe\n"
                    "%install\nmkdir -p %{buildroot}/usr/share/" + name + "\necho hi > %{buildroot}/usr/share/" + name + "/README\n"
                    "%files\n/usr/share/" + name + "/README\n")
    subprocess.run(["rpmbuild", "--define", f"_topdir {top}", "--nodeps", "-bb", str(spec)], check=True, capture_output=True)
    return next((top / "RPMS" / "noarch").glob("*.rpm"))


def upload(client, path, repo="custom", sha=True, chunk=1024 * 64):
    data = path.read_bytes()
    r = client.post("/api/packages/uploads", json={"name": path.name, "size": len(data), "repo": repo,
                                                   "sha256": hashlib.sha256(data).hexdigest() if sha else ""})
    assert r.status_code == 201, r.get_json()
    uid = r.get_json()["id"]
    off = 0
    while off < len(data):
        r = client.put(f"/api/packages/uploads/{uid}/chunk?offset={off}", data=data[off:off + chunk])
        assert r.status_code == 200, r.get_json()
        off = r.get_json()["received"]
    r = client.post(f"/api/packages/uploads/{uid}/finish")
    assert r.status_code == 202
    return uid, r.get_json()["job_id"]


def zip_of(tmp, files, name="bundle.zip"):
    p = tmp / name
    with zipfile.ZipFile(p, "w") as z:
        for arc, src in files.items():
            if isinstance(src, (bytes, str)):
                z.writestr(arc, src)
            else:
                z.write(src, arc)
    return p


def test_zip_of_debs_is_indexed_and_scanned(app, operator, tmp_path):
    a = make_deb(tmp_path, "autoinfra-a", "1.0", depends="autoinfra-b (>= 1.0), libc6")
    b = make_deb(tmp_path, "autoinfra-b", "1.1")
    z = zip_of(tmp_path, {"pkgs/a.deb": a, "pkgs/b.deb": b})
    uid, jid = upload(operator, z, "cloudstack")
    assert wait_job(operator, jid)["state"] == "success"
    root = app.tmp / "repo" / "cloudstack" / "deb"
    pk = (root / "Packages").read_text()
    assert "Package: autoinfra-a" in pk and "Package: autoinfra-b" in pk and "SHA256:" in pk
    assert gzip.open(root / "Packages.gz").read().decode() == pk
    # apt's own tool must produce the same set of packages from our layout
    ref = subprocess.run(["apt-ftparchive", "packages", "."], cwd=root, capture_output=True, text=True).stdout
    assert sorted(l for l in ref.splitlines() if l.startswith("Package:")) == sorted(l for l in pk.splitlines() if l.startswith("Package:"))
    items = operator.get("/api/packages/items?repo=cloudstack").get_json()["items"]
    assert {i["name"] for i in items} == {"autoinfra-a", "autoinfra-b"}
    miss = operator.get("/api/packages/deps/cloudstack").get_json()["missing"]
    assert [m["dependency"] for m in miss] == ["libc6"]
    log = operator.get(f"/api/jobs/{jid}/log.txt").data.decode()
    for step in ("[1/5] Verifying checksum ... OK", "[2/5] Extracting", "[4/5]", "[5/5]", "Completed successfully"):
        assert step in log


def test_upload_list_and_states(app, operator, tmp_path):
    deb = make_deb(tmp_path)
    uid, jid = upload(operator, deb, "kvm")
    wait_job(operator, jid)
    u = [x for x in operator.get("/api/packages/uploads").get_json()["uploads"] if x["id"] == uid][0]
    assert u["state"] == "done" and u["job_id"] == jid and u["files"] == 1


@needs_rpm
def test_rpm_repo_has_repodata(app, operator, tmp_path):
    rpm = make_rpm(tmp_path)
    z = zip_of(tmp_path, {"el9/x.rpm": rpm})
    uid, jid = upload(operator, z, "databases")
    assert wait_job(operator, jid)["state"] == "success"
    rd = app.tmp / "repo" / "databases" / "rpm" / "repodata" / "repomd.xml"
    assert rd.exists()
    items = operator.get("/api/packages/items?repo=databases&kind=rpm").get_json()["items"]
    assert items[0]["name"] == "autoinfra-probe" and items[0]["version"] == "1.0-1.el9" and items[0]["arch"] == "noarch"


def test_airgap_bundle_layout_kept(app, operator, tmp_path):
    deb = make_deb(tmp_path)
    z = zip_of(tmp_path, {"airgap/www/monitoring/deb/x.deb": deb, "airgap/www/monitoring/README.txt": "hello",
                          "airgap/www/templates/systemvm/systemvmtemplate-4.22.0-x86_64-kvm.qcow2.bz2": b"BZh9fake"})
    uid, jid = upload(operator, z, "custom")
    assert wait_job(operator, jid)["state"] == "success"
    assert (app.tmp / "repo/monitoring/deb/x.deb").exists() and (app.tmp / "repo/monitoring/files/README.txt").exists()
    assert (app.tmp / "repo/templates/files/systemvm/systemvmtemplate-4.22.0-x86_64-kvm.qcow2.bz2").exists()


def test_wrong_sha_rejected(operator, tmp_path):
    deb = make_deb(tmp_path)
    data = deb.read_bytes()
    r = operator.post("/api/packages/uploads", json={"name": deb.name, "size": len(data), "repo": "custom", "sha256": "0" * 64})
    uid = r.get_json()["id"]
    operator.put(f"/api/packages/uploads/{uid}/chunk?offset=0", data=data)
    jid = operator.post(f"/api/packages/uploads/{uid}/finish").get_json()["job_id"]
    assert wait_job(operator, jid)["state"] == "failed"
    u = [x for x in operator.get("/api/packages/uploads").get_json()["uploads"] if x["id"] == uid][0]
    assert u["state"] == "error" and "checksum mismatch" in u["message"]


def test_resume_after_offset_mismatch(operator, tmp_path):
    data = os.urandom(300_000)
    p = tmp_path / "blob.bin"
    p.write_bytes(data)
    r = operator.post("/api/packages/uploads", json={"name": "blob.bin", "size": len(data), "repo": "custom"})
    uid = r.get_json()["id"]
    assert operator.put(f"/api/packages/uploads/{uid}/chunk?offset=0", data=data[:100_000]).status_code == 200
    r = operator.put(f"/api/packages/uploads/{uid}/chunk?offset=0", data=data[:100_000])   # duplicate chunk after a "network error"
    assert r.status_code == 409 and r.get_json()["received"] == 100_000
    assert operator.put(f"/api/packages/uploads/{uid}/chunk?offset=100000", data=data[100_000:]).status_code == 200
    jid = operator.post(f"/api/packages/uploads/{uid}/finish").get_json()["job_id"]
    assert wait_job(operator, jid)["state"] == "success"


def test_finish_incomplete_upload(operator):
    uid = operator.post("/api/packages/uploads", json={"name": "a.zip", "size": 10, "repo": "custom"}).get_json()["id"]
    operator.put(f"/api/packages/uploads/{uid}/chunk?offset=0", data=b"12345")
    r = operator.post(f"/api/packages/uploads/{uid}/finish")
    assert r.status_code == 409 and r.get_json()["received"] == 5


def test_too_much_data(operator):
    uid = operator.post("/api/packages/uploads", json={"name": "a.zip", "size": 4, "repo": "custom"}).get_json()["id"]
    assert operator.put(f"/api/packages/uploads/{uid}/chunk?offset=0", data=b"123456").status_code == 400


def test_chunk_after_finish_refused(operator, tmp_path):
    deb = make_deb(tmp_path)
    uid, jid = upload(operator, deb)
    wait_job(operator, jid)
    assert operator.put(f"/api/packages/uploads/{uid}/chunk?offset=0", data=b"x").status_code == 409


def test_delete_upload(operator):
    uid = operator.post("/api/packages/uploads", json={"name": "a.zip", "size": 10, "repo": "custom"}).get_json()["id"]
    assert operator.delete(f"/api/packages/uploads/{uid}").status_code == 200
    assert operator.delete(f"/api/packages/uploads/{uid}").status_code == 404


@pytest.mark.parametrize("body,field", [
    ({"name": "../evil.zip", "size": 10, "repo": "custom"}, "name"), ({"name": "a b.zip", "size": 10, "repo": "custom"}, "name"),
    ({"name": "x.exe", "size": 10, "repo": "custom"}, "name"), ({"name": "a.zip", "size": 10, "repo": "nope"}, "repo"),
    ({"name": "a.zip", "size": 0, "repo": "custom"}, "size"), ({"name": "a.zip", "size": "x", "repo": "custom"}, "size"),
    ({"name": "a.zip", "size": 10, "repo": "custom", "sha256": "xyz"}, "sha256"), ({"name": "", "size": 10, "repo": "custom"}, "name"),
    ({"name": "/etc/passwd", "size": 10, "repo": "custom"}, "name"), ({"name": ".hidden.zip", "size": 10, "repo": "custom"}, "name"),
])
def test_upload_validation(operator, body, field):
    r = operator.post("/api/packages/uploads", json=body)
    assert r.status_code == 400 and field in r.get_json()["fields"]


@pytest.mark.parametrize("name", ["a.zip", "a.tar", "a.tar.gz", "a.tgz", "a.tar.xz", "a.tar.bz2", "a.iso", "a.deb", "a.rpm", "a.whl",
                                  "a.qcow2", "a.qcow2.bz2", "a.img", "model.gguf"])
def test_upload_accepts_types(operator, name):
    assert operator.post("/api/packages/uploads", json={"name": name, "size": 10, "repo": "custom"}).status_code == 201


def test_disk_space_check(operator, monkeypatch):
    import collections, shutil as sh
    monkeypatch.setattr(sh, "disk_usage", lambda p: collections.namedtuple("u", "total used free")(100, 99, 1))
    r = operator.post("/api/packages/uploads", json={"name": "a.zip", "size": 10, "repo": "custom"})
    assert r.status_code == 507


@pytest.mark.parametrize("member", ["../escape.txt", "/abs/path.txt", "a/../../escape.txt", "C:/win.txt", "..\\win.txt"])
def test_zip_traversal_refused(app, operator, tmp_path, member):
    z = zip_of(tmp_path, {member: "x"})
    uid, jid = upload(operator, z)
    assert wait_job(operator, jid)["state"] == "failed"
    assert not (tmp_path.parent / "escape.txt").exists()
    assert "archive" in operator.get(f"/api/jobs/{jid}/log.txt").data.decode()


def tar_of(tmp, mode, build):
    p = tmp / ("x.tar" + {"w": "", "w:gz": ".gz", "w:xz": ".xz", "w:bz2": ".bz2"}[mode])
    with tarfile.open(p, mode) as t:
        build(t)
    return p


def add(t, name, data=b"x", **kw):
    ti = tarfile.TarInfo(name)
    for k, v in kw.items():
        setattr(ti, k, v)
    ti.size = len(data) if ti.type == tarfile.REGTYPE else 0
    t.addfile(ti, io.BytesIO(data) if ti.type == tarfile.REGTYPE else None)


@pytest.mark.parametrize("kind", [
    {"name": "../evil"}, {"name": "/etc/evil"}, {"name": "link", "type": tarfile.SYMTYPE, "linkname": "/etc/passwd"},
    {"name": "hard", "type": tarfile.LNKTYPE, "linkname": "x"}, {"name": "dev", "type": tarfile.CHRTYPE}, {"name": "fifo", "type": tarfile.FIFOTYPE},
])
def test_tar_unsafe_members_refused(operator, tmp_path, kind):
    p = tar_of(tmp_path, "w:gz", lambda t: add(t, **kind))
    uid, jid = upload(operator, p)
    assert wait_job(operator, jid)["state"] == "failed"


@pytest.mark.parametrize("mode", ["w", "w:gz", "w:xz", "w:bz2"])
def test_tar_formats_extract(app, operator, tmp_path, mode):
    deb = make_deb(tmp_path)
    p = tar_of(tmp_path, mode, lambda t: t.add(deb, arcname="debs/probe.deb"))
    uid, jid = upload(operator, p, "apps")
    assert wait_job(operator, jid)["state"] == "success"
    assert (app.tmp / "repo/apps/deb/probe.deb").exists()


def test_symlink_in_zip_refused(operator, tmp_path):
    p = tmp_path / "l.zip"
    with zipfile.ZipFile(p, "w") as z:
        zi = zipfile.ZipInfo("link")
        zi.external_attr = (0o120777 << 16)
        z.writestr(zi, "/etc/passwd")
    uid, jid = upload(operator, p)
    assert wait_job(operator, jid)["state"] == "failed"


def test_extraction_size_limit(app, operator, tmp_path):
    from autoinfra import db
    with app.app_context():
        db.set_setting("max_extract_gb", 1)
    from autoinfra import packages
    big = tmp_path / "big.zip"
    with zipfile.ZipFile(big, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("zero.bin", b"\0" * (2 * 1024 * 1024))
    with pytest.raises(packages.UnsafeArchive):
        packages.extract_archive(str(big), str(tmp_path / "out"), limit=1024 * 1024)


def test_single_qcow_file_goes_to_files(app, operator, tmp_path):
    p = tmp_path / "systemvmtemplate-4.22.0-x86_64-kvm.qcow2.bz2"
    p.write_bytes(b"BZh91AY&SY" + os.urandom(1000))
    uid, jid = upload(operator, p, "templates")
    assert wait_job(operator, jid)["state"] == "success"
    items = operator.get("/api/packages/items?repo=templates").get_json()["items"]
    assert items[0]["kind"] == "file" and items[0]["path"].endswith(".qcow2.bz2")


def test_wheels_sorted(app, operator, tmp_path):
    z = zip_of(tmp_path, {"wheels/foo-1.0-py3-none-any.whl": b"PK fake wheel"})
    uid, jid = upload(operator, z, "openstack")
    wait_job(operator, jid)
    assert (app.tmp / "repo/openstack/wheels/foo-1.0-py3-none-any.whl").exists()
    assert operator.get("/api/packages/items?repo=openstack&kind=whl").get_json()["items"]


def test_reindex_and_verify(app, operator, tmp_path):
    deb = make_deb(tmp_path)
    uid, jid = upload(operator, deb, "core")
    wait_job(operator, jid)
    jid2 = operator.post("/api/packages/reindex/core").get_json()["job_id"]
    assert wait_job(operator, jid2)["state"] == "success"
    v = operator.post("/api/packages/verify/core").get_json()
    assert v["ok"] and v["checked"] == 1
    f = next((app.tmp / "repo/core/deb").glob("*.deb"))
    f.write_bytes(f.read_bytes() + b"tamper")
    v = operator.post("/api/packages/verify/core").get_json()
    assert not v["ok"] and v["bad"]


def test_repo_summary_and_disk(operator, tmp_path):
    deb = make_deb(tmp_path)
    uid, jid = upload(operator, deb, "custom")
    wait_job(operator, jid)
    s = {r["repo"]: r for r in operator.get("/api/packages/repo").get_json()["repos"]}
    assert s["custom"]["deb"] == 1 and s["custom"]["size"] > 0 and len(s) == 11
    d = operator.get("/api/packages/disk").get_json()
    assert d["total"] > 0 and d["repos"]["custom"] > 0


@pytest.mark.parametrize("repo", ["nope", "../x"])
def test_unknown_repo_404(operator, repo):
    assert operator.get(f"/api/packages/deps/{repo}").status_code == 404
    assert operator.post(f"/api/packages/reindex/{repo}").status_code in (404,)


@pytest.mark.parametrize("product", ["cloudstack", "openstack", "kvm", "databases", "monitoring", "ollama"])
@pytest.mark.parametrize("family", ["debian", "rhel"])
def test_required_lists(operator, product, family):
    r = operator.get(f"/api/packages/required/{product}?family={family}").get_json()
    assert r["items"] and r["ready"] is False and all("present" in i for i in r["items"])


def test_required_becomes_ready(app, operator, tmp_path):
    debs = [make_deb(tmp_path, n) for n in ("qemu-system-x86", "qemu-utils", "ovmf", "cpu-checker", "libvirt-daemon-system", "libvirt-clients", "virtinst",
                                             "bridge-utils", "vlan", "openvswitch-switch")]
    z = zip_of(tmp_path, {f"d/{p.name}": p for p in debs})
    uid, jid = upload(operator, z, "kvm")
    wait_job(operator, jid)
    assert operator.get("/api/packages/required/kvm?family=debian").get_json()["ready"] is True


def test_required_bad_inputs(operator):
    assert operator.get("/api/packages/required/nope").status_code == 404
    assert operator.get("/api/packages/required/kvm?family=windows").status_code == 400


@pytest.mark.parametrize("product", ["cloudstack", "openstack", "kvm", "databases", "monitoring", "ollama"])
@pytest.mark.parametrize("os_", ["ubuntu-24.04", "rocky-9", "alma-9"])
def test_download_kit_is_valid_bash(operator, product, os_):
    r = operator.post("/api/packages/online/kit", json={"product": product, "os": os_})
    assert r.status_code == 200 and r.headers["X-Zip-Name"].endswith(".zip")
    script = r.data.decode()
    assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0
    assert ('FAMILY="rhel"' in script) == os_.startswith(("rocky", "alma"))


def test_kit_cloudstack_has_repo_and_template(operator):
    s = operator.post("/api/packages/online/kit", json={"product": "cloudstack", "os": "ubuntu-24.04", "version": "4.22.0"}).data.decode()
    assert "download.cloudstack.org/ubuntu noble 4.22" in s and "systemvmtemplate-4.22.0-x86_64-kvm.qcow2.bz2" in s
    s = operator.post("/api/packages/online/kit", json={"product": "cloudstack", "os": "rocky-9", "version": "4.20.1"}).data.decode()
    assert "el/9/4.20" in s


def test_kit_bad_version(operator):
    assert operator.post("/api/packages/online/kit", json={"product": "cloudstack", "version": "x;rm -rf /"}).status_code == 400


def test_manifest_and_catalog(operator):
    m = operator.post("/api/packages/online/manifest", json={"product": "cloudstack", "os": "rocky-9"}).get_json()
    assert m["family"] == "rhel" and m["total_mb"] > 1000 and m["online_enabled"] is False
    assert "cloudstack" in operator.get("/api/packages/online/manifest-catalog").get_json()


def test_online_fetch_refused_when_airgapped(operator):
    assert operator.post("/api/packages/online/fetch", json={"product": "kvm"}).status_code == 409


def test_deb_control_pure_python_gz(tmp_path):
    """Fallback parser (no dpkg-deb) on a gzip-compressed control archive."""
    from autoinfra import packages
    d = tmp_path / "b"
    (d / "DEBIAN").mkdir(parents=True)
    (d / "DEBIAN/control").write_text("Package: purepy\nVersion: 2.0\nArchitecture: all\nMaintainer: x <x@x.x>\nDescription: y\n")
    out = tmp_path / "p.deb"
    subprocess.run(["dpkg-deb", "-Zgzip", "--build", "--root-owner-group", str(d), str(out)], check=True, capture_output=True)
    old = shutil.which
    try:
        packages.shutil.which = lambda n: None if n == "dpkg-deb" else old(n)
        c = packages.deb_control(str(out))
    finally:
        packages.shutil.which = old
    assert c["Package"] == "purepy" and c["Version"] == "2.0"


def test_broken_deb_skipped(app, operator, tmp_path):
    bad = tmp_path / "broken_1.0_all.deb"
    bad.write_bytes(b"not an ar archive")
    good = make_deb(tmp_path)
    z = zip_of(tmp_path, {"a.deb": bad, "b.deb": good})
    uid, jid = upload(operator, z, "custom")
    assert wait_job(operator, jid)["state"] == "success"
    assert "skipping broken package" in operator.get(f"/api/jobs/{jid}/log.txt").data.decode()


def test_virtual_package_provided(app, operator, tmp_path):
    """Ubuntu 24.04: qemu-kvm is a virtual package Provided by qemu-system-x86 - it must count as present."""
    d = tmp_path / "qsx"
    (d / "DEBIAN").mkdir(parents=True)
    (d / "DEBIAN/control").write_text("Package: qemu-system-x86\nVersion: 8.2\nArchitecture: all\nMaintainer: x <x@x.x>\nProvides: qemu-kvm, qemu-system-i386\nDescription: q\n")
    out = tmp_path / "qemu-system-x86_8.2_all.deb"
    subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(d), str(out)], check=True, capture_output=True)
    uid, jid = upload(operator, out, "kvm")
    wait_job(operator, jid)
    items = {i["id"]: i for i in operator.get("/api/packages/required/kvm?family=debian").get_json()["items"]}
    assert items["qemu"]["missing"] == ["qemu-utils", "ovmf", "cpu-checker"]


def test_repo_served_over_http_without_login(app, operator, anon, tmp_path):
    """run-local.sh mode: nodes fetch http://MASTER:PORT/repo/... (no nginx)"""
    deb = make_deb(tmp_path)
    uid, jid = upload(operator, deb, "core")
    wait_job(operator, jid)
    r = anon.get("/repo/core/deb/Packages")
    assert r.status_code == 200 and b"Package: autoinfra-probe" in r.data
    assert anon.get("/repo/core/deb/").status_code == 200 and b"Packages" in anon.get("/repo/core/deb/").data
    assert anon.get("/repo/").status_code == 200
    for bad in ("/repo/../data/autoinfra.db", "/repo/%2e%2e/data/autoinfra.db", "/repo/core/deb/../../../data/secrets.json", "/repo/nope"):
        assert anon.get(bad).status_code == 404, bad


def test_repo_url_env(app, monkeypatch):
    from autoinfra import inventory
    monkeypatch.setenv("AUTOINFRA_REPO_URL", "http://10.0.0.5:8000/repo")
    with app.app_context():
        assert inventory.repo_base() == "http://10.0.0.5:8000/repo"


def test_kit_downloads_everything_and_restores_apt_sources(app):
    """No-docker mode on a used machine: resolve against an empty dpkg status; never leave vendor sources behind."""
    from autoinfra import packages
    with app.app_context():
        _, script, _ = packages.build_kit("cloudstack", "debian", "24.04", "4.22.0")
    assert "Dir::State::status=$STAGE/empty-status" in script and script.count("apt-get $E install") == 2
    assert "/etc/apt/sources.list.d/autoinfra-kit-cloudstack.list" in script
    assert "trap 'rm -f /etc/apt/sources.list.d/autoinfra-kit-*.list" in script
    import subprocess, tempfile, os
    p = os.path.join(tempfile.mkdtemp(), "kit.sh"); open(p, "w").write(script)
    assert subprocess.run(["bash", "-n", p]).returncode == 0


def test_build_scripts_parse():
    import pathlib, subprocess
    for f in sorted((pathlib.Path(__file__).parent.parent / "online").glob("*.sh")):
        assert subprocess.run(["bash", "-n", str(f)]).returncode == 0, f
    assert (pathlib.Path(__file__).parent.parent / "online/build-all.sh").exists()
