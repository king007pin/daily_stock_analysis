# -*- coding: utf-8 -*-
"""Pytest compatibility hooks."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import time
import threading
from collections.abc import Awaitable, Callable
from contextvars import copy_context
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar
from warnings import warn

import anyio.to_thread
import pytest
import fastapi.testclient
import httpx
import starlette.testclient
from anyio._backends import _asyncio

T = TypeVar("T")

ISOLATED_ENV_FILE = str(Path(__file__).resolve().parent / "fixtures" / "__absent__.env")

# Every key the developer's real .env defines. Only the names are kept; the values are
# read and dropped. Absent in CI, where the set is empty and this guard does nothing.
try:
    from dotenv import dotenv_values as _dotenv_values

    _REPO_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    _REPO_ENV_KEYS = frozenset(_dotenv_values(_REPO_ENV_PATH).keys()) if _REPO_ENV_PATH.exists() else frozenset()
except Exception:  # noqa: BLE001 - never let this break collection
    _REPO_ENV_KEYS = frozenset()

# What those keys looked like before any test ran. Anything else is leakage.
_ENV_BASELINE = {key: os.environ.get(key) for key in _REPO_ENV_KEYS}


def _reset_config_singleton() -> None:
    try:
        from src.config import Config

        Config.reset_instance()
    except Exception:  # noqa: BLE001 - a broken import must not break every test
        pass


def _repair_missing_env_file() -> None:
    """Re-isolate ``ENV_FILE`` only when a test left it *unset*.

    Around a dozen files pop it in ``tearDown``/``tearDownClass`` instead of restoring
    it, and from that point on the config falls back to ``<repo>/.env``. Repairing an
    absent variable fixes that. Repairing a variable that merely points somewhere else
    must NOT happen: several classes legitimately set it in ``setUpClass`` and every test
    in the class then depends on it.
    """
    if "ENV_FILE" in os.environ:
        return
    os.environ["ENV_FILE"] = ISOLATED_ENV_FILE
    _reset_config_singleton()


def _undo_repo_env_leakage() -> None:
    """Remove values of the developer's real ``.env`` that a test left in ``os.environ``.

    ``ENV_FILE`` cannot defend against this: the environment outranks it. At least one
    test loads the repo file straight into the process, and every later test then reads
    the developer's settings. Run as a post-condition only, so a test that deliberately
    sets one of these keys for itself is never disturbed mid-test.
    """
    repaired = False
    for key, original in _ENV_BASELINE.items():
        if os.environ.get(key) == original:
            continue
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original
        repaired = True
    if repaired:
        _reset_config_singleton()


@pytest.fixture(autouse=True)
def _keep_env_isolated():
    """Keep the offline suite offline, whatever a test does to the environment.

    Two distinct leaks, repaired at the only points where each can be caught safely:

    * ``ENV_FILE`` popped rather than restored — repaired *before* each test, because the
      pop can happen in ``tearDownClass``, after the previous test's fixtures have run.
      Only an absent variable is repaired; a class that sets its own path keeps it.
    * The repo ``.env`` loaded straight into ``os.environ`` — undone *after* each test,
      so a test that sets one of those keys on purpose still sees its own value.

    Invisible until 2026-09-01, when three opt-in feature flags were switched on in that
    ``.env``: the offline suite began fetching benchmark data for every scored signal and
    ran 12 minutes instead of 210 seconds with nothing failing. A suite that stays green
    while it quietly goes online reports the wrong thing twice.
    """
    _repair_missing_env_file()
    try:
        yield
    finally:
        _repair_missing_env_file()
        _undo_repo_env_leakage()


@pytest.fixture(autouse=True, scope="session")
def _isolate_repo_env_file():
    """Stop the offline suite from reading the developer's real ``.env``.

    ``Config`` falls back to ``<repo>/.env`` whenever ``ENV_FILE`` is unset, so a
    developer's own settings leak into every test that does not pin its inputs.
    That produced a failure mode worse than a plain red suite: three tests passed
    in CI (which has no ``.env``) and failed locally, because the repo ``.env``
    sets ``REPORT_LANGUAGE=en`` while the tests assert Chinese output.

    Pointing ``ENV_FILE`` at a path that does not exist makes local runs match CI
    exactly. Tests that need specific configuration must inject it explicitly —
    ``monkeypatch.setenv("ENV_FILE", ...)`` still wins for the duration of a test.
    """
    previous = os.environ.get("ENV_FILE")
    os.environ["ENV_FILE"] = ISOLATED_ENV_FILE
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("ENV_FILE", None)
        else:
            os.environ["ENV_FILE"] = previous


_original_call_soon_threadsafe = asyncio.BaseEventLoop.call_soon_threadsafe


async def _shutdown_default_executor_inline(
    self: asyncio.BaseEventLoop,
    timeout: float | None = None,
) -> None:
    """Avoid lost wakeups while asyncio.run() tears down test-only executors."""
    del timeout
    executor = getattr(self, "_default_executor", None)
    if executor is None:
        return
    self._executor_shutdown_called = True
    self._default_executor = None
    executor.shutdown(wait=True)


def _call_soon_threadsafe_with_extra_wakeup(
    self: asyncio.BaseEventLoop,
    callback,
    *args,
    context=None,
):
    """Wake the selector again for sandboxed test runs where the first wake is lost."""
    handle = _original_call_soon_threadsafe(self, callback, *args, context=context)
    write_to_self = getattr(self, "_write_to_self", None)
    if callable(write_to_self):
        write_to_self()
        threading.Timer(0.001, write_to_self).start()
    return handle


asyncio.BaseEventLoop.call_soon_threadsafe = _call_soon_threadsafe_with_extra_wakeup
asyncio.BaseEventLoop.shutdown_default_executor = _shutdown_default_executor_inline


async def _run_sync_via_asyncio_to_thread(
    func: Callable[..., T],
    *args: Any,
    abandon_on_cancel: bool = False,
    cancellable: bool | None = None,
    limiter: Any = None,
) -> T:
    """Use asyncio's executor path when AnyIO worker queues miss wakeups."""
    del abandon_on_cancel, limiter
    if cancellable is not None:
        warn(
            "The `cancellable=` keyword argument to `anyio.to_thread.run_sync` is "
            "deprecated since AnyIO 4.1.0; use `abandon_on_cancel=` instead",
            DeprecationWarning,
            stacklevel=2,
        )
    future: concurrent.futures.Future[T] = concurrent.futures.Future()
    context = copy_context()

    def runner() -> None:
        try:
            future.set_result(context.run(func, *args))
        except BaseException as exc:
            future.set_exception(exc)

    threading.Thread(target=runner, name="pytest-anyio-worker", daemon=True).start()
    while not future.done():
        await asyncio.sleep(0.001)
    return future.result()


def _wait_for_cross_thread_result(loop: asyncio.AbstractEventLoop, future: concurrent.futures.Future[T]) -> T:
    write_to_self = getattr(loop, "_write_to_self", None)
    while not future.done():
        if callable(write_to_self):
            write_to_self()
        time.sleep(0.001)
    return future.result()


def _run_sync_from_thread_with_wakeup(
    cls,
    func: Callable[..., T],
    args: tuple[Any, ...],
    token: object,
) -> T:
    @wraps(func)
    def wrapper() -> None:
        try:
            _asyncio.set_current_async_library("asyncio")
            future.set_result(func(*args))
        except BaseException as exc:
            future.set_exception(exc)
            if not isinstance(exc, Exception):
                raise

    loop = token or _asyncio.threadlocals.current_token.native_token
    if loop.is_closed():
        raise _asyncio.RunFinishedError
    future: concurrent.futures.Future[T] = concurrent.futures.Future()
    loop.call_soon_threadsafe(wrapper)
    return _wait_for_cross_thread_result(loop, future)


def _run_async_from_thread_with_wakeup(
    cls,
    func: Callable[..., Awaitable[T]],
    args: tuple[Any, ...],
    token: object,
) -> T:
    loop = token or _asyncio.threadlocals.current_token.native_token
    if loop.is_closed():
        raise _asyncio.RunFinishedError
    context = copy_context()
    context.run(_asyncio.set_current_async_library, "asyncio")
    future = context.run(asyncio.run_coroutine_threadsafe, func(*args), loop=loop)
    return _wait_for_cross_thread_result(loop, future)


anyio.to_thread.run_sync = _run_sync_via_asyncio_to_thread
_asyncio.AsyncIOBackend.run_sync_from_thread = classmethod(_run_sync_from_thread_with_wakeup)
_asyncio.AsyncIOBackend.run_async_from_thread = classmethod(_run_async_from_thread_with_wakeup)


class _ThreadlessTestClient:
    """Small TestClient replacement that avoids AnyIO's cross-thread portal."""

    def __init__(
        self,
        app,
        base_url: str = "http://testserver",
        raise_server_exceptions: bool = True,
        follow_redirects: bool = True,
        **_: Any,
    ) -> None:
        self.app = app
        self.base_url = base_url
        self.raise_server_exceptions = raise_server_exceptions
        self.follow_redirects = follow_redirects
        self.cookies = httpx.Cookies()
        self._lifespan_ctx = None
        self._lifespan_enter_count = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_client: httpx.AsyncClient | None = None

    def _get_lifespan_context(self):
        return getattr(getattr(self.app, "router", None), "lifespan_context", None)

    def _build_async_client(self, follow_redirects: bool) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(
            app=self.app,
            raise_app_exceptions=self.raise_server_exceptions,
        )
        return httpx.AsyncClient(
            transport=transport,
            base_url=self.base_url,
            follow_redirects=follow_redirects,
            cookies=self.cookies,
        )

    def __enter__(self):
        if self._lifespan_enter_count == 0:
            self._loop = asyncio.new_event_loop()
            lifespan_context = self._get_lifespan_context()
            if callable(lifespan_context):
                self._lifespan_ctx = lifespan_context(self.app)
                self._loop.run_until_complete(self._lifespan_ctx.__aenter__())
            self._async_client = self._build_async_client(self.follow_redirects)
        self._lifespan_enter_count += 1
        return self

    def __exit__(self, *args: Any) -> None:
        if self._lifespan_enter_count == 0:
            return None

        self._lifespan_enter_count -= 1
        if self._lifespan_enter_count == 0 and self._loop is not None:
            try:
                async def _close() -> None:
                    if self._async_client is not None:
                        await self._async_client.aclose()
                    if self._lifespan_ctx is not None:
                        await self._lifespan_ctx.__aexit__(*args)

                self._loop.run_until_complete(_close())
            finally:
                self._lifespan_ctx = None
                self._async_client = None
                self._loop.close()
                self._loop = None
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        follow_redirects = kwargs.pop("follow_redirects", self.follow_redirects)
        kwargs.pop("allow_redirects", None)

        if self._lifespan_enter_count > 0 and self._loop is not None and self._async_client is not None:
            response = self._loop.run_until_complete(
                self._async_client.request(method, url, follow_redirects=follow_redirects, **kwargs)
            )
            self.cookies = httpx.Cookies(self._async_client.cookies)
            return response

        async def _send() -> httpx.Response:
            async with self._build_async_client(follow_redirects) as client:
                response = await client.request(method, url, **kwargs)
                self.cookies = httpx.Cookies(client.cookies)
                return response

        return asyncio.run(_send())

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("HEAD", url, **kwargs)


fastapi.testclient.TestClient = _ThreadlessTestClient
starlette.testclient.TestClient = _ThreadlessTestClient
