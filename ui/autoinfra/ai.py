"""Ollama AI assistant: answers with the live infrastructure as context (hosts, metrics, alerts, components, failed jobs,
knowledge-base documents). Runs against a local Ollama server - nothing leaves the air-gapped network."""
import json, os, re, time, urllib.error, urllib.request

from flask import Blueprint, g, jsonify, render_template, request

from . import auth, components, db, monitoring

bp = Blueprint("ai", __name__)
QUICK = ["Explain the health status of my infrastructure", "Show recent errors", "Check database status",
         "Explain cluster topology", "Which host has the highest CPU usage and why?", "Generate a PDF report of all components"]
MAX_KB_CHARS = 200_000


def ollama_url():
    return (db.get_setting("ollama_url") or os.environ.get("AUTOINFRA_OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")


def ollama(path, payload=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(ollama_url() + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def context_text(question=""):
    hosts = db.q("SELECT id, ip, hostname, roles, status, os_pretty FROM hosts ORDER BY id")
    lm = monitoring.latest()
    lines = ["# Hosts"]
    for h in hosts:
        m = lm.get(h["id"], {})
        met = f" cpu={m['cpu']:.0f}% mem={m['mem']:.0f}% disk={m['disk']:.0f}% load={m['load1']}" if m.get("cpu") is not None else ""
        lines.append(f"- {h['hostname'] or h['ip']} ({h['ip']}) roles={h['roles'] or '-'} os={h['os_pretty'] or '?'} status={h['status']}{met}")
    lines.append("# Open alerts")
    for a in db.q("SELECT severity, message FROM alerts WHERE resolved=0 ORDER BY id DESC LIMIT 30"):
        lines.append(f"- [{a['severity']}] {a['message']}")
    lines.append("# Components")
    for c in db.q("SELECT id, state, hosts FROM components"):
        lines.append(f"- {components.COMPONENTS.get(c['id'], {}).get('name', c['id'])}: {c['state']} on hosts {c['hosts']}")
    lines.append("# Recent failed jobs (last lines of the log)")
    for j in db.q("SELECT id, title, log_path FROM jobs WHERE state='failed' ORDER BY id DESC LIMIT 3"):
        try:
            tail = open(j["log_path"], errors="replace").read().splitlines()[-15:]
        except OSError:
            tail = []
        lines.append(f"- job #{j['id']} {j['title']}:\n    " + "\n    ".join(tail))
    ov = monitoring.overview()
    lines.append(f"# Totals: hosts {ov['hosts']['total']} ({ov['hosts']['online']} online), VMs {ov['vms']['total']}, "
                 f"alerts critical={ov['alerts']['critical']} warning={ov['alerts']['warning']}, avg cpu {ov['gauges']['cpu']}%, "
                 f"avg mem {ov['gauges']['mem']}%, avg disk {ov['gauges']['disk']}%")
    words = {w for w in re.findall(r"[a-z0-9_-]{4,}", question.lower())}
    kb = []
    for d in db.q("SELECT name, content FROM kb_docs"):
        score = sum(d["content"].lower().count(w) for w in words)
        if score:
            kb.append((score, d))
    kb.sort(key=lambda x: -x[0])
    if kb:
        lines.append("# Knowledge base excerpts")
        for _, d in kb[:3]:
            lines.append(f"## {d['name']}\n{d['content'][:3000]}")
    return "\n".join(lines)


SYSTEM = ("You are the AutoInfra assistant for an air-gapped Apache CloudStack / OpenStack platform. Answer using ONLY the live data "
          "below; say clearly when the data does not contain the answer. Be concise and practical; suggest concrete next steps "
          "(which page in AutoInfra, which command). Never invent hosts, numbers or log lines.\n\n")


def wants_report(msg):
    m = msg.lower()
    if "report" in m and ("pdf" in m or "excel" in m or "xlsx" in m or "generate" in m):
        return "xlsx" if ("excel" in m or "xlsx" in m) else "pdf"
    return None


@bp.route("/ai")
@auth.login_required
def page():
    return render_template("ai.html", quick=QUICK)


@bp.route("/api/ai/models")
@auth.login_required
def models():
    try:
        r = ollama("/api/tags", timeout=5)
        return jsonify(ok=True, url=ollama_url(), models=[m.get("name") for m in r.get("models", [])],
                       selected=db.get_setting("ollama_model") or "llama3.1:8b")
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, url=ollama_url(), error=f"Ollama not reachable: {e}", models=[], selected=db.get_setting("ollama_model") or "")


@bp.route("/api/ai/chat", methods=["POST"])
@auth.login_required
def chat():
    j = request.get_json(silent=True) or {}
    msg = str(j.get("message", "")).strip()
    if not msg:
        return jsonify(error="message is empty"), 400
    if len(msg) > 4000:
        return jsonify(error="message too long (max 4000 characters)"), 400
    model = j.get("model") or db.get_setting("ollama_model") or "llama3.1:8b"
    user = g.user["username"]
    db.ex("INSERT INTO chats(ts,user,role,content) VALUES(?,?,?,?)", (time.time(), user, "user", msg))
    report = None
    fmt = wants_report(msg)
    if fmt:
        from . import reports
        rid = reports.generate("infra", fmt, user)
        report = {"id": rid, "fmt": fmt, "url": f"/api/reports/{rid}/download"}
    hist = db.q("SELECT role, content FROM chats WHERE user=? ORDER BY id DESC LIMIT 8", (user,))[::-1]
    messages = [{"role": "system", "content": SYSTEM + context_text(msg)}] + [{"role": h["role"], "content": h["content"]} for h in hist]
    try:
        r = ollama("/api/chat", {"model": model, "messages": messages, "stream": False, "options": {"temperature": 0.2}},
                   timeout=float(db.get_setting("ollama_timeout", 180)))
        answer = (r.get("message") or {}).get("content", "").strip() or "(empty answer)"
        ok = True
    except urllib.error.HTTPError as e:
        answer, ok = f"Ollama returned HTTP {e.code}: {e.read().decode(errors='replace')[:300]}", False
    except Exception as e:  # noqa: BLE001
        answer, ok = f"Ollama is not reachable at {ollama_url()} ({e}). Deploy 'Ollama AI Platform' in Components or set the URL in Settings.", False
    if report:
        answer += f"\n\nThe {fmt.upper()} report is ready: Reports > #{report['id']}."
    db.ex("INSERT INTO chats(ts,user,role,content) VALUES(?,?,?,?)", (time.time(), user, "assistant", answer))
    auth.audit("ai.chat", model, msg[:120], ok=ok)
    return jsonify(answer=answer, ok=ok, model=model, report=report)


@bp.route("/api/ai/history")
@auth.login_required
def history():
    return jsonify(messages=db.q("SELECT ts, role, content FROM chats WHERE user=? ORDER BY id DESC LIMIT 100", (g.user["username"],))[::-1])


@bp.route("/api/ai/history", methods=["DELETE"])
@auth.login_required
def clear_history():
    db.ex("DELETE FROM chats WHERE user=?", (g.user["username"],))
    return jsonify(ok=True)


@bp.route("/api/ai/kb")
@auth.login_required
def kb_list():
    return jsonify(docs=db.q("SELECT id, name, length(content) AS size, created, by_user FROM kb_docs ORDER BY id DESC"))


@bp.route("/api/ai/kb", methods=["POST"])
@auth.role_required("operator")
def kb_add():
    if "file" in request.files:
        f = request.files["file"]
        name, content = os.path.basename(f.filename or "doc.txt"), f.read().decode("utf-8", errors="replace")
    else:
        j = request.get_json(silent=True) or {}
        name, content = str(j.get("name", "")).strip(), str(j.get("content", ""))
    if not name or not content.strip():
        return jsonify(error="name and content are required"), 400
    if len(content) > MAX_KB_CHARS:
        return jsonify(error=f"document too large (max {MAX_KB_CHARS} characters)"), 413
    did = db.ex("INSERT INTO kb_docs(name, content, created, by_user) VALUES(?,?,?,?)", (name[:120], content, time.time(), g.user["username"]))
    auth.audit("ai.kb.add", name)
    return jsonify(id=did), 201


@bp.route("/api/ai/kb/<int:did>", methods=["DELETE"])
@auth.role_required("operator")
def kb_delete(did):
    if not db.one("SELECT id FROM kb_docs WHERE id=?", (did,)):
        return jsonify(error="no such document"), 404
    db.ex("DELETE FROM kb_docs WHERE id=?", (did,))
    auth.audit("ai.kb.delete", did)
    return jsonify(ok=True)
