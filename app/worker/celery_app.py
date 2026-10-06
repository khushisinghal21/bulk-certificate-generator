import os

from celery import Celery

from app.config import get_settings

# Make each prefork child initialise Celery's task tracer itself instead of relying on state
# inherited from the parent. Without this, on macOS every task fails with
# "ValueError: not enough values to unpack (expected 3, got 0)". Harmless on Linux.
os.environ.setdefault("FORKED_BY_MULTIPROCESSING", "1")

settings = get_settings()

celery_app = Celery("certificates", broker=settings.celery_broker_url, include=["app.worker.tasks"])
celery_app.conf.update(
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=True,
    task_serializer="json",
    accept_content=["json"],
    # Job state lives in the database, so no result backend is needed.
    task_ignore_result=True,
    # Acknowledge a task only after it finishes: if a worker dies mid-batch the broker
    # redelivers it, and the claim/lease logic resumes where it stopped.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Batches are CPU-bound; don't let one worker hoard tasks other workers could run.
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
)
