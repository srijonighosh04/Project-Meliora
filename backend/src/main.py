import sys
from pathlib import Path
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Ensure project root and backend are in sys.path
_ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from src.api.middleware import RequestCorrelationMiddleware, RequestSizeLimitMiddleware
from src.api.router import api_v1_router, root_health_router
from src.api.v1.measurements_mock import router as measurements_pulse_router
from src.core.config import Settings, get_settings
from src.core.database import close_db_connection
from src.core.exceptions import register_exception_handlers
from src.core.logging import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Lifespan context manager handling application startup and graceful shutdown."""
    settings = getattr(app.state, "settings", get_settings())
    logger = setup_logging(
        log_level=settings.LOG_LEVEL,
        environment=settings.ENVIRONMENT,
    )
    logger.info(
        f"Starting {settings.PROJECT_NAME} v{settings.VERSION} "
        f"[env: {settings.ENVIRONMENT}]"
    )

    # Initialize tables and seed development users if in development environment
    if settings.ENVIRONMENT in ("development", "staging"):
        try:
            from src.core.database import get_engine, get_session_factory
            from src.models.base import Base
            import src.models  # load models
            from src.core.seed import seed_dev_data

            engine = get_engine()
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            session_factory = get_session_factory()
            async with session_factory() as session:
                await seed_dev_data(session)
            logger.info("Local development database initialized and seeded.")
        except Exception as e:
            logger.warning(f"Database auto-initialization note: {e}")

    yield

    logger.info("Initiating application shutdown...")
    await close_db_connection()
    logger.info("Application shutdown complete.")


def create_app(settings: Settings | None = None) -> FastAPI:
    """FastAPI application factory."""
    if settings is None:
        settings = get_settings()

    # Determine OpenAPI documentation visibility
    # In development/staging, OpenAPI is exposed at /docs and /redoc
    is_dev = settings.ENVIRONMENT in ("development", "staging", "testing")
    openapi_url = f"{settings.API_V1_PREFIX}/openapi.json" if is_dev else None
    docs_url = "/docs" if is_dev else None
    redoc_url = "/redoc" if is_dev else None

    app = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        description=(
            "NASA Space Apps Challenge — Astronaut Health Monitoring Software Backend. "
            "Exposes secure REST APIs for crew physiological monitoring and flight surgeon clinical review."
        ),
        openapi_url=openapi_url,
        docs_url=docs_url,
        redoc_url=redoc_url,
        lifespan=lifespan,
    )

    # Store settings in app state for access in routes/dependencies
    app.state.settings = settings

    # 1. Register Centralized Exception Handlers
    register_exception_handlers(app)

    # 2. Add Request Correlation & Safe Logging Middleware
    app.add_middleware(RequestSizeLimitMiddleware)
    app.add_middleware(RequestCorrelationMiddleware)

    # 3. Add CORS Middleware
    cors_origins = list(dict.fromkeys(settings.ALLOWED_ORIGINS + ["http://localhost:5173", "http://localhost:3000"]))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=r"https://.*\.vercel\.app",
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    # 4. Include Routers
    app.include_router(root_health_router)
    app.include_router(api_v1_router)
    app.include_router(measurements_pulse_router)

    return app


# Application entry point instance
app = create_app()

if __name__ == "__main__":
    current_settings = get_settings()
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=8000,
        reload=current_settings.ENVIRONMENT == "development",
        log_level=current_settings.LOG_LEVEL.lower(),
    )

