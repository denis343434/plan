from fastapi import FastAPI

from app.logging_conf import configure_logging
from app.routers import send

configure_logging()

app = FastAPI(title="Messaging Service")
app.include_router(send.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
