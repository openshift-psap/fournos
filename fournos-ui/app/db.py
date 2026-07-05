"""PostgreSQL persistence layer using SQLAlchemy async."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import uuid4

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------

class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    name = Column(String, unique=True, nullable=False, index=True)
    project = Column(String, nullable=False, index=True)
    preset = Column(String, default="")
    cluster = Column(String, nullable=False, index=True)
    pipeline = Column(String, default="")
    owner = Column(String, default="", index=True)
    status = Column(String, default="Pending", index=True)
    message = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Float, nullable=True)
    mlflow_url = Column(String, default="")
    ci_artifacts_url = Column(String, default="")
    config_overrides = Column(JSONB, default=dict)
    tags = Column(ARRAY(String), default=list)
    fjob_spec = Column(JSONB, default=dict)
    fjob_status = Column(JSONB, default=dict)
    error_message = Column(Text, default="")
    triggered_by_schedule = Column(String, nullable=True, index=True)
    trigger_type = Column(String, default="manual")

    events = relationship("JobEvent", back_populates="job", cascade="all, delete-orphan")


class JobEvent(Base):
    __tablename__ = "job_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    job_id = Column(String, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    phase = Column(String, nullable=False)
    message = Column(Text, default="")
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    job = relationship("Job", back_populates="events")


# ---------------------------------------------------------------------------
# Engine & session factory
# ---------------------------------------------------------------------------

engine = create_async_engine(settings.database_url, echo=False, pool_size=5, max_overflow=10)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Create all tables (development convenience -- use Alembic in production)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured")


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

async def upsert_job(session: AsyncSession, **kwargs: Any) -> Job:
    """Insert or update a job record keyed by name (atomic)."""
    if "id" not in kwargs:
        kwargs["id"] = str(uuid4())

    update_cols = {k: v for k, v in kwargs.items() if k not in ("id", "name") and v is not None}

    stmt = (
        pg_insert(Job)
        .values(**kwargs)
        .on_conflict_do_update(index_elements=["name"], set_=update_cols)
        .returning(Job)
    )
    result = await session.execute(stmt)
    job = result.scalar_one()
    return job


async def add_job_event(
    session: AsyncSession, job_id: str, phase: str, message: str = ""
) -> JobEvent:
    """Record a status transition."""
    event = JobEvent(job_id=job_id, phase=phase, message=message)
    session.add(event)
    await session.flush()
    return event


async def get_job_by_name(session: AsyncSession, name: str) -> Job | None:
    result = await session.execute(select(Job).where(Job.name == name))
    return result.scalar_one_or_none()


async def list_jobs(
    session: AsyncSession,
    *,
    project: str | None = None,
    cluster: str | None = None,
    status: str | None = None,
    owner: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[Sequence[Job], int]:
    """List archived jobs with optional filters. Returns (jobs, total_count)."""
    stmt = select(Job)
    count_stmt = select(func.count(Job.id))

    if project:
        stmt = stmt.where(Job.project == project)
        count_stmt = count_stmt.where(Job.project == project)
    if cluster:
        stmt = stmt.where(Job.cluster == cluster)
        count_stmt = count_stmt.where(Job.cluster == cluster)
    if status:
        stmt = stmt.where(Job.status == status)
        count_stmt = count_stmt.where(Job.status == status)
    if owner:
        stmt = stmt.where(Job.owner == owner)
        count_stmt = count_stmt.where(Job.owner == owner)

    stmt = stmt.order_by(Job.created_at.desc()).limit(limit).offset(offset)

    result = await session.execute(stmt)
    jobs = result.scalars().all()

    count_result = await session.execute(count_stmt)
    total = count_result.scalar() or 0

    return jobs, total


async def list_jobs_by_schedule(
    session: AsyncSession, schedule_name: str,
) -> Sequence[Job]:
    """List all jobs triggered by a specific schedule."""
    result = await session.execute(
        select(Job)
        .where(Job.triggered_by_schedule == schedule_name)
        .order_by(Job.created_at.desc())
    )
    return result.scalars().all()


async def delete_job_by_name(session: AsyncSession, name: str) -> bool:
    """Delete a job and all related logs/events by name. Returns True if deleted."""
    job = await get_job_by_name(session, name)
    if job is None:
        return False
    await session.delete(job)
    await session.flush()
    return True


async def get_job_events(session: AsyncSession, job_id: str) -> Sequence[JobEvent]:
    result = await session.execute(
        select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.timestamp)
    )
    return result.scalars().all()
