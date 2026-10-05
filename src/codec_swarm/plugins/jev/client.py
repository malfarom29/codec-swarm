"""Thin wrapper over typesafe-sdk so plugins depend on one call shape, and tests can script it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy, SystemOneResponse

MODEL = "jev-latest"  # jev-1.13 is not served by name; each response reports the concrete version (M0)

SystemOne = Callable[[Any, Mapping[str, Any]], Awaitable[SystemOneResponse]]


class JevClient:
    """One shared client per process. Calls are logged so mission costs can include Jev usage."""

    def __init__(self, timeout: float = 10.0) -> None:
        self._client: AsyncTypeSafeClient | None = None
        self._timeout = timeout
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, state: Any, questions: Mapping[str, Any]) -> SystemOneResponse:
        if self._client is None:
            self._client = AsyncTypeSafeClient(model=MODEL, retry=RetryPolicy(max_retries=1), timeout=self._timeout)
        response = await self._client.system_one(state, questions)
        self.calls.append({"model": response.model, "questions": list(questions), **response.usage.model_dump()})
        return response

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
