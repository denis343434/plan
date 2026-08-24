from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.logging_conf import configure_logging
from app.routers import accounts, config, parse

configure_logging()

app = FastAPI(title="Parser Service")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(parse.router)
app.include_router(config.router)
app.include_router(accounts.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
