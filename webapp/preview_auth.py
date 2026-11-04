"""Minimum access restriction for the hosted client preview (FBX_AUTH_MODE=preview).

Every request except /healthz must carry valid HTTP Basic credentials (over Render's HTTPS) for one of the named
users in FBX_PREVIEW_USERS. Only scrypt hashes are configured, never passwords:
    FBX_PREVIEW_USERS = "<user>:scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>[;<user>:...]"
Create an entry with: python deploy/make_preview_credential.py <user>
Repeated wrong passwords from one address are refused for 15 minutes. This is a preview gate for a few named
FalconBridge users, not the full sign-in and organization system (deferred).
"""
import base64
import hashlib
import hmac
import time
from collections import defaultdict, deque

MAX_FAILURES, WINDOW = 10, 15 * 60
_failures = defaultdict(deque)
_verified = {}                                  # sha256(header) -> (user, expiry): avoids re-running scrypt per asset


def hash_password(password, salt, n=2 ** 14, r=8, p=1):
    h = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def parse_users(spec):
    """{user: hash} from FBX_PREVIEW_USERS; malformed entries raise ValueError (the app then refuses to start)."""
    users = {}
    for item in (x.strip() for x in (spec or "").split(";")):
        if not item:
            continue
        user, _, h = item.partition(":")
        parts = h.split("$")
        if not user or len(parts) != 6 or parts[0] != "scrypt":
            raise ValueError("FBX_PREVIEW_USERS entries must be <user>:scrypt$n$r$p$salt$hash")
        users[user] = h
    return users


def _check(users, user, password):
    h = users.get(user)
    if h is None:
        hashlib.scrypt(b"x", salt=b"0" * 16, n=2 ** 14, r=8, p=1, dklen=32)       # same cost for unknown users
        return False
    _, n, r, p, salt, _ = h.split("$")
    return hmac.compare_digest(hash_password(password, base64.b64decode(salt), int(n), int(r), int(p)), h)


def locked(ip, now=None):
    now = now or time.time()
    q = _failures[ip]
    while q and q[0] < now - WINDOW:
        q.popleft()
    return len(q) >= MAX_FAILURES


def authenticate(users, header, ip):
    """The user name, or None. Counts a failure only when credentials were sent (the first, unauthenticated request
    of a browser is not a failure)."""
    if not header or not header.startswith("Basic "):
        return None
    key = hashlib.sha256(header.encode()).hexdigest()
    hit = _verified.get(key)
    if hit and hit[1] > time.time():
        return hit[0]
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:                                                                # noqa: BLE001
        user, password = "", ""
    if user and _check(users, user, password):
        _verified[key] = (user, time.time() + 600)
        return user
    _failures[ip].append(time.time())
    return None
