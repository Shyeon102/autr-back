from fastapi import APIRouter

from autr.ops.heartbeat import heartbeat


router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return heartbeat()


@router.get("/ready")
async def ready() -> dict:
    return {"status": "ready"}
