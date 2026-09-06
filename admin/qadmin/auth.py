"""Credentials, sessions, the sign-in lockout, and who a request is from."""

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
import threading

from . import config

_lock = threading.Lock()
_sessions = {}   # token -> expiry timestamp
_failures = {}   # client ip -> [count, locked-until]


# --------------------------------------------------------------- credentials
def _hash(password, salt, rounds=config.PBKDF2_ROUNDS):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds).hex()


def stored_credentials():
    """The password set from the console, or None to fall back to the env var."""
    try:
        return json.loads(config.STATE.read_text())
    except (OSError, ValueError):
        return None


def check_password(password):
    record = stored_credentials()
    if record:
        expected = record["hash"]
        actual = _hash(password, bytes.fromhex(record["salt"]), record["iterations"])
        return hmac.compare_digest(actual, expected)
    return bool(config.PASSWORD) and hmac.compare_digest(password, config.PASSWORD)


def ensure_password():
    """Nothing is baked into the image. Given no ADMIN_PASSWORD and nothing
    stored, mint one, store it the way "Change password" does, and print it
    once - the log is the only place it ever appears in the clear.
    """
    if config.PASSWORD is not None:
        if stored_credentials():
            print("[admin] a password set from the console is stored and takes"
                  " precedence over ADMIN_PASSWORD", flush=True)
        return
    if stored_credentials():
        return
    generated = secrets.token_urlsafe(15)
    try:
        set_password(generated)
    except OSError as exc:
        raise SystemExit(f"[admin] cannot store a generated password at {config.STATE}: {exc}")
    print(f"[admin] no ADMIN_PASSWORD given; generated one for user {config.USER!r}: {generated}",
          flush=True)
    print(f"[admin] it is stored hashed at {config.STATE} and will not be shown again;"
          " change it under 'Change password' in the console", flush=True)


def set_password(password):
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    store_credentials({"salt": salt.hex(), "iterations": config.PBKDF2_ROUNDS,
                       "hash": _hash(password, salt)})


def store_credentials(record):
    """Write a credentials record. Everyone is signed out if it changed what
    signs in; returns whether that happened."""
    changed = record != stored_credentials()
    config.STATE.parent.mkdir(parents=True, exist_ok=True)
    config.STATE.write_text(json.dumps(record))
    try:
        config.STATE.chmod(0o600)
    except OSError:
        pass
    if changed:
        with _lock:
            _sessions.clear()  # force everyone to sign in again
    return changed


def needs_rehash():
    """Whether the stored hash was made with fewer rounds than are used now."""
    record = stored_credentials()
    return bool(record) and int(record.get("iterations", 0)) < config.PBKDF2_ROUNDS


# ------------------------------------------------------------------ sessions
def new_session():
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = time.time() + config.SESSION_HOURS * 3600
        for old, expiry in list(_sessions.items()):
            if expiry < time.time():
                del _sessions[old]
    return token


def session_valid(token):
    with _lock:
        expiry = _sessions.get(token)
        if expiry is None:
            return False
        if expiry < time.time():
            del _sessions[token]
            return False
    return True


def end_session(token):
    with _lock:
        _sessions.pop(token, None)


# ------------------------------------------------------------------- lockout
def note_failure(ip):
    with _lock:
        entry = _failures.setdefault(ip, [0, 0.0])
        entry[0] += 1
        if entry[0] >= config.LOCKOUT_AFTER:
            entry[1] = time.time() + config.LOCKOUT_SECONDS
            entry[0] = 0


def lockout_remaining(ip):
    with _lock:
        entry = _failures.get(ip)
        return max(0, int(entry[1] - time.time())) if entry else 0


# ----------------------------------------------------------- who is calling
def trusted_proxies(raw=None):
    """TRUSTED_PROXIES as networks: 10.0.0.5, 172.18.0.0/16, or both."""
    if raw is None:
        raw = os.environ.get("TRUSTED_PROXIES", "")
    networks = []
    for entry in re.split(r"[\s,]+", raw.strip()):
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            print(f"[admin] ignoring unreadable TRUSTED_PROXIES entry {entry!r}", flush=True)
    return networks


TRUSTED_PROXIES = trusted_proxies()


def client_ip(peer, forwarded, trusted=None):
    """The address to hold responsible for a request.

    Only a connection from loopback - our own Apache - may carry a forwarded
    address; anything else is unproxied and its headers are the client's own
    claim. Within the header, the rightmost entry is the one Apache appended and
    the only one the client could not have written, so walk leftwards from there
    past any further proxies we were told to trust.
    """
    if trusted is None:
        trusted = TRUSTED_PROXIES
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if not addr.is_loopback or not forwarded:
        return str(addr)
    for hop in reversed([h.strip() for h in forwarded.split(",")]):
        try:
            candidate = ipaddress.ip_address(hop)
        except ValueError:
            break
        if isinstance(candidate, ipaddress.IPv6Address) and candidate.ipv4_mapped:
            candidate = candidate.ipv4_mapped
        if any(candidate in network for network in trusted):
            continue
        return str(candidate)
    return str(addr)
