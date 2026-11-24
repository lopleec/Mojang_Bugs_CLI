"""The same public, read-only search endpoint used by bugs.mojang.com."""

from __future__ import annotations

import json
import math
import os
import socket
import ssl
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import __version__
from .query import issue_key

BASE_URL = "https://bugs.mojang.com"


class MojangError(Exception):
    """A service or transport failure that can be shown without a traceback."""


def _error_text(data: bytes) -> str:
    try:
        obj = json.loads(data)
        if isinstance(obj, dict):
            parts = [obj.get("message"), obj.get("error"), obj.get("errorMessages")]
            return "; ".join(str(v) for v in parts if v)[:500]
    except (ValueError, UnicodeError):
        pass
    return ""


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    # python.org's macOS build can have no bundled CA file until its optional
    # certificate installer runs. Use a real system CA bundle in that case.
    if (
        not paths.cafile
        and not paths.capath
        and not any(os.environ.get(v) for v in ("SSL_CERT_FILE", "SSL_CERT_DIR"))
    ):
        for candidate in (
            "/etc/ssl/cert.pem",
            "/opt/homebrew/etc/ca-certificates/cert.pem",
            "/etc/pki/tls/certs/ca-bundle.crt",
        ):
            if Path(candidate).is_file():
                context.load_verify_locations(cafile=candidate)
                break
    return context


class MojangClient:
    def __init__(self, *, timeout: float = 30, delay: float = 0.3, retries: int = 2):
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or not math.isfinite(delay)
            or delay < 0
            or not 0 <= retries <= 10
        ):
            raise ValueError(
                "timeout must be positive, delay nonnegative, and retries between 0 and 10"
            )
        self.timeout = timeout
        self.delay = delay
        self.retries = retries
        self._last_request: float | None = None
        self._context = _ssl_context()

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            BASE_URL + "/api/jql-search-post",
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"Mojang_Bugs_CLI/{__version__} (com.lopleec.mojang_bugs_cli)",
            },
            method="POST",
        )
        for attempt in range(self.retries + 1):
            if self._last_request is not None:
                remaining = self.delay - (time.monotonic() - self._last_request)
                if remaining > 0:
                    time.sleep(remaining)
            self._last_request = time.monotonic()
            try:
                with urlopen(request, timeout=self.timeout, context=self._context) as response:
                    data = response.read()
            except HTTPError as exc:
                detail = _error_text(exc.read())
                retryable = exc.code in {429, 500, 502, 503, 504}
                wait = _retry_after(exc.headers.get("Retry-After"))
                if retryable and attempt < self.retries and (wait is None or wait <= 60):
                    time.sleep(wait if wait is not None else 2**attempt)
                    continue
                hint = {
                    400: "Invalid query; check filter values or JQL",
                    401: "Authentication required; only public reports are supported",
                    403: "Access denied; try again later",
                    429: "Rate limit reached; retry later or increase --delay",
                }.get(exc.code, "Request failed")
                raise MojangError(
                    f"{hint} (HTTP {exc.code})" + (f": {detail}" if detail else "")
                ) from exc
            except (URLError, TimeoutError, socket.timeout, ConnectionError, HTTPException) as exc:
                if isinstance(getattr(exc, "reason", exc), ssl.SSLCertVerificationError):
                    raise MojangError(
                        "HTTPS certificate verification failed; configure system CAs or set SSL_CERT_FILE to a trusted CA bundle"
                    ) from exc
                if attempt < self.retries:
                    time.sleep(2**attempt)
                    continue
                raise MojangError(f"Unable to connect to the Mojang tracker: {exc}") from exc
            try:
                result = json.loads(data)
            except (ValueError, UnicodeError) as exc:
                raise MojangError(
                    "The tracker did not return JSON; the service may be unavailable or the API may have changed"
                ) from exc
            if not isinstance(result, dict) or not isinstance(result.get("issues"), list):
                raise MojangError(
                    "Unrecognized tracker response: " + (_error_text(data) or "missing issues list")
                )
            for issue in result["issues"]:
                if (
                    not isinstance(issue, dict)
                    or not isinstance(issue.get("key"), str)
                    or not isinstance(issue.get("fields"), dict)
                ):
                    raise MojangError(
                        "The tracker returned an incomplete bug report; stopped fetching"
                    )
            return result
        raise AssertionError("unreachable")

    def search_page(self, project: str, jql: str, page: int, page_size: int) -> dict:
        return self._post(
            {
                "advanced": True,
                "search": jql,
                "project": project,
                "isForge": False,
                "filter": "all",
                "page": page,
                "maxResults": page_size,
            }
        )

    def iter_issues(
        self,
        project: str,
        jql: str,
        *,
        page_size: int = 25,
        limit: int | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> Iterator[dict]:
        if not 1 <= page_size <= 100 or (limit is not None and limit < 1):
            raise ValueError("page_size must be 1–100; limit must be a positive integer or None")
        # Keep page size fixed: the backend caches cursors by query and page size.
        size = min(page_size, limit) if limit else page_size
        page, count = 0, 0
        seen: set[str] = set()
        while True:
            result = self.search_page(project, jql, page, size)
            issues = result["issues"]
            pagination = result.get("pagination")
            if isinstance(pagination, dict) and isinstance(pagination.get("hasNextPage"), bool):
                if pagination.get("currentPage", page) != page:
                    raise MojangError(
                        "The tracker returned the wrong page; stopped to avoid an incomplete export"
                    )
                more = pagination["hasNextPage"]
            elif isinstance(result.get("isLast"), bool):
                more = not result["isLast"]
            elif isinstance(result.get("total"), int):
                more = (page + 1) * size < result["total"]
            else:
                raise MojangError("Missing pagination information; unable to confirm completeness")
            new_count = 0
            for issue in issues:
                key = issue["key"]
                if key in seen:
                    continue
                seen.add(key)
                new_count += 1
                count += 1
                yield issue
                if limit is not None and count >= limit:
                    if progress:
                        progress(page + 1, count)
                    return
            if progress:
                progress(page + 1, count)
            if not more:
                return
            if not new_count:
                raise MojangError(
                    "Pagination made no progress; stopped to avoid an incomplete export"
                )
            page += 1

    def get_issue(self, key: str) -> dict:
        key = issue_key(key)
        project = key.rsplit("-", 1)[0]
        result = self.search_page(project, f"key = {key}", 0, 1)
        matches = [i for i in result["issues"] if i["key"] == key]
        if not matches:
            raise MojangError(f"{key} was not found; the report may be unavailable or private")
        return matches[0]
