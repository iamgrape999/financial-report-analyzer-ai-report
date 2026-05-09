"""Local async pytest support used when external pytest-asyncio is unavailable."""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator
from typing import Any

import pytest

_LOOP: asyncio.AbstractEventLoop | None = None


def _loop() -> asyncio.AbstractEventLoop:
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "asyncio: run an async test function on the local test event loop")


def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    """Run ``async def`` tests marked with pytest-style fixtures."""
    test_func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test_func):
        return None

    kwargs = {
        name: pyfuncitem.funcargs[name]
        for name in pyfuncitem._fixtureinfo.argnames  # noqa: SLF001 - pytest public-ish internals
    }
    _loop().run_until_complete(test_func(**kwargs))
    return True


@pytest.hookimpl(tryfirst=True)
def pytest_fixture_setup(fixturedef: pytest.FixtureDef[Any], request: pytest.FixtureRequest) -> Any | None:
    """Resolve coroutine and async-generator fixtures on the shared test loop."""
    fixture_func = fixturedef.func
    if not (inspect.iscoroutinefunction(fixture_func) or inspect.isasyncgenfunction(fixture_func)):
        return None

    kwargs = {name: request.getfixturevalue(name) for name in fixturedef.argnames}
    loop = _loop()

    if inspect.isasyncgenfunction(fixture_func):
        agen: AsyncGenerator[Any, None] = fixture_func(**kwargs)
        result = loop.run_until_complete(agen.__anext__())

        def finalizer() -> None:
            try:
                loop.run_until_complete(agen.__anext__())
            except StopAsyncIteration:
                return
            raise RuntimeError("Async generator fixture yielded more than once")

        request.addfinalizer(finalizer)
    else:
        result = loop.run_until_complete(fixture_func(**kwargs))

    fixturedef.cached_result = (result, None, None)
    return result


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()
        _LOOP = None
