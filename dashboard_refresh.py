"""Share dashboard refreshes across sessions without blocking Streamlit's renderer."""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import logging
from threading import Lock
from time import monotonic


@dataclass
class Refresh:
    started: float
    future: Future
    previous: Future | None
    revision: tuple


class DashboardRefresh:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="balticpulse")
        self.lock = Lock()
        self.jobs: dict[str, Refresh] = {}

    @staticmethod
    def _load(name, loader, args):
        start = monotonic()
        try:
            return loader(*args)
        except Exception as exc:
            logging.getLogger(__name__).warning("Dashboard refresh %s failed (%s)", name, type(exc).__name__)
            raise
        finally:
            logging.getLogger(__name__).info("Dashboard refresh %s took %.2fs", name, monotonic() - start)

    def read(self, name, loader, args, fallback, pending, *, interval=900, revision=()):
        with self.lock:
            job = self.jobs.get(name)
            now = monotonic()
            if job is None or (job.future.done() and (job.revision != revision or now - job.started >= interval)):
                previous = job.future if job is not None and job.revision == revision else None
                job = Refresh(now, self.pool.submit(self._load, name, loader, args), previous, revision)
                self.jobs[name] = job
            if job.future.done():
                ready = job.future
            else:
                pending.append(job.future)
                ready = job.previous

        if ready is not None:
            try:
                return ready.result()
            except Exception:
                return fallback(False)
        return fallback(True)
