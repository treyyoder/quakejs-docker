"""The HTTP layer: request plumbing and dispatch.

Endpoints register themselves from routes.py with a method, a path, whether
they are public, and what kind of body they take. Dispatch checks the session
before a byte of body is read, parses JSON for the routes that take it, and
turns exceptions into the status codes the console expects. Nothing here
knows what any route does, which is the point: the place a request is
admitted is one short function, not a case in a four-hundred-line chain.
"""

import base64
import hmac
import json
import re
import secrets
import threading
import urllib.error
import urllib.parse
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import audit
from . import auth
from . import chat
from . import config
from . import stats


class PayloadTooLarge(Exception):
    """A request body over MAX_JSON_BYTES; answered with 413 before it is read."""


class Route:
    __slots__ = ("fn", "public", "body")

    def __init__(self, fn, public, body):
        self.fn, self.public, self.body = fn, public, body


_routes = {}      # (method, path) -> Route
_prefixes = []    # (method, prefix, Route), for paths that end in a name


def route(method, path, public=False, body=None, prefix=False):
    """Register an endpoint.

    body is None, "json" (parsed and capped before the route runs) or "raw"
    (the route reads the stream itself). Only a public route runs without a
    session, and the session is checked before any body is touched.
    """
    def register(fn):
        entry = Route(fn, public, body)
        if prefix:
            _prefixes.append((method, path, entry))
        else:
            _routes[(method, path)] = entry
        return fn
    return register


class Handler(BaseHTTPRequestHandler):
    server_version = "quakejs-admin"
    # A client that connects and goes quiet would otherwise hold its thread
    # forever; this is per read, so a slow but moving upload is unaffected.
    timeout = config.REQUEST_TIMEOUT

    def log_message(self, fmt, *args):
        print(f"[admin] {self.client_ip()} {fmt % args}", flush=True)

    def version_string(self):
        # The stock one appends the Python version; nobody needs to be told.
        return self.server_version

    def client_ip(self):
        """Who to hold responsible: the forwarded address, never the proxy."""
        forwarded = self.headers.get("X-Forwarded-For") if getattr(self, "headers", None) else None
        return auth.client_ip(self.client_address[0], forwarded)

    # -- cookies
    def guest_token(self):
        """Stable per-browser token, used only to throttle chat."""
        raw = self.headers.get("Cookie")
        if raw:
            try:
                morsel = SimpleCookie(raw).get(chat.GUEST_COOKIE)
                if morsel and re.fullmatch(r"[A-Za-z0-9_-]{16,64}", morsel.value):
                    return morsel.value
            except Exception:
                pass
        return secrets.token_urlsafe(16)

    def cookie_flags(self):
        """Attributes every cookie gets. Secure is added when the proxy in front
        reports TLS, so the cookie is never sent back over plain HTTP; a client
        that lies about the header only restricts its own cookie.
        """
        secure = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        return "HttpOnly; SameSite=Strict" + ("; Secure" if secure else "")

    def set_cookie(self, header):
        """Send this Set-Cookie with the next response."""
        self._pending_cookie = header

    def set_guest_cookie(self, token):
        self.set_cookie("%s=%s; Path=%s; %s; Max-Age=31536000"
                        % (chat.GUEST_COOKIE, token, config.MOUNT_PATH, self.cookie_flags()))

    def cookie_token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            return SimpleCookie(raw).get("qadmin").value
        except (AttributeError, Exception):
            return None

    # -- authentication
    def authed(self):
        token = self.cookie_token()
        if token and auth.session_valid(token):
            return True
        # Kept for scripting; the console UI uses the session cookie. It counts
        # against the same lockout as the sign-in form - otherwise it would be an
        # unthrottled way to guess the password against any endpoint, each guess
        # costing the server a full PBKDF2 round.
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            ip = self.client_ip()
            if auth.lockout_remaining(ip):
                return False
            try:
                raw = base64.b64decode(header[6:]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return False
            user, _, password = raw.partition(":")
            if hmac.compare_digest(user, config.USER) and auth.check_password(password):
                return True
            auth.note_failure(ip)
        return False

    def deny(self):
        # No WWW-Authenticate header: the console renders its own sign-in form.
        self.json({"error": "authentication required"}, 401)

    # -- responses
    def json(self, payload, status=200):
        self.send_bytes(json.dumps(payload).encode(), "application/json", status)

    def send_bytes(self, body, mime, status=200, cache=None, headers=None):
        self.status = status      # dispatch audits what succeeded
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        # Every response says what it is and is to be taken at its word.
        self.send_header("X-Content-Type-Options", "nosniff")
        if cache:
            self.send_header("Cache-Control", cache)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        pending = getattr(self, "_pending_cookie", None)
        if pending:
            self.send_header("Set-Cookie", pending)
            self._pending_cookie = None
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > config.MAX_JSON_BYTES:
            raise PayloadTooLarge(length)
        return json.loads(self.rfile.read(length) or b"{}")

    # -- dispatch
    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        # The same route, the same headers, no body; send_bytes knows.
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        self.query = urllib.parse.parse_qs(parsed.query)
        self.rest = None       # what followed a prefix route's path
        self.payload = None    # the parsed JSON body, for routes that take one
        self.status = None     # what the route answered, once it has
        entry = _routes.get((method, parsed.path))
        if entry is None:
            for known_method, prefix, candidate in _prefixes:
                if known_method == method and parsed.path.startswith(prefix):
                    entry, self.rest = candidate, parsed.path[len(prefix):]
                    break
        if entry is None:
            return self.json({"error": "not found"}, 404)
        # Authentication comes before the body is read: reading first would let
        # anyone tie up memory and a thread with a payload they never earned.
        if not entry.public and not self.authed():
            return self.deny()
        if entry.body == "json":
            try:
                self.payload = self.read_json()
            except PayloadTooLarge:
                return self.json(
                    {"error": f"request body over {config.MAX_JSON_BYTES // 1024} KB"}, 413)
            except ValueError:
                return self.json({"error": "body is not valid JSON"}, 400)
        try:
            entry.fn(self)
            # Every admin action that succeeded is on the record - and every
            # sign-in, which is the one public action worth remembering.
            if (method == "POST" and (not entry.public or parsed.path == "/api/login")
                    and self.status is not None and self.status < 400):
                audit.record(self.client_ip(), parsed.path,
                             audit.detail(parsed.path, self.payload, self.query))
            return None
        except ValueError as exc:
            return self.json({"error": str(exc)}, 400)
        except (urllib.error.URLError, OSError, zipfile.BadZipFile) as exc:
            return self.json({"error": f"{type(exc).__name__}: {exc}"}, 502)
        except Exception as exc:  # surface failures to the console instead of 500ing blind
            return self.json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def main():
    if config.PASSWORD == "":
        raise SystemExit("[admin] ADMIN_PASSWORD is empty; the console is disabled")
    auth.ensure_password()
    from . import routes  # noqa: F401  (importing it registers every endpoint)
    from . import backup, bans
    threading.Thread(target=chat.tail_chat, daemon=True).start()
    threading.Thread(target=stats.tail_game_log, daemon=True).start()
    threading.Thread(target=bans.sweep, daemon=True).start()
    threading.Thread(target=backup.backup_thread, daemon=True).start()
    server = ThreadingHTTPServer((config.BIND, config.PORT), Handler)
    print(f"[admin] listening on {config.BIND}:{config.PORT} as user {config.USER!r}", flush=True)
    server.serve_forever()
