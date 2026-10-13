"""Apple account handling and Find My report fetching, on top of `findmy`.

All Apple network operations run through this module with a lock: the web
routes (login, 2FA, manual refresh) and the background poller share one
serialized path so we never run two Apple sessions concurrently.

The account state is persisted to DATA_DIR/apple_account.json and reloaded
for every operation, which keeps the aiohttp session lifecycle simple.
"""

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from . import config, db
from findmy.keys import KeyPair
from findmy.reports import (
    AsyncAppleAccount,
    AsyncSmsSecondFactor,
    LoginState,
    RemoteAnisetteProvider,
)

log = logging.getLogger("apple")

_lock = threading.Lock()
_STATE_FILE = os.path.join(config.DATA_DIR, "apple_account.json")

# Apple limits how many hashed keys make sense per request; batch into chunks.
_CHUNK_SIZE = 250


def _anisette() -> RemoteAnisetteProvider:
    return RemoteAnisetteProvider(config.ANISETTE_URL)


def _load_account() -> AsyncAppleAccount:
    """Account from persisted state, or a fresh logged-out one."""
    state = None
    if os.path.exists(_STATE_FILE):
        with open(_STATE_FILE) as f:
            state = json.load(f)
    return AsyncAppleAccount(_anisette(), state_info=state)


def _save_account(account: AsyncAppleAccount) -> None:
    with open(_STATE_FILE, "w") as f:
        json.dump(account.to_json(), f)
    try:
        os.chmod(_STATE_FILE, 0o600)
    except OSError:
        pass


def has_account() -> bool:
    return os.path.exists(_STATE_FILE)


def logout() -> None:
    with _lock:
        if os.path.exists(_STATE_FILE):
            os.remove(_STATE_FILE)


async def _login_locked(username: str, password: str) -> dict:
    account = AsyncAppleAccount(_anisette())
    try:
        state = await account.login(username, password)
        if state == LoginState.REQUIRE_2FA:
            methods = []
            for m in await account.get_2fa_methods():
                if isinstance(m, AsyncSmsSecondFactor):
                    phone = str(m.phone_number)
                    info = {
                        "kind": "sms",
                        "phone_id": m.phone_number_id,
                        # Only the last two digits survive Apple's redaction;
                        # keep the tail of whatever the string carries.
                        "last_digits": phone[-2:] if phone else "??",
                    }
                else:
                    info = {"kind": "trusted_device"}
                methods.append(info)
            # Keep the half-authenticated account around for the 2FA step.
            _save_account(account)
            return {"state": "2fa_required", "methods": methods}
        _save_account(account)
        return _account_status_locked(account)
    finally:
        await account.close()


async def _submit_2fa_locked(method: str, code: str, phone_id: int | None) -> dict:
    account = _load_account()
    try:
        if method == "sms":
            if phone_id is None:
                return {"state": "error", "error": "phone_id required for SMS 2FA"}
            state = await account.sms_2fa_submit(phone_id, code)
        else:
            state = await account.td_2fa_submit(code)
        _save_account(account)
        if state == LoginState.LOGGED_IN:
            return _account_status_locked(account)
        return {"state": "error", "error": f"2FA rejected (state: {state.name})"}
    finally:
        await account.close()


async def _request_sms_locked(phone_id: int) -> dict:
    account = _load_account()
    try:
        await account.sms_2fa_request(phone_id)
        return {"state": "sms_sent"}
    finally:
        await account.close()


def _account_status_locked(account: AsyncAppleAccount) -> dict:
    state = account.login_state
    status = {
        "LOGGED_OUT": "logged_out",
        "REQUIRE_2FA": "2fa_required",
        "AUTHENTICATED": "authenticated",
        "LOGGED_IN": "logged_in",
    }.get(state.name, state.name.lower())
    out = {"state": status}
    if status == "logged_in":
        out["account_name"] = account.account_name
    return out


def login(username: str, password: str) -> dict:
    """Blocking wrapper used by routes; runs its own event loop."""
    with _lock:
        return asyncio.run(_login_locked(username, password))


def submit_2fa(method: str, code: str, phone_id: int | None = None) -> dict:
    with _lock:
        return asyncio.run(_submit_2fa_locked(method, code, phone_id))


def request_sms(phone_id: int) -> dict:
    with _lock:
        return asyncio.run(_request_sms_locked(phone_id))


def account_status() -> dict:
    with _lock:
        if not has_account():
            return {"state": "logged_out"}
        account = _load_account()
        try:
            return _account_status_locked(account)
        finally:
            asyncio.run(account.close())


def refresh_locations() -> dict:
    """Fetch and decrypt location reports for every key of every tag.

    Returns a summary dict: per-tag new report counts, plus totals.
    """
    with _lock:
        if not has_account():
            return {"error": "no_apple_account"}

        tags = db.list_tags()
        summary = {"tags": {}, "total_new": 0}
        if not tags:
            return summary

        return asyncio.run(_refresh_locked(tags, summary))


async def _refresh_locked(tags: list[dict], summary: dict) -> dict:
    account = _load_account()
    try:
        if account.login_state != LoginState.LOGGED_IN:
            return {"error": f"apple_state_{account.login_state.name.lower()}"}

        since = datetime.now(timezone.utc) - timedelta(hours=config.FETCH_WINDOW_HOURS)

        for tag in tags:
            keypairs = {
                index: KeyPair(priv, name=f"{tag['name']}#{index}")
                for index, priv in db.tag_private_keys(tag["id"])
            }
            if not keypairs:
                summary["tags"][str(tag["id"])] = 0
                continue

            new_reports = 0
            all_keys = list(keypairs.values())
            for start in range(0, len(all_keys), _CHUNK_SIZE):
                chunk = all_keys[start : start + _CHUNK_SIZE]
                fetched = await account.fetch_location_history(chunk)
                for key, reports in fetched.items():
                    index = next(i for i, k in keypairs.items() if k is key)
                    rows = []
                    for report in reports:
                        if not report.can_decrypt(key):
                            continue
                        report.decrypt(key)
                        rows.append(
                            {
                                "key_index": index,
                                "ts": report.timestamp.isoformat(),
                                "lat": report.latitude,
                                "lon": report.longitude,
                                "accuracy": report.horizontal_accuracy,
                                "confidence": report.confidence,
                            }
                        )
                    if rows:
                        new_reports += db.insert_reports(tag["id"], rows)
            summary["tags"][str(tag["id"])] = new_reports
            summary["total_new"] += new_reports
        return summary
    finally:
        await account.close()
