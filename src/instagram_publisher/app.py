import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from instagram_publisher.api_dependencies import require_admin
from instagram_publisher.config import Settings
from instagram_publisher.connections import router as connections_router
from instagram_publisher.db import make_session_factory
from instagram_publisher.guards import RequestGuard
from instagram_publisher.legacy_routes import router as legacy_router
from instagram_publisher.lifecycle_routes import router as lifecycle_router
from instagram_publisher.media import MAX_VIDEO_BYTES
from instagram_publisher.media_routes import router as media_router
from instagram_publisher.meta import MetaClient
from instagram_publisher.post_routes import router as post_router
from instagram_publisher.recovery_routes import router as recovery_router
from instagram_publisher.status_routes import router as status_router
from instagram_publisher.unified import UnifiedMetaClient


def configure_security(app, settings):
    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path == "/api/accounts/import-unified-token":
            return JSONResponse(
                {"detail": "Provide a token string and a valid Instagram username only"}, status_code=422
            )
        return await request_validation_exception_handler(request, exc)

    @app.middleware("http")
    async def security_headers(request, call_next):
        if request.method in {"POST", "PUT", "PATCH"}:
            length = request.headers.get("content-length")
            if length and (not length.isdigit() or int(length) > MAX_VIDEO_BYTES + 2_000_000):
                return JSONResponse({"detail": "Request body exceeds the upload limit"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if settings.public_base_url.startswith("https://"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    app.add_middleware(
        RequestGuard,
        admin_key=settings.admin_api_key.get_secret_value(),
        upload_limit=MAX_VIDEO_BYTES + 2_000_000,
    )


def create_app(settings: Settings | None = None, session_factory=None, meta_client=None):
    settings = settings or Settings()
    session_factory = session_factory or make_session_factory(settings.database_url)
    settings.media_root.mkdir(parents=True, exist_ok=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    owned_client = meta_client is None
    client = meta_client or MetaClient(
        app_id=settings.meta_app_id,
        app_secret=settings.meta_app_secret.get_secret_value(),
        redirect_uri=settings.redirect_uri,
        api_version=settings.meta_api_version,
    )
    unified_client = UnifiedMetaClient(
        app_id=settings.meta_unified_app_id,
        app_secret=settings.meta_unified_app_secret.get_secret_value(),
        redirect_uri=settings.redirect_uri,
        api_version=settings.meta_api_version,
    )

    @asynccontextmanager
    async def lifespan(_app):
        yield
        if owned_client:
            client.close()
        unified_client.close()

    app = FastAPI(
        title="Instagram Publisher",
        description=(
            "Private Instagram publishing API. Upload media, create a draft, review its revision, "
            "then approve to queue it. The separate worker performs publishing. Demo mode never posts "
            "to Instagram. Use Authorize with your admin API key; secrets never appear in responses."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.meta_client = client
    app.state.unified_client = unified_client
    app.include_router(connections_router, dependencies=[Depends(require_admin)])
    for router in (
        legacy_router,
        lifecycle_router,
        media_router,
        post_router,
        recovery_router,
        status_router,
    ):
        app.include_router(router)
    configure_security(app, settings)
    return app
