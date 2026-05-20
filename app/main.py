"""FastAPI app entry point."""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.db.session import init_db
from app.routes import api, dashboard
from app.jobs import poll_active_shipments, daily_summary


scheduler = AsyncIOScheduler(timezone="Asia/Karachi")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    scheduler.add_job(
        poll_active_shipments,
        IntervalTrigger(hours=3),
        id="tracking_poll",
        replace_existing=True,
    )
    scheduler.add_job(
        daily_summary,
        CronTrigger(hour=9, minute=0),
        id="daily_summary",
        replace_existing=True,
    )
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Ecom Agent", version="0.1", lifespan=lifespan)
app.include_router(api.router)
app.include_router(dashboard.router)

# Mount static dir only if it exists (Railway may not deploy empty dirs).
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/health")
async def health():
    return {"status": "ok"}
