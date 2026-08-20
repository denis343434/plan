from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.exceptions import NoAccountAvailableError, NotFoundError
from app.routers import accounts, campaigns, leads, messages, templates

app = FastAPI(title="Data Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(leads.router)
app.include_router(accounts.router)
app.include_router(messages.router)
app.include_router(campaigns.router)
app.include_router(templates.router)


@app.exception_handler(NotFoundError)
def handle_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(NoAccountAvailableError)
def handle_no_account_available(request: Request, exc: NoAccountAvailableError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
