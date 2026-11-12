"""Package repository: chunked/resumable uploads, SHA256 verification, safe extraction (zip/tar/iso), apt + dnf repo
indexing, dependency scan, and the online 'download kit' for an internet-connected machine."""
import fnmatch, gzip, hashlib, io, json, lzma, os, re, shutil, stat, subprocess, tarfile, tempfile, time, zipfile

from flask import Blueprint, Response, abort, g, jsonify, render_template, request, send_file, send_from_directory

from . import auth, catalog, db, jobs, validators as V

bp = Blueprint("packages", __name__)
REPOS = catalog.REPOS
ARCHIVE_EXT = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".iso")
SINGLE_EXT = (".deb", ".rpm", ".whl", ".qcow2", ".bz2", ".img", ".raw", ".ova", ".tar.zst", ".gguf", ".bin", ".json", ".txt")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,200}$")
MAX_FILES = 300_000


def repo_root():
    d = os.environ.get("AUTOINFRA_REPO") or os.path.join(db.data_dir(), "repo")
    os.makedirs(d, exist_ok=True)
    return d


def upload_dir():
    d = os.path.join(db.data_dir(), "uploads")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def max_extract_bytes():
    return int(db.get_setting("max_extract_gb", 300)) * 1024 ** 3


def sha256_file(p, bufsize=4 * 1024 * 1024):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(bufsize), b""):
            h.update(b)
    return h.hexdigest()


def disk_usage():
    root = repo_root()
    du = shutil.disk_usage(root)
    per = {}
    for r in REPOS:
        d = os.path.join(root, r)
        tot = 0
        for dp, _, fs in os.walk(d):
            for f in fs:
                try:
                    tot += os.lstat(os.path.join(dp, f)).st_size
                except OSError:
                    pass
        per[r] = tot
    up = sum(os.path.getsize(os.path.join(upload_dir(), f)) for f in os.listdir(upload_dir()))
    return {"total": du.total, "used": du.used, "free": du.free, "repos": per, "uploads_pending": up}


# ------------------------------------------------------------------ safe extraction
class UnsafeArchive(Exception):
    pass


def safe_join(base, member):
    m = member.replace("\\", "/")
    if m.startswith("/") or re.match(r"^[A-Za-z]:", m):
        raise UnsafeArchive(f"absolute path in archive: {member}")
    parts = [p for p in m.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafeArchive(f"path traversal in archive: {member}")
    out = os.path.realpath(os.path.join(base, *parts)) if parts else os.path.realpath(base)
    if not (out == os.path.realpath(base) or out.startswith(os.path.realpath(base) + os.sep)):
        raise UnsafeArchive(f"path escapes the target: {member}")
    return out


def kind_of(name):
    n = name.lower()
    for e in sorted(ARCHIVE_EXT, key=len, reverse=True):
        if n.endswith(e):
            return e
    return None


def extract_archive(path, dest, log=print, limit=None, name=None):
    """name: original file name (the stored upload is <id>.part, so the type comes from the name)."""
    limit = limit or max_extract_bytes()
    os.makedirs(dest, exist_ok=True)
    ext = kind_of(name or path) or ""
    total = count = 0
    if ext == ".zip":
        with zipfile.ZipFile(path) as z:
            for i in z.infolist():
                mode = (i.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise UnsafeArchive(f"symbolic link in archive: {i.filename}")
                target = safe_join(dest, i.filename)
                count += 1
                total += i.file_size
                if count > MAX_FILES or total > limit:
                    raise UnsafeArchive("archive is larger than the allowed extraction size / file count")
                if i.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(i) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
    elif ext == ".iso":
        bsdtar = shutil.which("bsdtar")
        if not bsdtar:
            raise UnsafeArchive("ISO extraction needs bsdtar (package libarchive-tools); upload as a file to the 'templates' repo instead")
        listing = subprocess.run([bsdtar, "-tf", path], capture_output=True, text=True, check=True).stdout.splitlines()
        for m in listing:
            safe_join(dest, m)
        if len(listing) > MAX_FILES:
            raise UnsafeArchive("too many files in ISO")
        subprocess.run([bsdtar, "-xf", path, "-C", dest, "--no-same-owner"], check=True)
        count = len(listing)
        for dp, _, fs in os.walk(dest):
            for f in fs:
                p = os.path.join(dp, f)
                if os.path.islink(p):
                    os.unlink(p)
                else:
                    total += os.path.getsize(p)
    else:
        mode = {".tar": "r:", ".tar.gz": "r:gz", ".tgz": "r:gz", ".tar.xz": "r:xz", ".txz": "r:xz", ".tar.bz2": "r:bz2", ".tbz2": "r:bz2"}.get(ext)
        if not mode:
            raise UnsafeArchive(f"unsupported archive type: {os.path.basename(path)}")
        with tarfile.open(path, mode) as t:
            for m in t:
                if m.issym() or m.islnk():
                    raise UnsafeArchive(f"link in archive: {m.name}")
                if m.isdev() or m.isfifo():
                    raise UnsafeArchive(f"device/fifo in archive: {m.name}")
                target = safe_join(dest, m.name)
                count += 1
                total += m.size
                if count > MAX_FILES or total > limit:
                    raise UnsafeArchive("archive is larger than the allowed extraction size / file count")
                if m.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = t.extractfile(m)
                with open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
                os.chmod(target, 0o644 | (0o111 if m.mode & 0o111 else 0))
    log(f"extracted {count} entries ({total / 1024 / 1024:.1f} MB)")
    return count, total


# ------------------------------------------------------------------ deb indexing
def _ar_members(path):
    with open(path, "rb") as fh:
        if fh.read(8) != b"!<arch>\n":
            raise ValueError("not a .deb (ar) file")
        while True:
            hdr = fh.read(60)
            if len(hdr) < 60:
                return
            name = hdr[:16].decode().strip().rstrip("/")
            size = int(hdr[48:58].decode().strip())
            data = fh.read(size)
            if size % 2:
                fh.read(1)
            yield name, data


def deb_control(path):
    """Control fields of a .deb. Uses dpkg-deb when present (handles zstd), else pure Python (gz/xz/zstd-if-module)."""
    if shutil.which("dpkg-deb"):
        r = subprocess.run(["dpkg-deb", "-f", path], capture_output=True, text=True)
        if r.returncode == 0:
            return parse_control(r.stdout)
    for name, data in _ar_members(path):
        if name.startswith("control.tar"):
            if name.endswith(".zst"):
                try:
                    import zstandard
                except ImportError as e:
                    raise ValueError("zstd-compressed .deb needs dpkg-deb or the zstandard module") from e
                data = zstandard.ZstdDecompressor().decompress(data, max_output_size=64 * 1024 * 1024)
                mode = "r:"
            else:
                mode = {"control.tar.gz": "r:gz", "control.tar.xz": "r:xz", "control.tar": "r:"}[name]
            with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as t:
                for m in t:
                    if m.name.lstrip("./") == "control":
                        return parse_control(t.extractfile(m).read().decode(errors="replace"))
    raise ValueError("no control file")


def parse_control(text):
    out, key = {}, None
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and key:
            out[key] += "\n" + line
        elif ":" in line:
            key, v = line.split(":", 1)
            key = key.strip()
            out[key] = v.strip()
    return out


ORDER = ["Package", "Version", "Architecture", "Maintainer", "Installed-Size", "Pre-Depends", "Depends", "Recommends",
         "Suggests", "Conflicts", "Breaks", "Replaces", "Provides", "Section", "Priority", "Multi-Arch", "Source", "Homepage"]


def index_deb_dir(deb_dir, log=print):
    entries, items = [], []
    for dp, _, fs in os.walk(deb_dir):
        for f in sorted(fs):
            if not f.endswith(".deb"):
                continue
            p = os.path.join(dp, f)
            try:
                c = deb_control(p)
            except Exception as e:  # noqa: BLE001
                log(f"WARNING: skipping broken package {f}: {e}")
                continue
            rel = os.path.relpath(p, deb_dir)
            data = open(p, "rb").read()
            md5, sha = hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest()  # noqa: S324 - apt index format
            lines = [f"{k}: {c[k]}" for k in ORDER if k in c]
            lines += [f"Filename: ./{rel}", f"Size: {len(data)}", f"MD5sum: {md5}", f"SHA256: {sha}"]
            if "Description" in c:
                lines.append(f"Description: {c['Description']}")
            entries.append("\n".join(lines))
            items.append({"name": c.get("Package", f), "version": c.get("Version", ""), "arch": c.get("Architecture", ""),
                          "path": rel, "size": len(data), "sha256": sha, "depends": c.get("Depends", "") + (", " + c["Pre-Depends"] if c.get("Pre-Depends") else ""),
                          "provides": c.get("Provides", "")})
    text = "\n\n".join(entries) + ("\n" if entries else "")
    open(os.path.join(deb_dir, "Packages"), "w").write(text)
    with gzip.open(os.path.join(deb_dir, "Packages.gz"), "wt") as fh:
        fh.write(text)
    rel = (f"Origin: AutoInfra\nLabel: AutoInfra\nSuite: ./\nDate: {time.strftime('%a, %d %b %Y %H:%M:%S UTC', time.gmtime())}\n"
           "SHA256:\n")
    for n in ("Packages", "Packages.gz"):
        p = os.path.join(deb_dir, n)
        rel += f" {sha256_file(p)} {os.path.getsize(p)} {n}\n"
    open(os.path.join(deb_dir, "Release"), "w").write(rel)
    log(f"apt index: {len(items)} packages")
    return items


def rpm_header(path):
    """name, version-release, arch from an .rpm using the rpm tool when available."""
    if shutil.which("rpm"):
        r = subprocess.run(["rpm", "-qp", "--nosignature", "--nodigest", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\n", path],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            n, v, a = r.stdout.strip().split("\t")
            return n, v, a
    m = re.match(r"^(.+)-([^-]+-[^-]+)\.([^.]+)\.rpm$", os.path.basename(path))
    return (m.group(1), m.group(2), m.group(3)) if m else (os.path.basename(path), "", "")


def index_rpm_dir(rpm_dir, log=print):
    items = []
    for dp, _, fs in os.walk(rpm_dir):
        if "repodata" in dp.split(os.sep):
            continue
        for f in sorted(fs):
            if f.endswith(".rpm"):
                p = os.path.join(dp, f)
                n, v, a = rpm_header(p)
                items.append({"name": n, "version": v, "arch": a, "path": os.path.relpath(p, rpm_dir), "size": os.path.getsize(p),
                              "sha256": sha256_file(p), "depends": "", "provides": ""})
    tool = shutil.which("createrepo_c") or shutil.which("createrepo")
    if not tool:
        log("WARNING: createrepo_c is not installed - rpm files stored but NOT indexed (dnf cannot use this repo yet)")
        return items
    r = subprocess.run([tool, "--update", "-q", rpm_dir] if os.path.isdir(os.path.join(rpm_dir, "repodata")) else [tool, "-q", rpm_dir],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"createrepo_c failed: {r.stderr.strip()[:500]}")
    log(f"dnf index (repodata): {len(items)} packages")
    return items


def _dep_names(dep):
    out = []
    for group in [x.strip() for x in (dep or "").split(",") if x.strip()]:
        alts = [re.sub(r"\s*\(.*?\)|:any|\[.*?\]", "", a).strip() for a in group.split("|")]
        out.append([a for a in alts if a])
    return out


def scan_deps(repo, items=None):
    """Debian dependencies that no package of the master repositories satisfies (may still be on the base OS)."""
    rows = items if items is not None else db.q("SELECT * FROM repo_items WHERE kind='deb'")
    known = {r["name"] for r in db.q("SELECT name FROM repo_items WHERE kind='deb'")}
    provides = set()
    for r in rows:
        known.add(r["name"])
        for p in _dep_names(r.get("provides", "")):
            provides.update(p)
    deps_path = os.path.join(repo_root(), repo, "deb", "Packages")
    if items is None and os.path.exists(deps_path):
        ctl = [parse_control(b) for b in open(deps_path).read().split("\n\n") if b.strip()]
        rows = [{"name": c.get("Package"), "depends": c.get("Depends", "") + ("," + c["Pre-Depends"] if c.get("Pre-Depends") else "")} for c in ctl]
        for c in ctl:
            for p in _dep_names(c.get("Provides", "")):
                provides.update(p)
    missing = {}
    for r in rows:
        for alts in _dep_names(r.get("depends", "")):
            if not any(a in known or a in provides for a in alts):
                missing.setdefault(" | ".join(alts), []).append(r["name"])
    return [{"dependency": k, "needed_by": sorted(set(v))} for k, v in sorted(missing.items())]


def reindex(repo, log=print):
    base = os.path.join(repo_root(), repo)
    n = 0
    db.ex("DELETE FROM repo_items WHERE repo=?", (repo,))
    for kind, sub, fn in (("deb", "deb", index_deb_dir), ("rpm", "rpm", index_rpm_dir)):
        d = os.path.join(base, sub)
        if os.path.isdir(d):
            for it in fn(d, log):
                prov = " ".join(sorted({a for g in _dep_names(it.get("provides", "")) for a in g}))
                db.ex("INSERT OR REPLACE INTO repo_items(repo,kind,name,version,arch,path,size,sha256,created,provides) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (repo, kind, it["name"], it["version"], it["arch"], f"{sub}/{it['path']}", it["size"], it["sha256"], time.time(), prov))
                n += 1
    for sub in ("files", "wheels", "images", "systemvm", "models"):
        d = os.path.join(base, sub)
        for dp, _, fs in os.walk(d):
            for f in fs:
                p = os.path.join(dp, f)
                db.ex("INSERT OR REPLACE INTO repo_items(repo,kind,name,version,arch,path,size,sha256,created) VALUES(?,?,?,?,?,?,?,?,?)",
                      (repo, "whl" if f.endswith(".whl") else "file", f, "", "", os.path.relpath(p, base), os.path.getsize(p), "", time.time()))
                n += 1
    log(f"repository '{repo}': {n} items indexed")
    return n


def place_files(src_dir, repo, log=print):
    """Sort extracted files into <repo>/{deb,rpm,wheels,files}; an airgap bundle (airgap/www/<repo>/...) keeps its layout."""
    root = repo_root()
    touched = set()
    moved = 0
    for dp, dns, fs in os.walk(src_dir):
        rel_dp = os.path.relpath(dp, src_dir)
        parts = [] if rel_dp == "." else rel_dp.split(os.sep)
        for f in fs:
            p = os.path.join(dp, f)
            if os.path.islink(p):
                continue
            target_repo, sub = repo, None
            if "www" in parts:
                i = parts.index("www")
                tail = parts[i + 1:]
                if tail and tail[0] in REPOS:
                    target_repo = tail[0]
                    rest = tail[1:]
                    if rest and rest[0] in ("deb", "rpm"):
                        sub = os.path.join(*rest) if rest else ""
                    elif f.endswith(".deb"):
                        sub = "deb"
                    elif f.endswith(".rpm"):
                        sub = "rpm"
                    else:
                        sub = os.path.join("files", *rest) if rest else "files"
            if sub is None:
                sub = "deb" if f.endswith(".deb") else "rpm" if f.endswith(".rpm") else "wheels" if f.endswith(".whl") else \
                    os.path.join("files", *parts) if parts else "files"
            if sub.split(os.sep)[0] in ("deb", "rpm") and f.startswith(("Packages", "Release", "InRelease")):
                continue          # rebuilt by reindex
            if "repodata" in sub.split(os.sep):
                continue
            dst_dir = os.path.join(root, target_repo, sub)
            os.makedirs(dst_dir, exist_ok=True)
            shutil.move(p, os.path.join(dst_dir, f))
            moved += 1
            touched.add(target_repo)
    log(f"placed {moved} files into: {', '.join(sorted(touched)) or '-'}")
    return sorted(touched)


@jobs.register("process_upload")
def process_upload(log, upload_id):
    u = db.one("SELECT * FROM uploads WHERE id=?", (upload_id,))
    path = u["path"]
    st = lambda s, m="": db.ex("UPDATE uploads SET state=?, message=? WHERE id=?", (s, m, upload_id))
    try:
        st("verifying")
        log(f"[1/5] Verifying SHA256 checksum of {u['name']} ...")
        got = sha256_file(path)
        if u["sha256"] and got != u["sha256"]:
            raise ValueError(f"checksum mismatch: expected {u['sha256']}, got {got}")
        log(f"[1/5] Verifying checksum ... OK ({got[:16]}...)")
        work = tempfile.mkdtemp(prefix=f"x{upload_id}-", dir=upload_dir())
        try:
            if kind_of(u["name"]):
                st("extracting")
                log(f"[2/5] Extracting {u['name']} ...")
                n, _ = extract_archive(path, work, log, name=u["name"])
            else:
                log("[2/5] Single file - no extraction needed")
                shutil.copy(path, os.path.join(work, u["name"]))
                n = 1
            log("[3/5] Categorizing packages by component ...")
            touched = place_files(work, u["repo"], log)
            st("indexing")
            log("[4/5] Creating local apt/yum/dnf repositories ...")
            total = 0
            for r in touched or [u["repo"]]:
                total += reindex(r, log)
            log("[5/5] Scanning for dependencies ...")
            for r in touched:
                miss = scan_deps(r)
                if miss:
                    log(f"   {r}: {len(miss)} dependency group(s) not in the master repos (fine if the base OS provides them):")
                    for m in miss[:25]:
                        log(f"     - {m['dependency']}  (needed by {', '.join(m['needed_by'][:4])})")
                else:
                    log(f"   {r}: all dependencies satisfied")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        os.unlink(path)
        db.ex("UPDATE uploads SET state='done', message=?, files=?, finished=? WHERE id=?",
              (f"{n} files extracted", n, time.time(), upload_id))
        log(f"Total files extracted: {n}")
        log(f"Repository path: {os.path.join(repo_root(), u['repo'])}")
        log("Status: Completed successfully")
        return True
    except Exception as e:  # noqa: BLE001
        db.ex("UPDATE uploads SET state='error', message=?, finished=? WHERE id=?", (str(e)[:500], time.time(), upload_id))
        log(f"ERROR: {e}")
        return False


# ------------------------------------------------------------------ repository over HTTP for the nodes (read only, no login)
# With install-autoinfra.sh nginx serves the same directory on :8081/repo/; this route lets run-local.sh work without nginx.
@bp.route("/repo/")
@bp.route("/repo/<path:p>")
def serve_repo(p=""):
    root = repo_root()
    full = os.path.realpath(os.path.join(root, p))
    if not (full == os.path.realpath(root) or full.startswith(os.path.realpath(root) + os.sep)):
        abort(404)
    if os.path.isdir(full):
        names = sorted(os.listdir(full))
        body = "".join(f'<a href="{n}{"/" if os.path.isdir(os.path.join(full, n)) else ""}">{n}</a>\n' for n in names)
        return Response(f"<pre>{body}</pre>", mimetype="text/html")
    if not os.path.isfile(full):
        abort(404)
    return send_from_directory(root, os.path.relpath(full, root), conditional=True)


# ------------------------------------------------------------------ pages
@bp.route("/packages")
@auth.login_required
def packages_page():
    return render_template("packages.html", repos=REPOS, products=catalog.PRODUCTS)


# ------------------------------------------------------------------ upload API
@bp.route("/api/packages/uploads", methods=["POST"])
@auth.role_required("operator")
def create_upload():
    j = request.get_json(silent=True) or {}
    name, repo = os.path.basename(str(j.get("name", ""))), j.get("repo", "")
    errs = {}
    if not NAME_RE.match(name) or name != j.get("name"):
        errs["name"] = "file name may only contain letters, digits and . _ + -"
    elif not (kind_of(name) or name.lower().endswith(SINGLE_EXT)):
        errs["name"] = "supported: .zip .tar(.gz/.xz/.bz2) .iso .deb .rpm .whl .qcow2(.bz2) .img .gguf"
    if repo not in REPOS:
        errs["repo"] = f"repo must be one of {', '.join(REPOS)}"
    try:
        size = int(j.get("size", -1))
        if size <= 0:
            errs["size"] = "size must be > 0"
    except (TypeError, ValueError):
        errs["size"] = "size must be a number"
        size = 0
    sha = str(j.get("sha256", "")).lower()
    if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
        errs["sha256"] = "sha256 must be 64 hex characters"
    if errs:
        return jsonify(error="validation failed", fields=errs), 400
    free = shutil.disk_usage(upload_dir()).free
    if size * 2.2 > free:
        return jsonify(error=f"not enough disk space: need ~{size * 2.2 / 1e9:.1f} GB (upload + extraction), {free / 1e9:.1f} GB free"), 507
    uid = db.ex("INSERT INTO uploads(name,repo,size,sha256,state,created,by_user) VALUES(?,?,?,?,?,?,?)",
                (name, repo, size, sha, "uploading", time.time(), g.user["username"]))
    path = os.path.join(upload_dir(), f"{uid}.part")
    open(path, "wb").close()
    db.ex("UPDATE uploads SET path=? WHERE id=?", (path, uid))
    auth.audit("upload.start", name, f"repo={repo} size={size}")
    return jsonify(id=uid, chunk_size=8 * 1024 * 1024), 201


@bp.route("/api/packages/uploads/<int:uid>/chunk", methods=["PUT"])
@auth.role_required("operator")
def upload_chunk(uid):
    u = db.one("SELECT * FROM uploads WHERE id=?", (uid,))
    if not u:
        return jsonify(error="no such upload"), 404
    if u["state"] != "uploading":
        return jsonify(error=f"upload is {u['state']}"), 409
    try:
        off = int(request.args.get("offset", -1))
    except ValueError:
        off = -1
    cur = os.path.getsize(u["path"])
    if off != cur:
        return jsonify(error="offset mismatch - resume from 'received'", received=cur), 409
    data = request.get_data(cache=False)
    if cur + len(data) > u["size"]:
        return jsonify(error="more data than the declared size"), 400
    with open(u["path"], "ab") as fh:
        fh.write(data)
    db.ex("UPDATE uploads SET received=? WHERE id=?", (cur + len(data), uid))
    return jsonify(received=cur + len(data))


@bp.route("/api/packages/uploads/<int:uid>/finish", methods=["POST"])
@auth.role_required("operator")
def upload_finish(uid):
    u = db.one("SELECT * FROM uploads WHERE id=?", (uid,))
    if not u:
        return jsonify(error="no such upload"), 404
    if u["state"] != "uploading":
        return jsonify(error=f"upload is {u['state']}"), 409
    got = os.path.getsize(u["path"])
    if got != u["size"]:
        return jsonify(error=f"incomplete: {got} of {u['size']} bytes", received=got), 409
    db.ex("UPDATE uploads SET state='queued' WHERE id=?", (uid,))
    jid = jobs.start("extract", f"Extract & verify {u['name']}", [{"name": f"process {u['name']}", "fn": "process_upload",
                     "args": {"upload_id": uid}}], meta={"upload_id": uid, "repo": u["repo"]})
    auth.audit("upload.finish", u["name"], f"job {jid}")
    return jsonify(job_id=jid), 202


@bp.route("/api/packages/uploads")
@auth.login_required
def list_uploads():
    rows = db.q("SELECT id,name,repo,size,sha256,received,state,message,created,finished,by_user,files FROM uploads ORDER BY id DESC LIMIT 200")
    for r in rows:
        j = db.one("SELECT id FROM jobs WHERE kind='extract' AND meta LIKE ? ORDER BY id DESC", (f'%"upload_id": {r["id"]},%',))
        r["job_id"] = j["id"] if j else None
    return jsonify(uploads=rows)


@bp.route("/api/packages/uploads/<int:uid>", methods=["DELETE"])
@auth.role_required("operator")
def delete_upload(uid):
    u = db.one("SELECT * FROM uploads WHERE id=?", (uid,))
    if not u:
        return jsonify(error="no such upload"), 404
    if u["state"] in ("verifying", "extracting", "indexing", "queued"):
        return jsonify(error="upload is being processed"), 409
    if u["path"] and os.path.exists(u["path"]):
        os.unlink(u["path"])
    db.ex("DELETE FROM uploads WHERE id=?", (uid,))
    auth.audit("upload.delete", u["name"])
    return jsonify(ok=True)


# ------------------------------------------------------------------ repository API
@bp.route("/api/packages/repo")
@auth.login_required
def repo_summary():
    out = []
    for r, label in REPOS.items():
        s = db.one("SELECT COUNT(*) n, COALESCE(SUM(size),0) sz, SUM(kind='deb') deb, SUM(kind='rpm') rpm, SUM(kind IN ('file','whl')) files "
                   "FROM repo_items WHERE repo=?", (r,))
        out.append({"repo": r, "label": label, "items": s["n"], "size": s["sz"], "deb": s["deb"] or 0, "rpm": s["rpm"] or 0,
                    "files": s["files"] or 0, "url_deb": f"/repo/{r}/deb", "url_rpm": f"/repo/{r}/rpm"})
    return jsonify(repos=out, root=repo_root())


@bp.route("/api/packages/items")
@auth.login_required
def repo_items():
    sql, args, wh = "SELECT id,repo,kind,name,version,arch,path,size,sha256 FROM repo_items", [], []
    if request.args.get("repo"):
        wh.append("repo=?")
        args.append(request.args["repo"])
    if request.args.get("q"):
        wh.append("name LIKE ?")
        args.append(f"%{request.args['q']}%")
    if request.args.get("kind"):
        wh.append("kind=?")
        args.append(request.args["kind"])
    if wh:
        sql += " WHERE " + " AND ".join(wh)
    lim = min(5000, int(request.args.get("limit", 500)))
    return jsonify(items=db.q(sql + f" ORDER BY repo,name LIMIT {lim}", args))


def available_names():
    """package names in the master repos + the virtual names they Provide (qemu-kvm is provided by qemu-system-x86 on 24.04)"""
    have = set()
    for r in db.q("SELECT name, provides FROM repo_items WHERE kind IN ('deb','rpm')"):
        have.add(r["name"])
        have.update((r["provides"] or "").split())
    return have


def presence(product, family):
    have = available_names()
    files = [r["path"] for r in db.q("SELECT path FROM repo_items WHERE kind IN ('file','whl')")]
    out = []
    for c in catalog.required(product, family):
        if c["kind"] == "file":
            ok = any(fnmatch.fnmatch(os.path.basename(f), os.path.basename(c["pattern"])) or fnmatch.fnmatch(f, "*" + c["pattern"]) for f in files)
            out.append(dict(c, present=ok, missing=[] if ok else [c["pattern"]]))
        else:
            miss = [n for n in c["names"] if n not in have]
            out.append(dict(c, present=not miss, missing=miss))
    return out


@bp.route("/api/packages/required/<product>")
@auth.login_required
def required(product):
    if product not in catalog.PRODUCTS:
        return jsonify(error="unknown product"), 404
    fam = request.args.get("family", "debian")
    if fam not in ("debian", "rhel"):
        return jsonify(error="family must be debian or rhel"), 400
    items = presence(product, fam)
    return jsonify(product=product, family=fam, items=items, ready=all(i["present"] for i in items))


@bp.route("/api/packages/deps/<repo>")
@auth.login_required
def deps(repo):
    if repo not in REPOS:
        return jsonify(error="unknown repo"), 404
    return jsonify(missing=scan_deps(repo))


@bp.route("/api/packages/reindex/<repo>", methods=["POST"])
@auth.role_required("operator")
def reindex_api(repo):
    if repo not in REPOS:
        return jsonify(error="unknown repo"), 404
    jid = jobs.start("extract", f"Re-index repository {repo}", [{"name": f"reindex {repo}", "fn": "reindex_job", "args": {"repo": repo}}])
    auth.audit("repo.reindex", repo)
    return jsonify(job_id=jid), 202


@jobs.register("reindex_job")
def reindex_job(log, repo):
    reindex(repo, log)
    return True


@bp.route("/api/packages/disk")
@auth.login_required
def disk():
    return jsonify(disk_usage())


@bp.route("/api/packages/verify/<repo>", methods=["POST"])
@auth.role_required("operator")
def verify_repo(repo):
    if repo not in REPOS:
        return jsonify(error="unknown repo"), 404
    bad, n = [], 0
    for r in db.q("SELECT path, sha256 FROM repo_items WHERE repo=? AND sha256!=''", (repo,)):
        p = os.path.join(repo_root(), repo, r["path"])
        n += 1
        if not os.path.exists(p) or sha256_file(p) != r["sha256"]:
            bad.append(r["path"])
    auth.audit("repo.verify", repo, f"{n} checked, {len(bad)} bad", ok=not bad)
    return jsonify(checked=n, bad=bad, ok=not bad)


# ------------------------------------------------------------------ online download kit
KIT_SCRIPT = r"""#!/usr/bin/env bash
# AutoInfra download kit - run on an INTERNET-connected machine with zip and either docker, or NO_DOCKER=1 as root on the
# same OS as the nodes (Ubuntu 24.04 for .deb, RHEL/Rocky/Alma 9 for .rpm). Without docker nothing is installed on this
# machine: packages are resolved against an EMPTY package database, so everything is downloaded - also what is installed here.
# Produces {zipname}; upload it in AutoInfra > Package Repository > Upload Packages (repo: {repo}).
set -Eeuo pipefail
OUT="$(pwd)/{zipname}"
KIT_TMP=""; if [ -z "${{STAGE:-}}" ]; then STAGE="$(mktemp -d)"; KIT_TMP="$STAGE"; fi
# always leave this machine's apt/dnf sources as they were (also when a download fails)
trap 'rm -f /etc/apt/sources.list.d/autoinfra-kit-*.list /etc/apt/trusted.gpg.d/autoinfra-kit-*.gpg /etc/yum.repos.d/autoinfra-kit-*.repo 2>/dev/null; [ -n "$KIT_TMP" ] && rm -rf "$KIT_TMP"' EXIT
W="$STAGE/airgap/www/{repo}"; mkdir -p "$W/{sub}"
PKGS="{pkgs}"
FAMILY="{family}"
fetch_debs() {{
  {deb_repos}
  apt-get update -qq
  # empty dpkg status = "nothing is installed": apt resolves and downloads the COMPLETE dependency set, the same on a
  # used machine as in a clean container; virtual packages resolve to their provider
  : > "$STAGE/empty-status"; E="-o Dir::State::status=$STAGE/empty-status"
  ok=""; for p in $PKGS; do
    if apt-get $E install -s -qq --no-install-recommends "$p" >/dev/null 2>&1; then ok="$ok $p"; else echo "NOT AVAILABLE (add the vendor repository on this machine): $p"; fi
  done
  apt-get $E install -y -qq --download-only --no-install-recommends -o Dir::Cache::archives="$W/deb" $ok
  rm -f "$STAGE/empty-status"
  rm -rf "$W/deb/partial" "$W/deb/lock"
}}
fetch_rpms() {{
  dnf -q -y install 'dnf-command(download)' >/dev/null 2>&1 || true
  {rpm_repos}
  ok=""; for p in $PKGS; do dnf -q repoquery --whatprovides "$p" 2>/dev/null | grep -q . && ok="$ok $p" || echo "NOT AVAILABLE (add the vendor repository on this machine): $p"; done
  dnf -q -y download --resolve --alldeps --destdir "$W/rpm" $ok
}}
if [ "${{NO_DOCKER:-0}}" = "1" ]; then
  if [ "$FAMILY" = "rhel" ]; then fetch_rpms; else fetch_debs; fi
else
  img="{image}"
  docker run --rm -v "$STAGE:/stage" -e STAGE=/stage -e PKGS="$PKGS" -e FAMILY="$FAMILY" -e NO_DOCKER=1 -v "$(readlink -f "$0"):/kit.sh:ro" "$img" bash -c \
    'cd /stage && OUT=/dev/null bash /kit.sh --inner'
fi
[ "${{1:-}}" = "--inner" ] && exit 0
{files}
(cd "$STAGE" && zip -r -0 -q "$OUT" airgap)
sha256sum "$OUT" | tee "$OUT.sha256"
echo "DONE: $OUT"
"""


def build_kit(product, family, os_version, version, include_deps=True, include_templates=True):
    p = catalog.PRODUCTS[product]
    pkgs = catalog.packages_for(product, family)
    repo = p["repo"]
    image = ("rockylinux:9" if family == "rhel" else f"ubuntu:{os_version or '24.04'}")
    rpm_repos = ""
    if family == "rhel":
        rpm_repos = "dnf -q -y install epel-release >/dev/null 2>&1 || true"
        if product == "cloudstack":
            rpm_repos += (f"\n  printf '[cloudstack]\\nname=cloudstack\\nbaseurl=http://download.cloudstack.org/el/9/{version.rsplit('.', 1)[0]}/\\n"
                          "enabled=1\\ngpgcheck=0\\n' > /etc/yum.repos.d/autoinfra-kit-cloudstack.repo")
    deb_repos = ""
    if family == "debian" and product == "cloudstack":
        deb_repos = ("apt-get update -qq && apt-get install -y -qq curl gnupg ca-certificates >/dev/null; "
                     "curl -fsSL http://download.cloudstack.org/release.asc | gpg --dearmor > /etc/apt/trusted.gpg.d/autoinfra-kit-cloudstack.gpg; "
                     f"echo 'deb http://download.cloudstack.org/ubuntu noble {version.rsplit('.', 1)[0]}' > /etc/apt/sources.list.d/autoinfra-kit-cloudstack.list")
    files = ""
    if product == "cloudstack" and include_templates:
        series = version.rsplit(".", 1)[0]
        files = (f'mkdir -p "$W/systemvm"; curl -fL --retry 5 -o "$W/systemvm/systemvmtemplate-{series}.0-x86_64-kvm.qcow2.bz2" '
                 f'"http://download.cloudstack.org/systemvm/{series}/systemvmtemplate-{series}.0-x86_64-kvm.qcow2.bz2"')
    zipname = f"{product}-{version}-{family}{os_version.replace('.', '') if os_version else ''}-packages.zip"
    script = KIT_SCRIPT.format(zipname=zipname, repo=repo, sub="rpm" if family == "rhel" else "deb", pkgs=" ".join(pkgs),
                               family=family, image=image, rpm_repos=rpm_repos, deb_repos=deb_repos, files=files)
    return zipname, script, pkgs


@bp.route("/api/packages/online/manifest", methods=["POST"])
@auth.login_required
def online_manifest():
    j = request.get_json(silent=True) or {}
    product = j.get("product", "cloudstack")
    if product not in catalog.PRODUCTS:
        return jsonify(error="unknown product"), 400
    fam = "rhel" if str(j.get("os", "ubuntu-24.04")).startswith(("rocky", "alma", "rhel", "ol")) else "debian"
    comps = catalog.required(product, fam)
    sel = set(j.get("components") or [c["id"] for c in comps])
    comps = [c for c in comps if c["id"] in sel]
    total = sum(c["size_mb"] for c in comps)
    return jsonify(product=product, family=fam, components=comps, total_mb=total,
                   online_enabled=bool(db.get_setting("online_mode", False)))


@bp.route("/api/packages/online/kit", methods=["POST"])
@auth.login_required
def online_kit():
    j = request.get_json(silent=True) or {}
    product = j.get("product", "cloudstack")
    if product not in catalog.PRODUCTS:
        return jsonify(error="unknown product"), 400
    os_sel = str(j.get("os", "ubuntu-24.04"))
    fam = "rhel" if os_sel.startswith(("rocky", "alma", "rhel", "ol")) else "debian"
    osv = os_sel.split("-", 1)[1] if "-" in os_sel else ""
    ver = j.get("version") or catalog.PRODUCTS[product]["versions"][0]
    if ver not in catalog.PRODUCTS[product]["versions"] and V.version(ver):
        return jsonify(error=V.version(ver)), 400
    zipname, script, pkgs = build_kit(product, fam, osv, ver, include_templates=j.get("templates", True))
    auth.audit("online.kit", product, f"{fam} {ver}")
    return Response(script, mimetype="text/x-shellscript",
                    headers={"Content-Disposition": f"attachment; filename=download-{product}-{fam}.sh", "X-Zip-Name": zipname})


@bp.route("/api/packages/online/fetch", methods=["POST"])
@auth.role_required("operator")
def online_fetch():
    """Only when Settings > 'Master has internet' is on: runs the kit on the master itself as a job."""
    if not db.get_setting("online_mode", False):
        return jsonify(error="online mode is off (air-gapped). Download the kit and run it on an internet-connected machine."), 409
    j = request.get_json(silent=True) or {}
    product = j.get("product", "cloudstack")
    if product not in catalog.PRODUCTS:
        return jsonify(error="unknown product"), 400
    os_sel = str(j.get("os", "ubuntu-24.04"))
    fam = "rhel" if os_sel.startswith(("rocky", "alma", "rhel", "ol")) else "debian"
    ver = j.get("version") or catalog.PRODUCTS[product]["versions"][0]
    zipname, script, _ = build_kit(product, fam, os_sel.split("-", 1)[-1], ver)
    wd = tempfile.mkdtemp(prefix="kit-", dir=upload_dir())
    sp = os.path.join(wd, "kit.sh")
    open(sp, "w").write(script)
    env = {"NO_DOCKER": "1"} if j.get("no_docker") else {}
    jid = jobs.start("download", f"Online download {product} {ver} ({fam})",
                     [{"name": "download packages", "cmd": ["bash", sp], "cwd": wd, "env": env},
                      {"name": "import into repository", "fn": "import_local_zip", "args": {"path": os.path.join(wd, zipname), "repo": catalog.PRODUCTS[product]["repo"]}}])
    auth.audit("online.fetch", product, f"job {jid}")
    return jsonify(job_id=jid), 202


@jobs.register("import_local_zip")
def import_local_zip(log, path, repo):
    size = os.path.getsize(path)
    uid = db.ex("INSERT INTO uploads(name,repo,size,sha256,state,created,by_user,path,received) VALUES(?,?,?,?,?,?,?,?,?)",
                (os.path.basename(path), repo, size, "", "queued", time.time(), "online", path, size))
    return process_upload(log, uid)


@bp.route("/api/packages/online/manifest-catalog")
@auth.login_required
def online_catalog():
    return jsonify({k: p["versions"] for k, p in catalog.PRODUCTS.items()})
