"""Master-key ownership. Only the holder of the master key can pair a device.

The key itself is never stored — only a scrypt hash. Each paired device gets
its own random token (stored as sha256), so devices can be revoked one by one.
"""

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request

from . import db

COOKIE = "dork_token"
MAX_FAILS = 5
LOCKOUT_SECONDS = 300
_fails: dict[str, list[float]] = {}


def _scrypt(key: str, salt: bytes) -> bytes:
    return hashlib.scrypt(key.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def has_master_key() -> bool:
    return db.get_meta("master_hash") is not None


def set_master_key(key: str) -> None:
    salt = secrets.token_bytes(16)
    db.set_meta("master_salt", salt.hex())
    db.set_meta("master_hash", _scrypt(key, salt).hex())
    db.x("DELETE FROM devices")  # new key => every device must re-pair


def generate_master_key() -> str:
    # 4 groups of 5 chars, no ambiguous characters — easy to type on a phone
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4))


def verify_master_key(key: str) -> bool:
    salt, stored = db.get_meta("master_salt"), db.get_meta("master_hash")
    if not salt or not stored:
        return False
    return hmac.compare_digest(_scrypt(key.strip(), bytes.fromhex(salt)).hex(), stored)


def check_rate_limit(ip: str) -> None:
    recent = [t for t in _fails.get(ip, []) if time.time() - t < LOCKOUT_SECONDS]
    _fails[ip] = recent
    if len(recent) >= MAX_FAILS:
        raise HTTPException(429, "Too many wrong attempts. Try again in a few minutes.")


def record_fail(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.time())


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def pair_device(name: str, ip: str) -> str:
    token = secrets.token_urlsafe(32)
    db.x(
        "INSERT INTO devices(id, name, token_hash, ip, created_at, last_seen) VALUES(?,?,?,?,?,?)",
        (secrets.token_hex(6), name[:60] or "Device", _hash_token(token), ip, db.now(), db.now()),
    )
    return token


def unpair_token(token: str) -> None:
    db.x("DELETE FROM devices WHERE token_hash=?", (_hash_token(token),))


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _token_from(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE)


def require_device(request: Request) -> dict:
    """FastAPI dependency: the request must come from a paired device."""
    token = _token_from(request)
    device = db.one("SELECT * FROM devices WHERE token_hash=?", (_hash_token(token),)) if token else None
    if not device:
        raise HTTPException(401, "Not paired. Enter the master key.")
    if time.time() - (device["last_seen"] or 0) > 60:
        db.x("UPDATE devices SET last_seen=?, ip=? WHERE id=?", (db.now(), client_ip(request), device["id"]))
    return device


# ---------- pairing codes: a new device asks, a signed-in device shows a code ----------

PAIR_TTL = 300          # seconds a code stays valid
PAIR_MAX_TRIES = 5      # wrong codes before the request is dropped
PAIR_MAX_OPEN = 3       # open requests per IP
_pairs: dict[str, dict] = {}


def _prune_pairs() -> None:
    now = time.time()
    for rid in [r for r, p in _pairs.items() if p["expires"] < now]:
        _pairs.pop(rid, None)


def request_pairing(name: str, ip: str) -> dict:
    _prune_pairs()
    if sum(1 for p in _pairs.values() if p["ip"] == ip) >= PAIR_MAX_OPEN:
        raise HTTPException(429, "Too many connection requests. Wait a few minutes.")
    rid = secrets.token_urlsafe(12)
    _pairs[rid] = {"id": rid, "name": (name or "Device")[:60], "ip": ip, "code": f"{secrets.randbelow(10**6):06d}",
                   "expires": time.time() + PAIR_TTL, "tries": 0}
    return {"request_id": rid, "expires_in": PAIR_TTL}


def pending_pairings() -> list[dict]:
    _prune_pairs()
    return [{"id": p["id"], "name": p["name"], "ip": p["ip"], "code": p["code"],
             "expires_in": int(p["expires"] - time.time())} for p in _pairs.values()]


def deny_pairing(rid: str) -> None:
    _pairs.pop(rid, None)


def verify_pairing(rid: str, code: str, ip: str) -> str:
    """Correct code → new device token. Each request allows a few tries, then it's gone."""
    _prune_pairs()
    p = _pairs.get(rid)
    if not p:
        raise HTTPException(410, "That code expired. Tap Connect to get a new one.")
    if not hmac.compare_digest(p["code"], "".join(ch for ch in code if ch.isdigit())):
        p["tries"] += 1
        if p["tries"] >= PAIR_MAX_TRIES:
            _pairs.pop(rid, None)
            raise HTTPException(410, "Too many wrong codes. Tap Connect to start again.")
        raise HTTPException(401, f"Wrong code. {PAIR_MAX_TRIES - p['tries']} tries left.")
    _pairs.pop(rid, None)
    return pair_device(p["name"], ip)


# ---------- Dork Desktop on this PC signs in by itself ----------

def ensure_local_app_token(path) -> None:
    """Keep a valid device token in a local file for Dork Desktop (same PC, same user)."""
    if path.exists():
        token = path.read_text().strip()
        if token and db.one("SELECT id FROM devices WHERE token_hash=?", (_hash_token(token),)):
            return
    path.write_text(pair_device("This laptop (Dork Desktop)", "127.0.0.1"))
