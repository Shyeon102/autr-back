from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from autr.api.middleware.request_id import RequestIDMiddleware
from autr.api.routes.health import router as health_router
from autr.api.routes.status import router as status_router
from autr.api.routes.trading import router as trading_router
from autr.config.settings import get_settings
from autr.ops.lifecycle import on_shutdown, on_startup
from autr.ops.logging import configure_logging


settings = get_settings()
configure_logging()

app = FastAPI(title=settings.app_name, version=settings.app_version)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(health_router)
app.include_router(status_router)
app.include_router(trading_router, prefix="/api")


@app.on_event("startup")
async def _startup() -> None:
    await on_startup(app)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await on_shutdown(app)


@app.get("/")
async def root() -> dict:
    return {"message": "AUTR backend", "status": "running"}
