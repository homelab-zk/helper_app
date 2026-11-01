#!/usr/bin/env python3
"""One chat helper: a chat web app in front of the Claude Code CLI, answering from its own Markdown files.

Usage: python3 helper.py <helper folder>
The folder holds helper.json (name, purpose, port), docs/ (the Markdown files Claude may read) and
chats.db. manager.py starts one of these per helper; name, purpose and files are re-read on every
request, so only a port change needs a restart.

Each question runs `claude -p` (headless) in docs/, read-only (Read/Grep/Glob only), and resumes the
chat's Claude session for follow-ups. Standard library only.
"""
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from common import (ICONS, USERS_TABLE, BaseHandler, admin_user, check_login, html_escape, init_sessions,
                    read_credential, sql as _sql, verify_password)

HELPER_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
CONFIG_FILE = HELPER_DIR / "helper.json"
DOCS_DIR = HELPER_DIR / "docs"
DB_PATH = HELPER_DIR / "chats.db"
HOST = os.environ.get("CONV_HELPER_HOST", "0.0.0.0")
CLAUDE_BIN = os.environ.get("CONV_HELPER_CLAUDE", str(Path.home() / ".local/bin/claude"))
DEFAULT_MODEL = os.environ.get("CONV_HELPER_MODEL", "opus")
MODELS = {"opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku"}  # aliases: always the latest version
EFFORTS = ["low", "medium", "high", "xhigh", "max"]  # empty: Claude Code's default for the model
TIMEOUT_S = 600
HISTORY_FALLBACK_MESSAGES = 20

PROMPT = """You are "{name}", a helper for one person, reached through a small chat web app.

What this helper is for (written by its owner; follow it):
{purpose}

The working directory holds this helper's reference documents{files}

Find what you need with Grep and Glob and Read only the relevant parts; don't read whole large files.

How to answer:
- Base answers on these documents and say where each part comes from (document title, section or heading).
  Don't cite line numbers.
- If the documents don't cover something, say so. You may add general knowledge, but label it clearly as not
  from the documents.
- Reply in the language of the question. Use Markdown: short paragraphs, lists, and tables where they help."""

# added to the instructions when the user switches on web search for a question
WEB_NOTE = """

Web search is switched on for this question. Use WebSearch when the documents don't cover the question or it
needs current information. Still use the documents for anything they cover. In the answer, say which parts come
from the web and give the source links.
Search queries go to an external search service: never put text copied from the documents into a query; use
short topic keywords."""

RUNNING = {}  # chat id -> Popen (None while starting)
STOPPED = set()  # chat ids whose run was stopped by the user
LOCK = threading.Lock()


def sql(statement, args=()):
    return _sql(DB_PATH, statement, args)


def config():
    return json.loads(CONFIG_FILE.read_text())


def users():
    """{name: {"hash", "created", "changed"}}: this helper's own users, managed in the admin page.
    Read on every request, so added/removed users and new passwords apply at once."""
    rows, _ = sql("SELECT name, hash, created, changed FROM users")
    return {r["name"]: r for r in rows}


def find_user(name):
    rows, _ = sql("SELECT name, hash, changed FROM users WHERE name = ?", (name,))  # name is NOCASE
    return rows[0] if rows else None


def doc_files():
    return sorted(p.name for p in DOCS_DIR.iterdir() if p.is_file() and not p.name.startswith("."))


def system_prompt(web):
    cfg = config()
    files = doc_files()
    listing = (":\n" + "\n".join(f"- `{f}`" for f in files)) if files else \
        ", but there are none yet: say so, and answer from general knowledge, clearly labelled."
    return PROMPT.format(name=cfg["name"], purpose=cfg["purpose"].strip(), files=listing) + (WEB_NOTE if web else "")


# ---------- storage ----------
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, session_id TEXT,
                created REAL NOT NULL, updated REAL NOT NULL, model TEXT, effort TEXT);
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, tools TEXT, created REAL NOT NULL, model TEXT, effort TEXT, web TEXT);
            CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id);
        """)
        conn.execute(USERS_TABLE)
        if "owner" not in [r[1] for r in conn.execute("PRAGMA table_info(chats)")]:
            # added with per-helper users: each user sees only their own chats; older chats are the admin's
            conn.execute("ALTER TABLE chats ADD COLUMN owner TEXT")
            conn.execute("UPDATE chats SET owner = ?", (admin_user(read_credential()),))
    init_sessions(DB_PATH)


def get_chat(chat_id, owner):
    rows, _ = sql("SELECT * FROM chats WHERE id = ? AND owner = ?", (chat_id, owner))
    return rows[0] if rows else None


def add_message(chat_id, role, content, tools=None, model=None, effort=None, web=False):
    now = time.time()
    sql("INSERT INTO messages (chat_id, role, content, tools, model, effort, web, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (chat_id, role, content, json.dumps(tools) if tools else None, model, effort, "1" if web else None, now))
    sql("UPDATE chats SET updated = ? WHERE id = ?", (now, chat_id))


# ---------- claude CLI ----------
def claude_cmd(session_id, model, effort, web):
    # web search only, never WebFetch: Claude sees search results but can't open (or send data to) any URL
    tools = "Read,Grep,Glob,WebSearch" if web else "Read,Grep,Glob"
    cmd = [CLAUDE_BIN, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
           "--restricted", "--strict-mcp-config", "--disable-slash-commands", "--tools", tools,
           "--append-system-prompt", system_prompt(web)]
    if web:
        cmd += ["--allowedTools", "WebSearch"]  # headless: no one to approve it
    cmd += ["--model", model or DEFAULT_MODEL]
    if effort:
        cmd += ["--effort", effort]
    if session_id:
        cmd += ["--resume", session_id]
    return cmd


def describe_tool(name, inp):
    if name == "Grep":
        return f"Searching for “{inp.get('pattern', '')}”"
    if name == "Read":
        where = Path(inp.get("file_path", "")).stem or "a file"
        if inp.get("offset") and inp.get("limit"):
            where += f", lines {inp['offset']}–{inp['offset'] + inp['limit']}"
        return f"Reading {where}"
    if name == "WebSearch":
        return f"Searching the web for “{inp.get('query', '')}”"
    if name == "Glob":
        return f"Listing files {inp.get('pattern', '')}".strip()
    return name


def history_prompt(chat_id, question):
    """Prompt for a fresh session when the chat's Claude session can't be resumed."""
    rows, _ = sql("SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,))
    earlier = rows[:-1][-HISTORY_FALLBACK_MESSAGES:]  # the last row is the new question
    lines = ["Earlier conversation in this chat, for context:", ""]
    lines += [f"{'Question' if r['role'] == 'user' else 'Answer'}: {r['content']}\n" for r in earlier]
    return "\n".join(lines + ["New question:", question])


def stop_run(chat_id):
    with LOCK:
        proc = RUNNING.get(chat_id)
        if not proc:
            return False
        STOPPED.add(chat_id)
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return True


# ---------- HTTP ----------
class Handler(BaseHandler):
    server_version = "ConvHelper/1.0"
    DB = DB_PATH
    COOKIE = "h_" + re.sub(r"[^a-z0-9]", "", HELPER_DIR.name.lower())  # cookies ignore ports: one name per helper
    PUBLIC = {
        "/login": ("login.html", "text/html; charset=utf-8"),
        "/style.css": ("style.css", "text/css; charset=utf-8"),
        "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
        **ICONS,
    }
    STATIC = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/vendor/marked.min.js": ("vendor/marked.min.js", "text/javascript; charset=utf-8"),
        "/vendor/purify.min.js": ("vendor/purify.min.js", "text/javascript; charset=utf-8"),
    }
    CREDENTIAL = read_credential()

    # the admin login works on every helper; this helper's own users only here
    def check_credentials(self, username, password):
        if self.CREDENTIAL and check_login(self.CREDENTIAL, username, password):
            return True
        u = find_user(username)
        return u["name"] if u and verify_password(password, u["hash"]) else False

    def session_ok(self, user, created):
        if self.CREDENTIAL and user == admin_user(self.CREDENTIAL):
            return True
        u = find_user(user or "")
        return bool(u) and u["name"] == user and created >= u["changed"]  # removed user or new password: logged out

    def login_required(self):
        return bool(self.CREDENTIAL or users())

    def do_GET(self):
        url = urlparse(self.path)
        cfg = config()
        if url.path == "/manifest.webmanifest":  # public: installing needs it
            short = cfg["name"] if len(cfg["name"]) <= 12 else cfg["name"][:12].rstrip()
            return self.send_body(json.dumps({
                "id": "/", "name": cfg["name"], "short_name": short, "description": cfg["purpose"][:200],
                "start_url": "/", "scope": "/", "display": "standalone",
                "background_color": "#f6f7f9", "theme_color": "#2f5bd3",
                "icons": [
                    {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                    {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                    {"src": "/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
                    {"src": "/icons/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]}).encode(),
                "application/manifest+json")
        if self.serve_public(url.path, url.query, cfg["name"]):
            return
        if not self.authorized():
            return
        path = url.path
        if path in self.STATIC:
            self.send_file(*self.STATIC[path], replace=[(b"{{TITLE}}", html_escape(cfg["name"]).encode())])
        elif path == "/api/options":
            self.send_json({"name": cfg["name"], "purpose": cfg["purpose"], "files": doc_files(), "user": self.user,
                            "models": [{"id": k, "label": v} for k, v in MODELS.items()],
                            "efforts": EFFORTS, "default_model": DEFAULT_MODEL if DEFAULT_MODEL in MODELS else "opus"})
        elif path == "/api/chats":
            rows, _ = sql("""SELECT c.id, c.title, c.created, c.updated, COUNT(m.id) AS messages
                             FROM chats c LEFT JOIN messages m ON m.chat_id = c.id WHERE c.owner = ?
                             GROUP BY c.id ORDER BY c.updated DESC""", (self.user,))
            with LOCK:
                for r in rows:
                    r["running"] = r["id"] in RUNNING
            self.send_json(rows)
        elif m := re.fullmatch(r"/api/chats/(\d+)", path):
            chat = get_chat(int(m.group(1)), self.user)
            if not chat:
                return self.send_json({"error": "chat not found"}, 404)
            msgs, _ = sql("SELECT role, content, tools, model, effort, web, created FROM messages WHERE chat_id = ? ORDER BY id",
                          (chat["id"],))
            for msg in msgs:
                msg["tools"] = json.loads(msg["tools"]) if msg["tools"] else []
            with LOCK:
                chat["running"] = chat["id"] in RUNNING
            self.send_json({**chat, "messages": msgs})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            return self.login()
        if path == "/logout":
            return self.logout()
        if not self.authorized():
            return
        try:
            data = self.read_json()
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        if path == "/api/chats":
            title = " ".join(str(data.get("title") or "New chat").split())[:80]
            now = time.time()
            _, chat_id = sql("INSERT INTO chats (title, created, updated, owner) VALUES (?, ?, ?, ?)",
                             (title, now, now, self.user))
            self.send_json(get_chat(chat_id, self.user), 201)
        elif m := re.fullmatch(r"/api/chats/(\d+)/ask", path):
            question = str(data.get("question") or "").strip()
            if not question:
                return self.send_json({"error": "empty question"}, 400)
            model, effort = str(data.get("model") or ""), str(data.get("effort") or "")
            if (model and model not in MODELS) or (effort and effort not in EFFORTS):
                return self.send_json({"error": "unknown model or effort"}, 400)
            self.ask(int(m.group(1)), question, model, effort, bool(data.get("web")))
        elif m := re.fullmatch(r"/api/chats/(\d+)/stop", path):
            if not get_chat(int(m.group(1)), self.user):
                return self.send_json({"error": "chat not found"}, 404)
            self.send_json({"stopped": stop_run(int(m.group(1)))})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        if not self.authorized():
            return
        m = re.fullmatch(r"/api/chats/(\d+)", urlparse(self.path).path)
        if not m:
            return self.send_json({"error": "not found"}, 404)
        chat_id = int(m.group(1))
        if not get_chat(chat_id, self.user):
            return self.send_json({"error": "chat not found"}, 404)
        stop_run(chat_id)
        sql("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        sql("DELETE FROM chats WHERE id = ?", (chat_id,))
        self.send_json({"deleted": chat_id})

    # ----- asking: server-sent events over a POST response -----
    def emit(self, event, data):
        if self.client_gone:
            return
        try:
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.client_gone = True  # keep going: the answer is still saved

    def ask(self, chat_id, question, model, effort, web):
        chat = get_chat(chat_id, self.user)
        if not chat:
            return self.send_json({"error": "chat not found"}, 404)
        with LOCK:
            if chat_id in RUNNING:
                return self.send_json({"error": "this chat is still answering"}, 409)
            RUNNING[chat_id] = None
            STOPPED.discard(chat_id)
        try:
            self.client_gone = False
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")  # nginx: pass the stream through unbuffered
            self.end_headers()
            add_message(chat_id, "user", question)
            sql("UPDATE chats SET model = ?, effort = ? WHERE id = ?", (model, effort, chat_id))

            run = self.run_claude(chat_id, chat["session_id"], question, model, effort, web)
            if chat["session_id"] and not run["session_id"] and chat_id not in STOPPED:
                self.emit("status", {"text": "Earlier Claude session not found; continuing from the saved chat."})
                run = self.run_claude(chat_id, None, history_prompt(chat_id, question), model, effort, web)

            result = run["result"] or {}
            answer = result.get("result") or run["last_text"]
            if chat_id in STOPPED:
                answer = (answer + "\n\n" if answer else "") + "_Stopped._"
            elif not run["result"] or result.get("is_error"):
                detail = result.get("result") or run["stderr"].strip()[-500:] or f"exit code {run['returncode']}"
                answer = (answer + "\n\n" if answer and answer != detail else "") + f"**Error:** {detail}"
            add_message(chat_id, "assistant", answer, run["tools"], run["model"], effort, web)
            self.emit("done", {"answer": answer, "tools": run["tools"], "model": run["model"], "effort": effort, "web": web,
                               "seconds": round((result.get("duration_ms") or 0) / 1000)})
        finally:
            with LOCK:
                RUNNING.pop(chat_id, None)

    def run_claude(self, chat_id, session_id, prompt, model, effort, web):
        try:
            proc = subprocess.Popen(claude_cmd(session_id, model, effort, web), cwd=DOCS_DIR, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    start_new_session=True)
        except OSError as e:
            return {"session_id": None, "model": None, "result": None, "tools": [], "last_text": "",
                    "returncode": None, "stderr": f"could not start Claude Code: {e}"}
        with LOCK:
            RUNNING[chat_id] = proc
        stderr = []
        threading.Thread(target=lambda: stderr.append(proc.stderr.read()), daemon=True).start()
        timer = threading.Timer(TIMEOUT_S, stop_run, (chat_id,))
        timer.start()
        proc.stdin.write(prompt)
        proc.stdin.close()

        run = {"session_id": None, "model": None, "result": None, "tools": [], "last_text": ""}
        tool_index = {}  # tool_use id -> position in run["tools"]
        text = []
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "system" and ev.get("subtype") == "init":
                run["session_id"] = ev["session_id"]
                run["model"] = ev.get("model")
                sql("UPDATE chats SET session_id = ? WHERE id = ?", (ev["session_id"], chat_id))
            elif kind == "stream_event":
                e = ev.get("event", {})
                if e.get("type") == "message_start":
                    text = []
                    self.emit("turn", {})
                elif e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta":
                    text.append(e["delta"]["text"])
                    self.emit("text", {"delta": e["delta"]["text"]})
            elif kind == "assistant":
                for block in ev["message"].get("content", []):
                    if block.get("type") == "tool_use":
                        desc = describe_tool(block["name"], block.get("input", {}))
                        tool_index[block.get("id")] = len(run["tools"])
                        run["tools"].append(desc)
                        self.emit("tool", {"text": desc})
            elif kind == "user":
                # a tool call that was refused (e.g. web search while it's switched off) didn't happen
                for block in ev.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                        i = tool_index.get(block.get("tool_use_id"))
                        if i is not None and not run["tools"][i].endswith("(blocked)"):
                            run["tools"][i] += " (blocked)"
            elif kind == "result":
                run["result"] = ev
        proc.wait()
        timer.cancel()
        run.update(last_text="".join(text), returncode=proc.returncode, stderr="".join(stderr))
        return run


def main():
    if not CONFIG_FILE.is_file():
        sys.exit(f"No helper.json in {HELPER_DIR}")
    DOCS_DIR.mkdir(exist_ok=True)
    init_db()
    cfg = config()
    port = int(os.environ.get("CONV_HELPER_PORT") or cfg["port"])
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.daemon_threads = True
    # the manager stops helpers with SIGTERM: end running answers too (they run in their own process group)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    print(f"helper '{cfg['name']}' on http://{HOST}:{port}  docs={DOCS_DIR}  "
          f"login={'on' if Handler.CREDENTIAL or users() else 'OFF'}, users={len(users())}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for chat_id in list(RUNNING):
            stop_run(chat_id)


if __name__ == "__main__":
    main()
