from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db import ensure_indexes, get_db
from .routers import auth, masterdata, reports, statements, transactions
from .services.seed import seed


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = get_db()
    ensure_indexes(db)
    seed(db)
    yield


app = FastAPI(title="LW / CW Expense Tracker API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],   # lets the frontend read download file names
)

API = "/api"
app.include_router(auth.router, prefix=API)
app.include_router(statements.router, prefix=API)
app.include_router(transactions.router, prefix=API)
app.include_router(masterdata.router, prefix=API)
app.include_router(reports.router, prefix=API)


@app.get("/api/health")
def health():
    return {"status": "ok"}
