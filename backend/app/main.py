from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import export_, import_, progress, reels, sessions
from app.core.config import settings

app = FastAPI(title="ReelScribe", version="0.1.0")

origins = ["*"] if settings.frontend_url == "*" else [settings.frontend_url]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(import_.router, prefix="/api")
app.include_router(sessions.router, prefix="/api")
app.include_router(reels.router, prefix="/api")
app.include_router(export_.router, prefix="/api")
app.include_router(progress.router, prefix="/api")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
