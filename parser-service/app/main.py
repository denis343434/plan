from fastapi import FastAPI

from app.logging_conf import configure_logging
from app.routers import parse

configure_logging()

app = FastAPI(title="Parser Service")
app.include_router(parse.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
