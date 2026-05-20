from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app import templating as _templating  # noqa: F401 — register Jinja filters at import
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.routers import admin_routes, auth_routes, policy_routes, web
from app.startup_checks import validate_settings

BASE_DIR = Path(__file__).resolve().parent


def create_app() -> FastAPI:
    settings = get_settings()
    validate_settings(settings)
    app = FastAPI(title=settings.app_title)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        max_age=settings.session_max_age_seconds,
        same_site=settings.session_same_site,
        https_only=settings.secure_cookies,
    )

    @app.exception_handler(HTTPException)
    async def _redirect_303_handler(request: Request, exc: HTTPException):
        loc = None
        if exc.status_code == 303 and exc.headers:
            for hk, hv in exc.headers.items():
                if str(hk).lower() == "location":
                    loc = hv
                    break
        if loc:
            if request.headers.get("HX-Request"):
                from starlette.responses import Response

                return Response(status_code=401, headers={"HX-Redirect": loc})
            return RedirectResponse(url=loc, status_code=303)
        return await http_exception_handler(request, exc)

    app.include_router(auth_routes.router)
    app.include_router(admin_routes.router)
    app.include_router(policy_routes.router)
    app.include_router(web.router)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    return app


app = create_app()
