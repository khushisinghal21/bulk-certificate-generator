import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from app.api import certificates, jobs
from app.api.deps import DbSession, require_api_key
from app.config import get_settings
from app.database import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=get_settings().app_name,
        version="1.0.0",
        description=(
            "Submit a list of recipients, generate a PDF certificate for each one in the "
            "background, track progress and download the results."
        ),
        lifespan=lifespan,
    )

    api = APIRouter(prefix="/api/v1")
    protected = [Depends(require_api_key)]
    api.include_router(jobs.router, dependencies=protected)
    api.include_router(certificates.router, dependencies=protected)
    api.include_router(certificates.public_router)
    app.include_router(api)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/docs")

    @app.get("/health", tags=["health"])
    def health(db: DbSession) -> dict[str, str]:
        db.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app


app = create_app()
