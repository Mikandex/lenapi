"""Helpers for calling sync and async user-provided functions safely."""

import asyncio
import inspect
from collections.abc import Callable
from typing import Any


async def call_sync_or_async(function: Callable, *args: Any, **kwargs: Any) -> Any:
    """Run synchronous callbacks off-loop and await async callbacks normally.

    ``asyncio.to_thread`` propagates context variables, preserving request and
    database-session context for synchronous framework callbacks.
    """
    is_async = inspect.iscoroutinefunction(function) or inspect.iscoroutinefunction(
        getattr(function, "__call__", None)
    )
    if is_async:
        result = function(*args, **kwargs)
    else:
        result = await asyncio.to_thread(function, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result
