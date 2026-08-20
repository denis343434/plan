from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.logging_conf import configure_logging
from app.routers import accounts, campaigns

configure_logging()

app = FastAPI(title="Orchestrator Service")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(campaigns.router)
app.include_router(accounts.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# Смонтирован последним: FastAPI матчит роуты по порядку регистрации, поэтому
# конкретные API-пути выше (/campaigns, /accounts, /health) имеют приоритет,
# а StaticFiles(html=True) отдаёт web-интерфейс на "/" и любые незанятые пути.
_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")
