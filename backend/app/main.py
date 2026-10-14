from __future__ import annotations

import logging
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from app.api.debates import router
from app.api.admin import router as admin_router
from app.api.errors import APIError
from app.cli import load_topics
from app.config import Settings
from app.core.identity import identify
from app.core.quotas import clear_old_identities, weekly_identity_cleanup
from app.core.ratelimit import RequestLimits
from app.db.base import create_database
from app.db.migrate import upgrade_database
from app.engine.lifecycle import ExecutionManager, reconcile
from app.engine.provider import FakeProvider
from app.ops.backup import BackupService

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, provider_factory=FakeProvider, model_id: str = "fake",
               retry_policy=None, attempt_timeout: float = 900, shutdown_grace: float = 10,
               supervisor_interval: float = 5) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        settings.validate_shared()
        if settings.deployment_mode == "shared":
            from app.engine.budget import price_for
            if settings.cost_cap_cents is None:
                raise RuntimeError("Shared deployment requires MAX_RESERVED_COST_24H_USD")
            try:
                price_for(settings.groq_model)
            except ValueError:
                raise RuntimeError("Shared deployment requires confirmed model pricing") from None
        engine, sessions = create_database(settings.database_path)
        try:
            await upgrade_database(engine)
            await reconcile(sessions, set())
            executions = ExecutionManager(sessions, provider_factory, model_id, retry_policy=retry_policy,
                                          attempt_timeout=attempt_timeout, shutdown_grace=shutdown_grace,
                                          supervisor_interval=supervisor_interval, settings=settings)
            app.state.engine = engine
            app.state.sessions = sessions
            app.state.executions = executions
            app.state.topics = {topic.id: topic for topic in load_topics()}
            cleanup = None
            if settings.deployment_mode == "shared":
                await clear_old_identities(sessions)
                cleanup = asyncio.create_task(weekly_identity_cleanup(sessions), name="identity-cleanup")
            executions.start_supervisor()
            app.state.ready = True
            backups = BackupService(settings.database_path)
            app.state.backups = backups
            backups.start(lambda: not executions.tasks)
            try:
                yield
            finally:
                app.state.ready = False
                await backups.close()
                if cleanup is not None:
                    cleanup.cancel()
                    await asyncio.gather(cleanup, return_exceptions=True)
                await executions.close()
        finally:
            await engine.dispose()

    app = FastAPI(title="BotArena", lifespan=lifespan)
    app.state.ready = False
    app.state.settings = settings
    app.state.request_limits = RequestLimits()

    @app.middleware("http")
    async def request_guards(request, call_next):
        try:
            if settings.deployment_mode == "shared" and request.url.path.startswith("/api/") and request.method != "OPTIONS":
                request.state.identity = identify(request, settings)
                app.state.request_limits.check(request)
            response = await call_next(request)
        except APIError as exc:
            response = await api_error(request, exc)
        except Exception:
            logger.error("API request failed unexpectedly")
            response = JSONResponse({"error": {"code": "internal_error", "message": "Request could not be completed."}}, status_code=500)
        return response

    @app.exception_handler(APIError)
    async def api_error(_request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status,
                            headers={"Retry-After": str(exc.retry_after or 5)} if exc.retry_after is not None or exc.status == 503 else None)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _exc):
        return JSONResponse({"error": {"code": "invalid_request", "message": "Invalid request shape."}}, status_code=422)

    @app.exception_handler(SQLAlchemyError)
    async def storage_error(_request, _exc):
        return JSONResponse({"error": {"code": "storage_unavailable", "message": "Storage is temporarily unavailable."}},
                            status_code=503, headers={"Retry-After": "5"})

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        return JSONResponse({"error": {"code": "http_error", "message": "Request could not be handled."}}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request, _exc):
        logger.error("API request failed unexpectedly")
        return JSONResponse({"error": {"code": "internal_error", "message": "Request could not be completed."}},
                            status_code=500, headers={"Cache-Control": "no-store"} if request.url.path.startswith("/api/debates") else None)

    @app.get("/api/topics")
    async def topics():
        return {"topics": [asdict(topic) for topic in app.state.topics.values()]}

    @app.get("/api/health")
    async def health():
        if not app.state.ready:
            raise APIError(503, "not_ready", "Server is not ready.")
        async with app.state.sessions() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready"}

    app.include_router(router)
    app.include_router(admin_router)
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-Debate-Token"],
                       expose_headers=["Retry-After"])

    @app.middleware("http")
    async def cache_policy(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/api/debates", "/api/admin")):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


app = create_app()
