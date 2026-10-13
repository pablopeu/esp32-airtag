"""Environment-driven configuration for the tracking server."""

import os
import secrets


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


#: Directory for the sqlite DB, Apple account state and the secret key file.
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))

#: Base URL of the anisette-server providing Apple authentication headers.
ANISETTE_URL = os.environ.get("ANISETTE_URL", "http://anisette:6969")

#: Admin credentials for the web interface.
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
#: Either ADMIN_PASSWORD or ADMIN_PASSWORD_HASH (pbkdf2, see security.py).
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH")

#: Minutes between automatic location refreshes (0 disables auto-polling).
POLL_MINUTES = _int_env("POLL_MINUTES", 15)

#: How many days of location reports to keep in the database.
RETENTION_DAYS = _int_env("RETENTION_DAYS", 30)

#: Hours to look back when querying Apple for reports.
FETCH_WINDOW_HOURS = _int_env("FETCH_WINDOW_HOURS", 24 * 7)

#: HTTP port to listen on.
PORT = _int_env("PORT", 8080)

#: Cookie signing secret: SECRET env, or a random one persisted in DATA_DIR.
_secret_file = os.path.join(DATA_DIR, "secret_key")
if os.environ.get("SECRET"):
    SECRET = os.environ["SECRET"].encode()
elif os.path.exists(_secret_file):
    with open(_secret_file, "rb") as f:
        SECRET = f.read().strip()
else:
    os.makedirs(DATA_DIR, exist_ok=True)
    SECRET = secrets.token_hex(32).encode()
    with open(_secret_file, "wb") as f:
        f.write(SECRET)
    try:
        os.chmod(_secret_file, 0o600)
    except OSError:
        pass
