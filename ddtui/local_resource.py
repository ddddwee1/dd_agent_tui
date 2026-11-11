"""Cooperative local inference locks, using the project's existing lock chain."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import fcntl
import os
from pathlib import Path
import time


@asynccontextmanager
async def inference_locks(paths, timeout):
    descriptors = []
    deadline = time.monotonic() + timeout
    try:
        for path in paths:
            # Never create a task-specific substitute for a shared device lock.
            fd = os.open(Path(path).expanduser(), os.O_RDWR)
            descriptors.append(fd)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Local inference is waiting for the configured shared resource locks")
                    await asyncio.sleep(0.1)
        yield
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
