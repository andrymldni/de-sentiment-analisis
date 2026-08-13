"""Retry helpers with exponential backoff + jitter."""

from __future__ import annotations

import functools
import random
import time
from collections.abc import Callable
from typing import TypeVar

from ..logging_config import get_logger

logger = get_logger(__name__)
T = TypeVar("T")


def retry(
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_error: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203
                    last_error = exc
                    if attempt == attempts:
                        break
                    delay = min(base_delay * 2 ** (attempt - 1), max_delay)
                    delay *= 0.5 + random.random()  # full jitter band
                    logger.warning(
                        "%s failed (attempt %d/%d): %s - retrying in %.1fs",
                        func.__name__,
                        attempt,
                        attempts,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
            assert last_error is not None
            raise last_error

        return wrapper

    return decorator
