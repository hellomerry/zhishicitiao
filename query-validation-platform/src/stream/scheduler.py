"""任务队列调度器：排队 + 自适应并发 worker 池。

导入的 Query 不再立即扇出并行跑，而是进入队列，
由固定数量的 worker 协程在自适应并发限制下逐个消费。
"""
import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from src.config import settings
from src.db.session import SessionLocal
from src.models.tasks import Task
from src.stream.bus import bus
from src.stream.limiter import AdaptiveLimiter


def is_throttled(exc: Exception) -> bool:
    """识别限流/并发超限类错误（429、rate limit、too many 等）。"""
    name = type(exc).__name__.lower()
    if "ratelimit" in name or "throttl" in name or "toomany" in name:
        return True
    s = str(exc).lower()
    return any(k in s for k in ("429", "rate limit", "rate_limit", "too many",
                                "限流", "并发", "quota", "exceeded"))


class TaskScheduler:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.limiter = AdaptiveLimiter(
            min_c=settings.min_concurrency,
            max_c=settings.max_concurrency,
            initial=settings.initial_concurrency,
        )
        self._workers: list[asyncio.Task] = []
        self._started = False
        self._meta: dict[str, dict] = {}   # task_id(str) -> {"query":..., "status":...}
        self._paused = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self._recover_pending()
        for _ in range(self.limiter.max_c):
            self._workers.append(asyncio.create_task(self._worker()))

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        self._workers.clear()
        self._started = False

    async def _recover_pending(self) -> None:
        """重启后恢复未完成任务（draft/processing 重新入队）。"""
        async with SessionLocal() as session:
            tasks = list((await session.execute(
                select(Task).where(Task.status.in_(["draft", "processing"])))).scalars())
            for task in tasks:
                task.status = "draft"
            await session.commit()
            for task in tasks:
                tid = str(task.id)
                self._meta[tid] = {"query": task.query, "status": "queued"}
                await self.queue.put(task.id)
                await bus.publish("task_enqueued", {"query": task.query}, task_id=tid)

    async def enqueue(self, task_id, query: str) -> None:
        tid = str(task_id)
        self._meta[tid] = {"query": query, "status": "queued"}
        await self.queue.put(task_id)
        await bus.publish("task_enqueued", {"query": query}, task_id=tid)

    def pause(self) -> None:
        """暂停取新任务（检修停机），已在跑的任务继续完成。"""
        self._paused = True
        self._pause_event.clear()

    def resume(self) -> None:
        """恢复取新任务。"""
        self._paused = False
        self._pause_event.set()

    def clear(self) -> None:
        """清空内存中的队列与任务元数据（配合数据库清空）。"""
        self._meta.clear()
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def _worker(self) -> None:
        while True:
            await self._pause_event.wait()
            task_id = await self.queue.get()
            await self.limiter.acquire()
            tid = str(task_id)
            try:
                self._meta[tid]["status"] = "processing"
                await bus.publish("task_started", {"query": self._meta[tid]["query"]}, task_id=tid)
                await self._process(task_id)
                self._meta[tid]["status"] = "done"
                await self.limiter.report(success=True, throttled=False)
                await bus.publish("task_finished", {"query": self._meta[tid]["query"]}, task_id=tid)
            except Exception as e:  # noqa: BLE001
                throttled = is_throttled(e)
                self._meta[tid]["status"] = "failed"
                await self.limiter.report(success=False, throttled=throttled)
                await bus.publish("task_failed",
                                  {"query": self._meta[tid]["query"], "error": str(e),
                                   "throttled": throttled}, task_id=tid)
                await self._mark_status(task_id, "failed")
            finally:
                await self.limiter.release()
                self.queue.task_done()

    async def _mark_status(self, task_id, status: str) -> None:
        try:
            async with SessionLocal() as session:
                task = (await session.execute(
                    select(Task).where(Task.id == task_id))).scalars().first()
                if task:
                    task.status = status
                    await session.commit()
        except Exception:  # noqa: BLE001
            pass

    async def _process(self, task_id) -> None:
        from src.pipeline.orchestrator import run_pipeline
        async with SessionLocal() as session:
            task = (await session.execute(
                select(Task).where(Task.id == task_id))).scalar_one()
            task.status = "processing"
            await session.commit()
        await run_pipeline(task_id)
        async with SessionLocal() as session:
            task = (await session.execute(
                select(Task).where(Task.id == task_id))).scalar_one()
            task.status = "review"
            await session.commit()

    def snapshot(self) -> dict:
        counts = {"queued": 0, "processing": 0, "done": 0, "failed": 0}
        for m in self._meta.values():
            st = m.get("status")
            if st in counts:
                counts[st] += 1
        return {
            "counts": counts,
            "queue_size": self.queue.qsize(),
            "limiter": self.limiter.snapshot(),
            "tasks": list(self._meta.values()),
        }


scheduler = TaskScheduler()
