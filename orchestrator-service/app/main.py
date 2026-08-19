from fastapi import FastAPI

from app.logging_conf import configure_logging
from app.routers import accounts, campaigns

configure_logging()

app = FastAPI(title="Orchestrator Service")
app.include_router(campaigns.router)
app.include_router(accounts.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
