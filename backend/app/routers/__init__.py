from fastapi import APIRouter

from app.routers import core, corrections

api = APIRouter()
api.include_router(core.router)
api.include_router(corrections.router)
