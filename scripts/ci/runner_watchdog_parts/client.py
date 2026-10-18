"""The GitHub REST client the watchdog talks through.

``GitHubApi`` is the production ``Api``: authenticated urllib requests in which a read
is retried once on a transient failure and a mutation never is, with the rate-limit hints
of a refused request (``_rate_limit_hints``) attached to its ``ApiError``.
``_ReadsThrough`` sends an ``Api``'s reads through a caller's wait-and-retry and passes
its mutations straight through. ``ApiError`` and the ``Api`` protocol stay in
``runner_watchdog``, the only module that imports this one.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from runner_watchdog import Api, ApiError


def _rate_limit_hints(headers: Any) -> tuple[float | None, str | None]:
    """Seconds until the rate-limit window resets, and the remaining quota, from headers.

    ``Retry-After`` (seconds) is honoured first -- it is what a secondary rate
    limit sends. Otherwise ``X-RateLimit-Reset`` (an epoch second) gives the
    wait when the quota is spent (``X-RateLimit-Remaining`` is ``0``). A missing
    or non-numeric header yields ``None``, so a caller with no usable hint aborts
    rather than guessing a wait.
    """
    if headers is None:
        return None, None
    remaining = headers.get("X-RateLimit-Remaining")
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after)), remaining
        except (TypeError, ValueError):
            pass
    reset = headers.get("X-RateLimit-Reset")
    if reset and remaining == "0":
        try:
            return max(0.0, float(reset) - time.time()), remaining
        except (TypeError, ValueError):
            pass
    return None, remaining


class GitHubApi:
    """Minimal authenticated GitHub REST client over urllib.

    Retries once on a 5xx or a transport error, because a scheduled watchdog that
    dies on one transient failure heals nothing that tick.
    """

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.github.com",
        *,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._sleep = sleep
        self._open = opener
        # This watchdog exists because a shared installation quota ran out, and it
        # is itself a heavy consumer of that quota: the cancelled index alone is
        # sixteen pages every tick. Counting its own calls and remembering the last
        # `X-RateLimit-Remaining` the API reported turns "is the schedule
        # sustainable?" from a guess into a number in every tick's summary.
        self.calls = 0
        self.rate_limit_remaining: str | None = None

    def _request(self, method: str, path: str) -> Any:
        url = path if path.startswith("https://") else f"{self._base_url}/{path.lstrip('/')}"
        request = urllib.request.Request(url, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "kirocrew-ci-runner-watchdog")
        # A read is retried once on any transient failure. A mutation is NOT,
        # whatever the failure: once the request may be on the wire its effect
        # is unknown, and a repeat can only turn "it landed" into a conflict;
        # the caller reconciles from the run's own state.
        mutation = method != "GET"
        attempts = 0
        while True:
            attempts += 1
            self.calls += 1
            try:
                with self._open(request, timeout=30) as response:
                    # Tolerated rather than required: the quota reading is
                    # observability, and a response object without headers must
                    # not turn a successful read into a crash.
                    headers = getattr(response, "headers", None)
                    remaining = headers.get("X-RateLimit-Remaining") if headers else None
                    if remaining is not None:
                        self.rate_limit_remaining = remaining
                    body = response.read()
                    return json.loads(body) if body else None
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except (OSError, http.client.HTTPException) as body_exc:
                    # The error body is a courtesy; failing to read it must not
                    # turn a classified HTTP error into an unhandled crash.
                    detail = f"(error body unreadable: {type(body_exc).__name__})"
                if exc.code >= 500:
                    if not mutation and attempts == 1:
                        self._sleep(2)
                        continue
                    raise ApiError(exc.code, detail, ambiguous=mutation) from exc
                retry_after, remaining = _rate_limit_hints(exc.headers)
                if remaining is not None:
                    self.rate_limit_remaining = remaining
                raise ApiError(
                    exc.code, detail, retry_after=retry_after, remaining=remaining
                ) from exc
            except urllib.error.URLError as exc:
                # Usually the connection itself failed, but a timeout while
                # sending or awaiting the response surfaces here too, and then
                # the request may already have been delivered. A read is
                # retried; a mutation is ambiguous and left to the caller.
                if not mutation and attempts == 1:
                    self._sleep(2)
                    continue
                raise ApiError(0, str(exc.reason), ambiguous=mutation) from exc
            except (OSError, http.client.HTTPException, ValueError) as exc:
                # A timeout or reset while READING the response (the connect
                # succeeded, so it is not a URLError), a partial read
                # (``IncompleteRead`` is an HTTPException, not an OSError), or a
                # truncated body that is not JSON: the same transient class,
                # retried once for a read and surfaced as an ApiError otherwise.
                # For a mutation the request WAS sent, so the error is ambiguous.
                if not mutation and attempts == 1:
                    self._sleep(2)
                    continue
                raise ApiError(0, f"{type(exc).__name__}: {exc}", ambiguous=mutation) from exc

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str) -> None:
        self._request("POST", path)


class _ReadsThrough:
    """An ``Api`` whose READS go through a wrapper and whose mutations do not.

    The recovery pass reads the cancelled index, each run's jobs and each branch
    lookup through the plain client, so a rate limit whose window resets in a few
    seconds -- one the gather phase would simply wait out -- aborted the pass
    instead. For a cancelled run with less window left than one schedule interval
    there is no next tick, so a cheap reset was costing a verdict. Wrapping the
    reads hands recovery the same wait-and-retry the gather already has.

    Mutations pass straight through, deliberately: a cancel or re-run that may be
    on the wire is never repeated, whatever the failure.
    """

    def __init__(self, api: Api, read: Callable[[str], Any]) -> None:
        self._api = api
        self._read = read

    def get(self, path: str) -> Any:
        return self._read(path)

    def post(self, path: str) -> None:
        self._api.post(path)
