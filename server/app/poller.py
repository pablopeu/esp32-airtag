"""Background poller: refreshes locations periodically and prunes old reports."""

import logging
import threading
import time

from . import apple, config, db

log = logging.getLogger("poller")

_stop = threading.Event()
_thread: threading.Thread | None = None
_manual = threading.Event()


def start() -> None:
    global _thread
    if config.POLL_MINUTES <= 0:
        log.info("auto-polling disabled (POLL_MINUTES=0)")
        return
    _thread = threading.Thread(target=_run, name="poller", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()


def trigger_manual_refresh() -> None:
    """Ask the poller for an immediate refresh round."""
    _manual.set()


def _run() -> None:
    interval = config.POLL_MINUTES * 60
    # Stagger the first poll a bit so the app is fully up first.
    time.sleep(15)
    while not _stop.is_set():
        started = time.time()
        try:
            summary = apple.refresh_locations()
            if "error" in summary:
                log.warning("refresh failed: %s", summary["error"])
            else:
                log.info("refresh ok: %d new reports", summary.get("total_new", 0))
                db.meta_set("last_refresh", str(int(time.time())))
                db.meta_set("last_refresh_status", "ok")
                db.prune_old_reports()
        except Exception:  # noqa: BLE001 - poller must never die
            log.exception("refresh crashed")
            db.meta_set("last_refresh", str(int(time.time())))
            db.meta_set("last_refresh_status", "error")
        # Wait for the interval, or wake early on a manual request.
        _manual.wait(timeout=max(30.0, interval - (time.time() - started)))
        _manual.clear()
