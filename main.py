import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

load_dotenv()

from models import init_db  # noqa: E402  (load env first)
from routes import admin_routes, auth_routes, media_routes, oauth_routes, stripe_routes, web_routes
from routes.api_routes import router as api_router
from services.scheduler import scheduler_loop

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Gootier", lifespan=lifespan)

origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth_routes.router)
app.include_router(web_routes.router)
app.include_router(oauth_routes.router)
app.include_router(stripe_routes.router)
app.include_router(admin_routes.router)
app.include_router(media_routes.router)
app.include_router(api_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
