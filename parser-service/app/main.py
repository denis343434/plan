from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.logging_conf import configure_logging
from app.routers import parse

configure_logging()

app = FastAPI(title="Parser Service")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(parse.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
