"""Admin authentication: PBKDF2 password hashing and signed session cookies.

No third-party dependencies: hashlib/hmac from the stdlib.

Password hash format (ADMIN_PASSWORD_HASH):  pbkdf2$<iterations>$<salt-hex>$<dk-hex>
Session cookie format:  <expiry-unix-ts>.<hmac-sha256(ts, secret)>
"""

import hashlib
import hmac
import secrets
import time

from . import config

PBKDF2_ITERATIONS = 260_000
SESSION_COOKIE = "findyou_session"
SESSION_TTL_S = 12 * 60 * 60  # 12 hours


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, dk_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, TypeError):
        return False


def check_admin_credentials(username: str, password: str) -> bool:
    if username != config.ADMIN_USER:
        # Constant-ish time: still burn a PBKDF2 round on a wrong username.
        verify_password(password, hash_password("timing-equalizer"))
        return False
    if config.ADMIN_PASSWORD_HASH:
        return verify_password(password, config.ADMIN_PASSWORD_HASH)
    if config.ADMIN_PASSWORD:
        return hmac.compare_digest(password, config.ADMIN_PASSWORD)
    return False


def _sign(payload: str) -> str:
    return hmac.new(config.SECRET, payload.encode(), hashlib.sha256).hexdigest()


def create_session_cookie() -> str:
    expiry = str(int(time.time()) + SESSION_TTL_S)
    return f"{expiry}.{_sign(expiry)}"


def is_valid_session_cookie(cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    expiry, signature = cookie.split(".", 1)
    if not expiry.isdigit():
        return False
    if int(expiry) < time.time():
        return False
    return hmac.compare_digest(signature, _sign(expiry))
