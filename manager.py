#!/usr/bin/env python3
"""Helper admin: create and manage chat helpers, and keep each one running on its own port.

A helper is a folder data/helpers/<id>/ with helper.json (name, purpose, port, enabled), docs/ (its
Markdown files) and chats.db. This process serves the admin page and starts one helper.py process per
enabled helper, restarting it if it crashes. Deleted helpers are moved to data/trash/, not destroyed.

Standard library only. Settings: CONV_HELPER_ADMIN_PORT (8010), CONV_HELPER_HOST, CONV_HELPER_DATA,
CONV_HELPER_CREDENTIAL (user:password file shared by the admin page and every helper).
"""
import collections
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from convert import CONVERTIBLE, ConvertError, convert
import sqlite3

from common import (APP_DIR, DATA_DIR, ICONS, USERS_TABLE, BaseHandler, admin_user, atomic_write, hash_password,
                    init_sessions, read_credential, sql)

HOST = os.environ.get("CONV_HELPER_HOST", "0.0.0.0")
ADMIN_PORT = int(os.environ.get("CONV_HELPER_ADMIN_PORT", "8010"))
HELPERS_DIR = DATA_DIR / "helpers"
TRASH_DIR = DATA_DIR / "trash"
ADMIN_DB = DATA_DIR / "admin.db"
HELPER_PY = APP_DIR / "helper.py"
MAX_FILE = 20 * 1024 * 1024
MAX_CONVERT = 50 * 1024 * 1024  # PDF and DOCX uploads
FILE_NAME = re.compile(r"[\w][\w .()&+,'-]{0,119}\.(md|markdown|txt)", re.IGNORECASE)
UPLOAD_NAME = re.compile(r"[\w][\w .()&+,'-]{0,119}\.(md|markdown|txt|pdf|docx)", re.IGNORECASE)
ORIGINAL_TYPES = {".pdf": "application/pdf",
                  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
LOG_LINES = 200
USER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,39}")


# ---------- helpers on disk ----------
def helper_dir(hid):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", hid or ""):
        return None
    d = HELPERS_DIR / hid
    return d if (d / "helper.json").is_file() else None


def load(hid):
    d = helper_dir(hid)
    return json.loads((d / "helper.json").read_text()) if d else None


def save(hid, cfg):
    atomic_write(HELPERS_DIR / hid / "helper.json", json.dumps(cfg, indent=2) + "\n")


def all_helpers():
    out = []
    if HELPERS_DIR.is_dir():
        for d in sorted(HELPERS_DIR.iterdir()):
            if (d / "helper.json").is_file():
                out.append((d.name, json.loads((d / "helper.json").read_text())))
    return out


def originals_of(hid, md_name):
    """The uploaded PDF/DOCX files a Markdown file was converted from."""
    stem = md_name.rsplit(".", 1)[0]
    return [HELPERS_DIR / hid / "originals" / (stem + ext) for ext in CONVERTIBLE
            if (HELPERS_DIR / hid / "originals" / (stem + ext)).is_file()]


def files_of(hid):
    docs = HELPERS_DIR / hid / "docs"
    if not docs.is_dir():
        return []
    return [{"name": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime,
             "sources": [o.name for o in originals_of(hid, p.name)]}
            for p in sorted(docs.iterdir(), key=lambda p: p.name.lower()) if p.is_file() and not p.name.startswith(".")]


def users_sql(hid, statement, args=()):
    """Run SQL on the helper's own database (chats.db), where its users live."""
    db = HELPERS_DIR / hid / "chats.db"
    sql(db, USERS_TABLE)  # the helper may not have started yet
    return sql(db, statement, args)


def user_list(hid):
    rows, _ = users_sql(hid, "SELECT name, created, changed FROM users ORDER BY name COLLATE NOCASE")
    return rows


def check_password(password):
    if not 8 <= len(password) <= 200:
        return "Password: 8 to 200 characters."
    return None


def new_id(name):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "helper"
    hid, n = base, 2
    while (HELPERS_DIR / hid).exists():
        hid, n = f"{base}-{n}", n + 1
    return hid


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # same as the servers, so TIME_WAIT doesn't count
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def validate(data, hid=None):
    """Returns (clean values, error message)."""
    name = " ".join(str(data.get("name") or "").split())
    purpose = str(data.get("purpose") or "").strip()
    if not 1 <= len(name) <= 60:
        return None, "Name: 1 to 60 characters."
    if not purpose:
        return None, "Purpose: say what the helper is for."
    if len(purpose) > 20_000:
        return None, "Purpose: at most 20,000 characters."
    try:
        port = int(data.get("port"))
    except (TypeError, ValueError):
        return None, "Port: a number."
    if not 1024 <= port <= 65535:
        return None, "Port: between 1024 and 65535."
    if port == ADMIN_PORT:
        return None, f"Port {port} is the admin page's port."
    for other, cfg in all_helpers():
        if other != hid and cfg["port"] == port:
            return None, f"Port {port} is already used by helper “{cfg['name']}”."
    current = load(hid) if hid else None
    if not (current and current["port"] == port and SUP.running(hid)) and not port_free(port):
        return None, f"Port {port} is in use by another program on this host."
    return {"name": name, "purpose": purpose, "port": port}, None


# ---------- supervisor: one helper.py process per enabled helper ----------
class Supervisor:
    def __init__(self):
        self.procs = {}      # id -> Popen
        self.logs = collections.defaultdict(lambda: collections.deque(maxlen=LOG_LINES))
        self.crashes = {}    # id -> (count, time of last start)
        self.lock = threading.RLock()

    def running(self, hid):
        with self.lock:
            p = self.procs.get(hid)
            return bool(p and p.poll() is None)

    def status(self, hid, cfg):
        with self.lock:
            if self.running(hid):
                return "running"
            if not cfg.get("enabled"):
                return "stopped"
            count, _ = self.crashes.get(hid, (0, 0))
            return "failed" if count >= 5 else "starting"

    def log(self, hid, line):
        self.logs[hid].append(time.strftime("%Y-%m-%d %H:%M:%S ") + line.rstrip("\n"))

    def start(self, hid):
        with self.lock:
            if self.running(hid):
                return
            proc = subprocess.Popen([sys.executable, str(HELPER_PY), str(HELPERS_DIR / hid)], cwd=APP_DIR,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    env={**os.environ, "PYTHONUNBUFFERED": "1"}, start_new_session=True)
            self.procs[hid] = proc
            count, _ = self.crashes.get(hid, (0, 0))
            self.crashes[hid] = (count, time.time())
            self.log(hid, f"[admin] started (pid {proc.pid})")
        threading.Thread(target=self._read, args=(hid, proc), daemon=True).start()

    def _read(self, hid, proc):
        for line in proc.stdout:
            self.log(hid, line)
        proc.wait()
        self.log(hid, f"[admin] exited with code {proc.returncode}")

    def stop(self, hid):
        with self.lock:
            proc = self.procs.pop(hid, None)
            self.crashes.pop(hid, None)
        if proc and proc.poll() is None:
            proc.terminate()  # helper.py stops its running answers on SIGTERM
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()

    def restart(self, hid):
        self.stop(hid)
        self.start(hid)

    def watch(self):
        """Start enabled helpers that aren't running; back off after crashes, give up after 5 in a row."""
        while True:
            for hid, cfg in all_helpers():
                if not cfg.get("enabled"):
                    continue
                with self.lock:
                    proc = self.procs.get(hid)
                    if proc and proc.poll() is None:
                        count, started = self.crashes.get(hid, (0, 0))
                        if count and time.time() - started > 60:  # stayed up a minute: healthy again
                            self.crashes[hid] = (0, started)
                        continue
                    count, started = self.crashes.get(hid, (0, 0))
                    if proc is not None:  # it was running and died
                        count += 1
                        self.crashes[hid] = (count, started)
                        self.procs.pop(hid)
                        if count >= 5:
                            self.log(hid, "[admin] crashed 5 times in a row; not restarting. Fix it, then press Start.")
                    if count >= 5 or time.time() - started < min(60, 2 ** count):
                        continue
                self.start(hid)
            time.sleep(2)

    def stop_all(self):
        for hid in list(self.procs):
            self.stop(hid)


SUP = Supervisor()


def summary(hid, cfg):
    files = files_of(hid)
    return {"id": hid, "name": cfg["name"], "purpose": cfg["purpose"], "port": cfg["port"],
            "enabled": bool(cfg.get("enabled")), "status": SUP.status(hid, cfg),
            "files": len(files), "bytes": sum(f["size"] for f in files), "created": cfg.get("created"),
            "users": len(user_list(hid))}


# ---------- HTTP ----------
class Handler(BaseHandler):
    server_version = "ConvHelperAdmin/1.0"
    DB = ADMIN_DB
    COOKIE = "conv_admin"
    PUBLIC = {
        "/login": ("login.html", "text/html; charset=utf-8"),
        "/style.css": ("style.css", "text/css; charset=utf-8"),
        **ICONS,
    }
    STATIC = {
        "/": ("admin.html", "text/html; charset=utf-8"),
        "/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
    }
    CREDENTIAL = read_credential()

    def do_GET(self):
        url = urlparse(self.path)
        if self.serve_public(url.path, url.query, "Helper admin"):
            return
        if not self.authorized():
            return
        path = url.path
        if path in self.STATIC:
            self.send_file(*self.STATIC[path])
        elif path == "/api/helpers":
            self.send_json({"admin_port": ADMIN_PORT, "helpers": [summary(h, c) for h, c in all_helpers()]})
        elif m := re.fullmatch(r"/api/helpers/([^/]+)", path):
            cfg = load(m.group(1))
            if not cfg:
                return self.send_json({"error": "helper not found"}, 404)
            self.send_json({**summary(m.group(1), cfg), "file_list": files_of(m.group(1)),
                            "user_list": user_list(m.group(1)), "admin_user": admin_user(self.CREDENTIAL),
                            "log": list(SUP.logs[m.group(1)])[-80:]})
        elif m := re.fullmatch(r"/api/helpers/([^/]+)/originals/([^/]+)", path):
            d, name = helper_dir(m.group(1)), unquote(m.group(2))
            ext = "." + name.rsplit(".", 1)[-1].lower()
            f = d / "originals" / name if d and UPLOAD_NAME.fullmatch(name) and ext in ORIGINAL_TYPES else None
            if not f or not f.is_file():
                return self.send_json({"error": "file not found"}, 404)
            self.send_body(f.read_bytes(), ORIGINAL_TYPES[ext],
                           extra=[("Content-Disposition", f'attachment; filename="{f.name}"')])
        elif m := re.fullmatch(r"/api/helpers/([^/]+)/files/([^/]+)", path):
            f = self.file_path(m)
            if not f or not f.is_file():
                return self.send_json({"error": "file not found"}, 404)
            self.send_body(f.read_bytes(), "text/plain; charset=utf-8",
                           extra=[("Content-Disposition", f'inline; filename="{f.name}"'),
                                  ("X-Content-Type-Options", "nosniff")])
        else:
            self.send_json({"error": "not found"}, 404)

    def file_path(self, m):
        d = helper_dir(m.group(1))
        name = unquote(m.group(2))
        if not d or not FILE_NAME.fullmatch(name):
            return None
        return d / "docs" / name

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
        if path == "/api/helpers":
            clean, err = validate(data)
            if err:
                return self.send_json({"error": err}, 400)
            hid = new_id(clean["name"])
            (HELPERS_DIR / hid / "docs").mkdir(parents=True)
            save(hid, {**clean, "enabled": True, "created": time.time()})
            SUP.start(hid)
            self.send_json(summary(hid, load(hid)), 201)
        elif m := re.fullmatch(r"/api/helpers/([^/]+)/users", path):
            hid = m.group(1)
            if not helper_dir(hid):
                return self.send_json({"error": "helper not found"}, 404)
            name = str(data.get("username") or "").strip()
            password = str(data.get("password") or "")
            if not USER_NAME.fullmatch(name):
                return self.send_json({"error": "Username: 1 to 40 letters, digits and . _ @ - (starting with a "
                                                "letter or digit)."}, 400)
            if name.lower() == admin_user(self.CREDENTIAL).lower():
                return self.send_json({"error": "That's the admin account's name; it already works on every helper."}, 400)
            if err := check_password(password):
                return self.send_json({"error": err}, 400)
            now = time.time()
            try:
                users_sql(hid, "INSERT INTO users (name, hash, created, changed) VALUES (?, ?, ?, ?)",
                          (name, hash_password(password), now, now))
            except sqlite3.IntegrityError:  # names are unique, ignoring case
                return self.send_json({"error": f"User {name} already exists on this helper."}, 409)
            self.send_json({"users": user_list(hid)}, 201)
        elif m := re.fullmatch(r"/api/helpers/([^/]+)/(start|stop|restart)", path):
            hid, action = m.groups()
            cfg = load(hid)
            if not cfg:
                return self.send_json({"error": "helper not found"}, 404)
            cfg["enabled"] = action != "stop"
            save(hid, cfg)
            SUP.stop(hid)  # also resets the crash count
            if action != "stop":
                if not port_free(cfg["port"]):
                    cfg["enabled"] = False
                    save(hid, cfg)
                    return self.send_json({"error": f"Port {cfg['port']} is in use by another program."}, 409)
                SUP.start(hid)
                time.sleep(0.6)  # so the reply shows a quick crash (bad config, port taken) right away
            self.send_json(summary(hid, cfg))
        else:
            self.send_json({"error": "not found"}, 404)

    def do_PUT(self):
        if not self.authorized():
            return
        path = urlparse(self.path).path
        if m := re.fullmatch(r"/api/helpers/([^/]+)", path):
            hid = m.group(1)
            cfg = load(hid)
            if not cfg:
                return self.send_json({"error": "helper not found"}, 404)
            try:
                data = self.read_json()
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            clean, err = validate(data, hid)
            if err:
                return self.send_json({"error": err}, 400)
            port_changed = clean["port"] != cfg["port"]
            save(hid, {**cfg, **clean})
            if port_changed and cfg.get("enabled"):
                SUP.restart(hid)  # name and purpose are read live; only the port needs a restart
            self.send_json(summary(hid, load(hid)))
        elif m := re.fullmatch(r"/api/helpers/([^/]+)/users/([^/]+)", path):
            hid, name = m.group(1), unquote(m.group(2))
            try:
                password = str(self.read_json().get("password") or "")
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            if err := check_password(password):
                return self.send_json({"error": err}, 400)
            if not helper_dir(hid):
                return self.send_json({"error": "helper not found"}, 404)
            # a newer "changed" ends the user's current logins on the helper
            rows, _ = users_sql(hid, "UPDATE users SET hash = ?, changed = ? WHERE name = ? RETURNING name",
                                (hash_password(password), time.time(), name))
            if not rows:
                return self.send_json({"error": "user not found"}, 404)
            self.send_json({"users": user_list(hid)})
        elif m := re.fullmatch(r"/api/helpers/([^/]+)/files/([^/]+)", path):
            d, name = helper_dir(m.group(1)), unquote(m.group(2))
            if not d:
                return self.send_json({"error": "helper not found"}, 404)
            if not UPLOAD_NAME.fullmatch(name):
                return self.send_json({"error": "Use a .md, .markdown, .txt, .pdf or .docx file name of letters, digits, "
                                                "spaces and . _ - ( ) & + , ' (max 120 characters)."}, 400)
            if "." + name.rsplit(".", 1)[-1].lower() in CONVERTIBLE:
                return self.upload_convert(d, name)
            f = d / "docs" / name
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_FILE:
                return self.send_json({"error": f"{f.name}: larger than 20 MB."}, 413)
            body = self.rfile.read(length)
            try:
                body.decode("utf-8")
            except UnicodeDecodeError:
                return self.send_json({"error": f"{f.name}: not a UTF-8 text file."}, 400)
            existed = f.exists()
            tmp = f.with_name("." + f.name + ".upload")
            tmp.write_bytes(body)
            os.replace(tmp, f)
            for original in originals_of(d.name, f.name):  # a hand-made .md replaces a converted one
                original.unlink()
            self.send_json({"name": f.name, "size": len(body), "replaced": existed}, 200 if existed else 201)
        else:
            self.send_json({"error": "not found"}, 404)

    def upload_convert(self, d, name):
        """Keep the PDF/DOCX in originals/ and save its Markdown version in docs/<name>.md."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_CONVERT:
            return self.send_json({"error": f"{name}: larger than 50 MB."}, 413)
        originals = d / "originals"
        originals.mkdir(exist_ok=True)
        ext = "." + name.rsplit(".", 1)[-1].lower()
        tmp = originals / f".{name}.upload{ext}"  # the converter goes by the extension
        tmp.write_bytes(self.rfile.read(length))
        try:
            md, info = convert(tmp)
        except ConvertError as e:
            tmp.unlink(missing_ok=True)
            return self.send_json({"error": f"{name}: {e}"}, 422)
        stem = name.rsplit(".", 1)[0]
        md_name, original = stem + ".md", stem + ext
        target = d / "docs" / md_name
        existed = target.exists()
        for old in originals_of(d.name, md_name):  # e.g. report.docx replaced by report.pdf
            old.unlink()
        os.replace(tmp, originals / original)
        atomic_write(target, md)
        self.send_json({"name": md_name, "size": target.stat().st_size, "replaced": existed,
                        "converted_from": original, **info}, 200 if existed else 201)

    def do_DELETE(self):
        if not self.authorized():
            return
        path = urlparse(self.path).path
        if m := re.fullmatch(r"/api/helpers/([^/]+)/files/([^/]+)", path):
            f = self.file_path(m)
            if not f or not f.is_file():
                return self.send_json({"error": "file not found"}, 404)
            f.unlink()
            for original in originals_of(m.group(1), f.name):
                original.unlink()
            self.send_json({"deleted": f.name})
        elif m := re.fullmatch(r"/api/helpers/([^/]+)/users/([^/]+)", path):
            hid, name = m.group(1), unquote(m.group(2))
            if not helper_dir(hid):
                return self.send_json({"error": "helper not found"}, 404)
            # the helper then refuses this user's sessions; their chats stay
            rows, _ = users_sql(hid, "DELETE FROM users WHERE name = ? RETURNING name", (name,))
            if not rows:
                return self.send_json({"error": "user not found"}, 404)
            self.send_json({"users": user_list(hid)})
        elif m := re.fullmatch(r"/api/helpers/([^/]+)", path):
            hid = m.group(1)
            if not helper_dir(hid):
                return self.send_json({"error": "helper not found"}, 404)
            SUP.stop(hid)
            TRASH_DIR.mkdir(parents=True, exist_ok=True)
            dest = TRASH_DIR / f"{hid}-{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.move(str(HELPERS_DIR / hid), dest)
            SUP.logs.pop(hid, None)
            self.send_json({"deleted": hid, "moved_to": str(dest)})
        else:
            self.send_json({"error": "not found"}, 404)


def main():
    os.umask(0o077)  # chats, users, files: owner only (helpers inherit this)
    HELPERS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)
    init_sessions(ADMIN_DB)
    server = ThreadingHTTPServer((HOST, ADMIN_PORT), Handler)
    server.daemon_threads = True
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    threading.Thread(target=SUP.watch, daemon=True).start()
    print(f"helper admin on http://{HOST}:{ADMIN_PORT}  data={DATA_DIR}  "
          f"login={'on' if Handler.CREDENTIAL else 'OFF'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SUP.stop_all()


if __name__ == "__main__":
    main()
