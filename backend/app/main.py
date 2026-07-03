from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import export_, import_, progress, reels, sessions
from app.core.config import settings

app = FastAPI(title="ReelScribe", version="0.1.0")

# FRONTEND_URL: "*" или список доменов через запятую (Vercel + Render-статик и т.п.)
if settings.frontend_url == "*":
    origins = ["*"]
else:
    origins = [o.strip() for o in settings.frontend_url.split(",") if o.strip()]
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
