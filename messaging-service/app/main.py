from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.logging_conf import configure_logging
from app.routers import inbox, reply, send

configure_logging()

app = FastAPI(title="Messaging Service")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(send.router)
app.include_router(inbox.router)
app.include_router(reply.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
