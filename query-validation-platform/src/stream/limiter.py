"""自适应并发限制器（AIMD：加性增、乘性减）。

根据任务成败与限流信号动态调整并发上限：
- 触发限流（429/rate limit）→ 并发减半（乘性减），下限 min_c
- 连续成功满一个周期 → 并发 +1（加性增），上限 max_c
- 连续普通失败 ≥3 → 并发 -1
"""
import asyncio
import time

from src.stream.bus import bus


class AdaptiveLimiter:
    def __init__(self, min_c: int = 1, max_c: int = 8, initial: int = 2):
        self.min_c = min_c
        self.max_c = max_c
        self.capacity = max(min_c, min(initial, max_c))
        self._in_flight = 0
        self._cond = asyncio.Condition()
        self._consecutive_success = 0
        self._consecutive_fail = 0
        self.last_throttled_at: float | None = None

    async def acquire(self) -> None:
        async with self._cond:
            while self._in_flight >= self.capacity:
                await self._cond.wait()
            self._in_flight += 1

    async def release(self) -> None:
        async with self._cond:
            self._in_flight -= 1
            self._cond.notify_all()

    async def report(self, success: bool, throttled: bool) -> None:
        async with self._cond:
            old = self.capacity
            if throttled:
                self.capacity = max(self.min_c, self.capacity // 2)
                self._consecutive_success = 0
                self._consecutive_fail += 1
                self.last_throttled_at = time.time()
            elif success:
                self._consecutive_success += 1
                self._consecutive_fail = 0
                if self._consecutive_success >= max(2, self.capacity * 2) \
                        and self.capacity < self.max_c:
                    self.capacity += 1
                    self._consecutive_success = 0
            else:
                self._consecutive_fail += 1
                self._consecutive_success = 0
                if self._consecutive_fail >= 3 and self.capacity > self.min_c:
                    self.capacity -= 1
                    self._consecutive_fail = 0

            changed = self.capacity != old
            if changed:
                self._cond.notify_all()

        if throttled:
            await bus.publish("rate_limit", {
                "capacity": self.capacity,
                "in_flight": self._in_flight,
            })
        if changed:
            await bus.publish("concurrency", {
                "capacity": self.capacity,
                "in_flight": self._in_flight,
                "previous": old,
            })

    def snapshot(self) -> dict:
        return {
            "capacity": self.capacity,
            "in_flight": self._in_flight,
            "min_c": self.min_c,
            "max_c": self.max_c,
            "consecutive_success": self._consecutive_success,
            "consecutive_fail": self._consecutive_fail,
        }
