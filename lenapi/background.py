"""Small in-process background task support for route handlers."""

import asyncio
import inspect
import json
import threading
import time
import uuid
from collections.abc import Callable

from sqlalchemy import and_, or_, select, update

from .models import BACKGROUND_JOBS_TABLE

from .logging import get_logger

logger = get_logger("background")

_MEMORY_QUEUE_LOCK = threading.RLock()
_MEMORY_QUEUE_JOBS: dict[str, dict[str, dict]] = {}


class BackgroundTasks:
    """Collect functions to run after the response body has been sent."""

    def __init__(self) -> None:
        self._tasks: list[tuple[Callable, tuple, dict]] = []

    def add_task(self, function: Callable, *args, **kwargs) -> None:
        if not callable(function):
            raise TypeError("background task must be callable")
        self._tasks.append((function, args, kwargs))

    async def run(self) -> None:
        tasks, self._tasks = self._tasks, []
        for function, args, kwargs in tasks:
            try:
                if inspect.iscoroutinefunction(function):
                    await function(*args, **kwargs)
                else:
                    result = await asyncio.to_thread(function, *args, **kwargs)
                    if inspect.isawaitable(result):
                        await result
            except Exception:
                logger.exception("Background task %r failed", function)


class BackgroundQueue:
    """A database-, Redis-, or in-memory-cache-backed queue with an async worker.

    Import/register the same task functions in the API and worker process. Jobs
    use JSON-compatible positional and keyword arguments, and delivery is at
    least once, so handlers should be idempotent.
    """

    def __init__(self, database=None, *, backend: str = "database", redis_url: str | None = None,
                 queue_name: str = "default", max_attempts: int = 3, retry_delay: float = 5,
                 lease_seconds: float = 60) -> None:
        backend = backend.lower()
        if backend in {"cache", "system", "system_cache"}:
            backend = "memory"
        if backend not in {"database", "redis", "memory"}:
            raise ValueError("BackgroundQueue backend must be 'database', 'redis', or 'memory'/'cache'")
        if backend == "database" and (database is None or not hasattr(database, "engine")):
            raise ValueError("Database BackgroundQueue requires a configured lenApi Database")
        if backend == "redis" and not redis_url:
            raise ValueError("Redis BackgroundQueue requires redis_url")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if retry_delay < 0 or lease_seconds <= 0:
            raise ValueError("retry_delay must be non-negative and lease_seconds must be positive")
        self.backend = backend
        self.database = database
        self.engine = database.engine if database is not None else None
        self.max_attempts = max_attempts
        self.retry_delay = float(retry_delay)
        self.lease_seconds = float(lease_seconds)
        self._tasks: dict[str, Callable] = {}
        self.queue_name = queue_name
        self._memory_jobs = _MEMORY_QUEUE_JOBS.setdefault(queue_name, {})
        self._stream = f"lenapi:jobs:{queue_name}"
        self._group = f"lenapi-workers:{queue_name}"
        self._consumer = uuid.uuid4().hex
        self._redis = None
        if backend == "redis":
            try:
                from redis.asyncio import Redis
            except ImportError as exc:
                raise ImportError("Redis BackgroundQueue requires `pip install 'lenapi[redis]'`") from exc
            self._redis = Redis.from_url(redis_url, decode_responses=True)

    def task(self, name: str | None = None):
        """Register a worker handler with ``@queue.task('send_email')``."""
        def register(function):
            task_name = name or f"{function.__module__}.{function.__name__}"
            if not task_name or len(task_name) > 200:
                raise ValueError("task names must contain 1 to 200 characters")
            if task_name in self._tasks and self._tasks[task_name] is not function:
                raise ValueError(f"A background task named {task_name!r} is already registered")
            self._tasks[task_name] = function
            return function
        return register

    async def enqueue(self, task_name: str, *args, **kwargs) -> str:
        """Queue a named task and return its job ID."""
        if not isinstance(task_name, str) or not task_name or len(task_name) > 200:
            raise ValueError("task_name must contain 1 to 200 characters")
        payload = {"args": args, "kwargs": kwargs}
        encoded = json.dumps(payload, separators=(",", ":"))
        job_id = uuid.uuid4().hex
        now = time.time()

        if self.backend == "memory":
            with _MEMORY_QUEUE_LOCK:
                self._memory_jobs[job_id] = {
                    "job_id": job_id,
                    "task_name": task_name,
                    "payload": json.loads(encoded),
                    "status": "queued",
                    "attempts": 0,
                    "available_at": now,
                    "locked_until": None,
                    "last_error": None,
                    "created_at": now,
                    "completed_at": None,
                }
            return job_id

        if self.backend == "redis":
            event_id = await self._redis.xadd(self._stream, {
                "job_id": job_id,
                "task_name": task_name,
                "payload": encoded,
                "attempts": "0",
                "available_at": str(now),
            })
            await self._redis.hset(self._redis_state_key(job_id), mapping={
                "status": "queued", "attempts": "0", "task_name": task_name,
                "stream_id": event_id, "last_error": "",
            })
            return job_id

        def insert_job():
            with self.engine.begin() as connection:
                connection.execute(BACKGROUND_JOBS_TABLE.insert().values(
                    job_id=job_id,
                    task_name=task_name,
                    payload=json.loads(encoded),
                    status="queued",
                    attempts=0,
                    available_at=now,
                    created_at=now,
                ))

        await asyncio.to_thread(insert_job)
        return job_id

    def _claim(self):
        now = time.time()
        ready = and_(BACKGROUND_JOBS_TABLE.c.status == "queued",
                     BACKGROUND_JOBS_TABLE.c.available_at <= now)
        expired = and_(BACKGROUND_JOBS_TABLE.c.status == "running",
                       BACKGROUND_JOBS_TABLE.c.locked_until <= now)
        statement = (
            select(BACKGROUND_JOBS_TABLE)
            .where(or_(ready, expired))
            .order_by(BACKGROUND_JOBS_TABLE.c.available_at, BACKGROUND_JOBS_TABLE.c.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        with self.engine.begin() as connection:
            row = connection.execute(statement).mappings().first()
            if row is None:
                return None
            attempts = row["attempts"] + 1
            connection.execute(
                update(BACKGROUND_JOBS_TABLE)
                .where(BACKGROUND_JOBS_TABLE.c.job_id == row["job_id"])
                .values(status="running", attempts=attempts, locked_until=now + self.lease_seconds)
            )
            return {**row, "attempts": attempts}

    def _claim_memory(self):
        now = time.time()
        with _MEMORY_QUEUE_LOCK:
            eligible = [
                job for job in self._memory_jobs.values()
                if (job["status"] == "queued" and job["available_at"] <= now)
                or (job["status"] == "running" and (job["locked_until"] or 0) <= now)
            ]
            if not eligible:
                return None
            job = min(eligible, key=lambda item: (item["available_at"], item["created_at"]))
            job["status"] = "running"
            job["attempts"] += 1
            job["locked_until"] = now + self.lease_seconds
            job["last_error"] = None
            return dict(job)

    def _finish_memory(self, job_id: str, attempts: int, *, error: str | None = None) -> None:
        now = time.time()
        with _MEMORY_QUEUE_LOCK:
            job = self._memory_jobs.get(job_id)
            if job is None:
                return
            job["locked_until"] = None
            if error is None:
                job.update(status="done", last_error=None, completed_at=now)
            elif attempts >= self.max_attempts:
                job.update(status="failed", last_error=error[:4000], completed_at=now)
            else:
                job.update(
                    status="queued",
                    available_at=now + self.retry_delay * attempts,
                    last_error=error[:4000],
                )

    def _redis_state_key(self, job_id: str) -> str:
        return f"lenapi:job-status:{self.queue_name}:{job_id}"

    async def _ensure_redis_group(self):
        try:
            await self._redis.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _claim_redis(self):
        await self._ensure_redis_group()
        messages = []
        try:
            claimed = await self._redis.xautoclaim(
                self._stream, self._group, self._consumer,
                min_idle_time=max(1, int(self.lease_seconds * 1000)),
                start_id="0-0", count=1,
            )
            if len(claimed) > 1:
                messages = claimed[1]
        except (AttributeError, NotImplementedError):
            # Older Redis clients/servers can still consume new jobs; pending
            # job recovery requires Redis Streams XAUTOCLAIM support.
            pass
        if not messages:
            response = await self._redis.xreadgroup(
                self._group, self._consumer, {self._stream: ">"}, count=1
            )
            if response:
                messages = response[0][1]
        if not messages:
            return None
        stream_id, fields = messages[0]
        if float(fields.get("available_at", "0")) > time.time():
            await self._redis.xack(self._stream, self._group, stream_id)
            await self._redis.xdel(self._stream, stream_id)
            await self._redis.xadd(self._stream, fields)
            return None
        job = {
            "stream_id": stream_id,
            "job_id": fields["job_id"],
            "task_name": fields["task_name"],
            "payload": json.loads(fields["payload"]),
            "attempts": int(fields.get("attempts", "0")) + 1,
        }
        await self._redis.hset(self._redis_state_key(job["job_id"]), mapping={
            "status": "running", "attempts": str(job["attempts"]),
            "last_error": "", "stream_id": stream_id,
        })
        return job

    async def _finish_redis(self, job: dict, *, error: str | None = None):
        stream_id = job["stream_id"]
        job_id = job["job_id"]
        state_key = self._redis_state_key(job_id)
        if error is None:
            await self._redis.xack(self._stream, self._group, stream_id)
            await self._redis.xdel(self._stream, stream_id)
            await self._redis.hset(state_key, mapping={
                "status": "done", "attempts": str(job["attempts"]), "last_error": "",
            })
            return
        if job["attempts"] >= self.max_attempts:
            await self._redis.xack(self._stream, self._group, stream_id)
            await self._redis.xdel(self._stream, stream_id)
            await self._redis.hset(state_key, mapping={
                "status": "failed", "attempts": str(job["attempts"]), "last_error": error[:4000],
            })
            return
        await self._redis.xadd(self._stream, {
            "job_id": job_id,
            "task_name": job["task_name"],
            "payload": json.dumps(job["payload"], separators=(",", ":")),
            "attempts": str(job["attempts"]),
            "available_at": str(time.time() + self.retry_delay * job["attempts"]),
        })
        await self._redis.xack(self._stream, self._group, stream_id)
        await self._redis.xdel(self._stream, stream_id)
        await self._redis.hset(state_key, mapping={
            "status": "queued", "attempts": str(job["attempts"]), "last_error": error[:4000],
        })

    def _finish(self, job_id: str, attempts: int, *, error: str | None = None) -> None:
        now = time.time()
        if error is None:
            values = {"status": "done", "locked_until": None, "last_error": None, "completed_at": now}
        elif attempts >= self.max_attempts:
            values = {"status": "failed", "locked_until": None, "last_error": error[:4000]}
        else:
            values = {
                "status": "queued", "locked_until": None,
                "available_at": now + self.retry_delay * attempts,
                "last_error": error[:4000],
            }
        with self.engine.begin() as connection:
            connection.execute(update(BACKGROUND_JOBS_TABLE)
                               .where(BACKGROUND_JOBS_TABLE.c.job_id == job_id)
                               .values(**values))

    async def run_once(self) -> bool:
        """Claim and execute one available job; return False when the queue is empty."""
        if self.backend == "redis":
            job = await self._claim_redis()
        elif self.backend == "memory":
            job = self._claim_memory()
        else:
            job = await asyncio.to_thread(self._claim)
        if job is None:
            return False
        try:
            function = self._tasks[job["task_name"]]
            payload = job["payload"]
            if inspect.iscoroutinefunction(function):
                await function(*payload["args"], **payload["kwargs"])
            else:
                result = await asyncio.to_thread(function, *payload["args"], **payload["kwargs"])
                if inspect.isawaitable(result):
                    await result
        except Exception as exc:
            logger.exception("Background job %s (%s) failed", job["job_id"], job["task_name"])
            if self.backend == "redis":
                await self._finish_redis(job, error=repr(exc))
            elif self.backend == "memory":
                self._finish_memory(job["job_id"], job["attempts"], error=repr(exc))
            else:
                await asyncio.to_thread(self._finish, job["job_id"], job["attempts"], error=repr(exc))
        else:
            if self.backend == "redis":
                await self._finish_redis(job)
            elif self.backend == "memory":
                self._finish_memory(job["job_id"], job["attempts"])
            else:
                await asyncio.to_thread(self._finish, job["job_id"], job["attempts"])
        return True

    async def run_worker(self, *, poll_interval: float = 1, stop_event: asyncio.Event | None = None) -> None:
        """Keep processing jobs until ``stop_event`` is set."""
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        while stop_event is None or not stop_event.is_set():
            found_job = await self.run_once()
            if not found_job:
                try:
                    await asyncio.wait_for(
                        stop_event.wait() if stop_event is not None else asyncio.sleep(poll_interval),
                        timeout=poll_interval,
                    )
                except asyncio.TimeoutError:
                    pass

    async def status(self, job_id: str) -> dict | None:
        """Return the current state for a job."""
        if self.backend == "redis":
            state = await self._redis.hgetall(self._redis_state_key(job_id))
            if not state:
                return None
            if "attempts" in state:
                state["attempts"] = int(state["attempts"])
            return state

        if self.backend == "memory":
            with _MEMORY_QUEUE_LOCK:
                job = self._memory_jobs.get(job_id)
                return None if job is None else dict(job)

        def read_job():
            with self.engine.connect() as connection:
                row = connection.execute(
                    select(BACKGROUND_JOBS_TABLE).where(BACKGROUND_JOBS_TABLE.c.job_id == job_id)
                ).mappings().first()
                return None if row is None else dict(row)
        return await asyncio.to_thread(read_job)

    async def close(self) -> None:
        """Close the optional Redis client connection pool."""
        if self._redis is not None:
            await self._redis.aclose()
