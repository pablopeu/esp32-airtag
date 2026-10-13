"""Find You tracking server: web UI for tracking any number of ESP32 tags.

Run with:  uvicorn app.app:app --host 0.0.0.0 --port 8080
"""

import logging
import os
import time
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import apple, config, db, importer, poller, security

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")

BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def _authed(request) -> bool:
    return bool(request.session.get("admin"))


def _require_admin(request):
    """Returns a RedirectResponse when not authenticated, else None."""
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    return None


# --- auth -------------------------------------------------------------------


async def login_page(request):
    if _authed(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


async def login_submit(request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    if security.check_admin_credentials(username, password):
        request.session["admin"] = username
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Invalid credentials"}, status_code=401
    )


async def logout(request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- dashboard --------------------------------------------------------------


async def index(request):
    redirect = _require_admin(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "config": config,
            "last_refresh": db.meta_get("last_refresh"),
            "refresh_status": db.meta_get("last_refresh_status"),
        },
    )


async def tag_page(request):
    redirect = _require_admin(request)
    if redirect:
        return redirect
    tag = db.get_tag(int(request.path_params["tag_id"]))
    if not tag:
        return HTMLResponse("Tag not found", status_code=404)
    return templates.TemplateResponse(request, "tag.html", {"tag": tag})


# --- JSON API ---------------------------------------------------------------


async def api_tags(request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"tags": db.list_tags()})


async def api_tag_reports(request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    tag_id = int(request.path_params["tag_id"])
    hours = float(request.query_params.get("hours", 24))
    return JSONResponse({"reports": db.reports_for_tag(tag_id, hours=hours)})


async def api_refresh(request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    poller.trigger_manual_refresh()
    return JSONResponse({"status": "refresh_requested"})


async def api_status(request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(
        {
            "apple": apple.account_status(),
            "last_refresh": db.meta_get("last_refresh"),
            "last_refresh_status": db.meta_get("last_refresh_status"),
        }
    )


# --- tag management ---------------------------------------------------------


async def api_tag_delete(request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db.delete_tag(int(request.path_params["tag_id"]))
    return JSONResponse({"status": "deleted"})


async def api_import(request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    form = await request.form()
    name = (form.get("name") or "").strip() or None
    single_tag = form.get("single_tag") in ("on", "true", "1")

    upload = form.get("plist")
    if upload is not None and getattr(upload, "filename", ""):
        data = await upload.read()
        try:
            tag_ids = importer.import_plist(data, name, single_tag)
        except Exception as exc:  # noqa: BLE001 - surface parse errors to the UI
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"tag_ids": tag_ids})

    text = form.get("keys") or ""
    if text.strip():
        try:
            tag_id = importer.import_text_keys(text, name or "Pasted keys")
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"tag_ids": [tag_id]})

    return JSONResponse({"error": "Provide a plist file or pasted keys"}, status_code=400)


# --- Apple account ----------------------------------------------------------


async def api_apple_login(request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    if not username or not password:
        return JSONResponse({"error": "username and password required"}, status_code=400)
    try:
        return JSONResponse(apple.login(username, password))
    except Exception as exc:  # noqa: BLE001
        log.exception("apple login failed")
        return JSONResponse({"error": str(exc)}, status_code=502)


async def api_apple_2fa(request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    form = await request.form()
    method = form.get("method") or "trusted_device"
    code = (form.get("code") or "").strip()
    phone_id = form.get("phone_id")
    try:
        return JSONResponse(
            apple.submit_2fa(method, code, int(phone_id) if phone_id else None)
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("2FA failed")
        return JSONResponse({"error": str(exc)}, status_code=502)


async def api_apple_sms_request(request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    form = await request.form()
    phone_id = form.get("phone_id")
    if not phone_id:
        return JSONResponse({"error": "phone_id required"}, status_code=400)
    try:
        return JSONResponse(apple.request_sms(int(phone_id)))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=502)


async def api_apple_logout(request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    apple.logout()
    return JSONResponse({"state": "logged_out"})


async def healthz(request):
    return JSONResponse({"ok": True, "ts": int(time.time())})


routes = [
    Route("/healthz", healthz),
    Route("/login", login_page, methods=["GET"]),
    Route("/login", login_submit, methods=["POST"]),
    Route("/logout", logout),
    Route("/", index),
    Route("/tag/{tag_id:int}", tag_page),
    Route("/api/tags", api_tags),
    Route("/api/tags/{tag_id:int}/reports", api_tag_reports),
    Route("/api/tags/{tag_id:int}/delete", api_tag_delete, methods=["POST"]),
    Route("/api/import", api_import, methods=["POST"]),
    Route("/api/refresh", api_refresh, methods=["POST"]),
    Route("/api/status", api_status),
    Route("/api/apple/login", api_apple_login, methods=["POST"]),
    Route("/api/apple/2fa", api_apple_2fa, methods=["POST"]),
    Route("/api/apple/sms", api_apple_sms_request, methods=["POST"]),
    Route("/api/apple/logout", api_apple_logout, methods=["POST"]),
    Mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static"),
]


@asynccontextmanager
async def _lifespan(_app):
    db.init_db()
    poller.start()
    try:
        yield
    finally:
        poller.stop()


app = Starlette(
    routes=routes,
    middleware=[
        Middleware(
            SessionMiddleware,
            secret_key=config.SECRET,
            session_cookie=security.SESSION_COOKIE,
            max_age=security.SESSION_TTL_S,
            same_site="lax",
            https_only=os.environ.get("HTTPS_ONLY", "1") == "1",
        )
    ],
    lifespan=_lifespan,
)
