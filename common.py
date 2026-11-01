"""Shared by manager.py (admin) and helper.py (one chat helper): login, sessions, JSON and file responses.

Standard library only.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from contextlib import closing
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = Path(os.environ.get("CONV_HELPER_DATA", APP_DIR / "data")).resolve()
CRED_FILE = Path(os.environ.get("CONV_HELPER_CREDENTIAL", Path.home() / ".config/conv-helper/credential"))
SESSION_DAYS = 30

# "/style.css" in a page becomes "/style.css?v=<hash>", so browsers and proxies never use an old copy
ASSET_REF = re.compile(rb'(src|href)="(/[^"?]+\.(?:css|js))"')


def read_credential():
    return CRED_FILE.read_text().strip() if CRED_FILE.is_file() else ""


def sql(db_path, statement, args=()):
    with closing(sqlite3.connect(db_path, timeout=10)) as conn, conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(statement, args)
        return [dict(r) for r in cur.fetchall()], cur.lastrowid


def init_sessions(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    sql(db_path, "CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires REAL NOT NULL)")
    cols = [r["name"] for r in sql(db_path, "PRAGMA table_info(sessions)")[0]]
    for col, kind in (("user", "TEXT"), ("created", "REAL")):  # added with per-helper users
        if col not in cols:
            sql(db_path, f"ALTER TABLE sessions ADD COLUMN {col} {kind}")


# a helper's own users live in its chats.db; the admin page writes them, the helper reads them
USERS_TABLE = """CREATE TABLE IF NOT EXISTS users (
    name TEXT PRIMARY KEY COLLATE NOCASE, hash TEXT NOT NULL, created REAL NOT NULL, changed REAL NOT NULL)"""


def admin_user(credential):
    return credential.partition(":")[0]


# ----- password hashes for helper users (scrypt, standard library) -----
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def check_login(credential, username, password):
    user, _, pw = credential.partition(":")
    # compare both before combining, so the time taken doesn't reveal which was wrong
    ok_user = hmac.compare_digest(username.encode(), user.encode())
    ok_pw = hmac.compare_digest(password.encode(), pw.encode())
    return ok_user and ok_pw


def atomic_write(path, text):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class BaseHandler(BaseHTTPRequestHandler):
    """Login, cookies and response helpers. Subclasses set DB, COOKIE, STATIC, PUBLIC."""
    DB = None          # SQLite file holding the sessions table
    COOKIE = "session"
    STATIC = {}        # path -> (file under static/, content type), login needed
    PUBLIC = {}        # same, served without logging in
    CREDENTIAL = ""
    user = ""          # who is logged in, set by authorized()

    # subclasses can allow more accounts (helper users) and end sessions early (password changed).
    # check_credentials may return the account name as stored (so "Alice" logs in as "alice").
    def check_credentials(self, username, password):
        return check_login(self.CREDENTIAL, username, password)

    def session_ok(self, user, created):
        return True

    def login_required(self):
        return bool(self.CREDENTIAL)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    # ----- sessions -----
    def session_token(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie[self.COOKIE].value if self.COOKIE in cookie else ""

    def valid_session(self):
        """The logged-in user name, or None."""
        rows, _ = sql(self.DB, "SELECT user, created FROM sessions WHERE token_hash = ? AND expires > ?",
                      (token_hash(self.session_token()), time.time()))
        if rows and self.session_ok(rows[0]["user"], rows[0]["created"] or 0):
            return rows[0]["user"] or ""
        return None

    def authorized(self):
        if not self.login_required():
            self.user = admin_user(self.CREDENTIAL)
            return True
        user = self.valid_session()
        if user is not None:
            self.user = user
            return True
        if urlparse(self.path).path.startswith("/api/"):
            self.send_json({"error": "not logged in"}, 401)
        else:
            self.redirect("/login")
        return False

    def login(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(min(length, 10_000)).decode(errors="replace"))
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        ok = not self.login_required() or self.check_credentials(username, password)
        if ok:
            if isinstance(ok, str):
                username = ok
            token = secrets.token_urlsafe(32)
            now = time.time()
            sql(self.DB, "DELETE FROM sessions WHERE expires < ?", (now,))
            sql(self.DB, "INSERT INTO sessions (token_hash, expires, user, created) VALUES (?, ?, ?, ?)",
                (token_hash(token), now + SESSION_DAYS * 86400, username, now))
            cookie = f"{self.COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}"
            if self.headers.get("X-Forwarded-Proto") == "https":  # behind an HTTPS reverse proxy
                cookie += "; Secure"
            return self.redirect("/", cookie)
        time.sleep(1)  # slow down guessing
        self.redirect("/login?error=1")

    def logout(self):
        sql(self.DB, "DELETE FROM sessions WHERE token_hash = ?", (token_hash(self.session_token()),))
        self.redirect("/login", f"{self.COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")

    # ----- responses -----
    def redirect(self, location, cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_body(self, body, ctype, status=200, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def versioned(self, m):
        entry = self.STATIC.get(m.group(2).decode()) or self.PUBLIC.get(m.group(2).decode())
        if not entry:
            return m.group(0)
        digest = hashlib.sha256((STATIC_DIR / entry[0]).read_bytes()).hexdigest()[:10]
        return m.group(1) + b'="' + m.group(2) + b"?v=" + digest.encode() + b'"'

    def send_file(self, name, ctype, replace=()):
        body = (STATIC_DIR / name).read_bytes()
        for old, new in replace:
            body = body.replace(old, new)
        if ctype.startswith("text/html"):
            body = ASSET_REF.sub(self.versioned, body)
        self.send_body(body, ctype)

    def read_json(self, limit=100_000):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError("request too large")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"bad JSON: {e}") from None

    def serve_public(self, path, query, title):
        """Serve a PUBLIC file (login page gets the title and error flag). Returns True if handled."""
        if path not in self.PUBLIC:
            return False
        name, ctype = self.PUBLIC[path]
        replace = []
        if name == "login.html":
            replace.append((b"{{TITLE}}", html_escape(title).encode()))
            if "error" in parse_qs(query):
                replace.append((b'<p class="error" hidden>', b'<p class="error">'))
        self.send_file(name, ctype, replace)
        return True


def html_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


ICONS = {
    "/favicon.ico": ("icons/icon-192.png", "image/png"),
    "/icons/icon.svg": ("icons/icon.svg", "image/svg+xml"),
    "/icons/icon-192.png": ("icons/icon-192.png", "image/png"),
    "/icons/icon-512.png": ("icons/icon-512.png", "image/png"),
    "/icons/icon-maskable-512.png": ("icons/icon-maskable-512.png", "image/png"),
    "/icons/apple-touch-icon.png": ("icons/apple-touch-icon.png", "image/png"),
}
