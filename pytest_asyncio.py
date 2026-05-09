"""Tiny local pytest-asyncio compatibility shim for this repository's tests.

The execution environment used by the agent can block package installation, so
this module provides the subset of ``pytest_asyncio`` used by the existing test
suite: the ``fixture`` decorator. Async test execution and async fixture setup are
implemented in ``conftest.py``.
"""
from __future__ import annotations

from typing import Any, Callable, TypeVar

import pytest

F = TypeVar("F", bound=Callable[..., Any])


def fixture(*args: Any, **kwargs: Any):
    """Delegate to ``pytest.fixture`` with pytest-asyncio-compatible spelling."""
    return pytest.fixture(*args, **kwargs)
