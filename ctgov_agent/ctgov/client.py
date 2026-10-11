"""Thin, defensive client for the ClinicalTrials.gov Data API v2.

Responsibilities: pagination, retry with backoff on 429/5xx, a small TTL cache,
and a hard record cap so a broad query cannot run away. It knows nothing about
planning or charts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://clinicaltrials.gov/api/v2"
STUDY_URL = "https://clinicaltrials.gov/study/{nct_id}"

# Only request the modules the engine reads. Cuts payload size ~10x vs full records.
STUDY_FIELDS = ",".join([
    "protocolSection.identificationModule",
    "protocolSection.statusModule",
    "protocolSection.sponsorCollaboratorsModule",
    "protocolSection.conditionsModule",
    "protocolSection.designModule",
    "protocolSection.armsInterventionsModule",
    "protocolSection.contactsLocationsModule",
    "derivedSection.conditionBrowseModule",
    "derivedSection.interventionBrowseModule",
])

MAX_PAGE_SIZE = 1000


class CTGovError(RuntimeError):
    pass


@dataclass
class FetchResult:
    studies: list[dict[str, Any]]
    total_count: Optional[int]
    truncated: bool
    params: dict[str, Any]


class CTGovClient:
    def __init__(
        self,
        base_url: str = API_BASE,
        timeout: float = 30.0,
        max_retries: int = 4,
        cache_ttl_s: float = 900.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.base_url = base_url
        self.max_retries = max_retries
        self.cache_ttl_s = cache_ttl_s
        self._cache: dict[str, tuple[float, FetchResult]] = {}
        # The API answers 429 after roughly ten back-to-back calls (observed with scripts/probe_api.py),
        # so all requests from this client share a minimum spacing. Retries still back off on 429.
        self.min_interval_s = float(os.getenv("CTGOV_MIN_INTERVAL_S", "0.35"))
        self._rate_lock = threading.Lock()
        self._next_slot = 0.0
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "ctgov-viz-agent/0.1"},
        )

    # ------------------------------------------------------------------ #
    def search_studies(self, params: dict[str, Any], max_records: int,
                       fields: Optional[list[str]] = None) -> FetchResult:
        """Fetch up to `max_records` studies matching `params`, following pageTokens.

        `fields` narrows the payload to the leaf paths the analysis needs. If the API rejects
        the narrowed field list, the request is retried once with whole modules.
        """
        field_str = ",".join(fields) if fields else STUDY_FIELDS
        key = self._cache_key({**params, "_fields": field_str}, max_records)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.cache_ttl_s:
            return hit[1]
        try:
            result = self._paginate(params, max_records, field_str)
        except CTGovError as e:
            if not fields or "400" not in str(e):
                raise
            log.warning("narrow field list rejected (%s); retrying with full modules", e)
            result = self._paginate(params, max_records, STUDY_FIELDS)
        self._cache[key] = (time.monotonic(), result)
        return result

    def count(self, params: dict[str, Any]) -> int:
        """Total number of studies matching `params` (one tiny request, cached)."""
        key = self._cache_key({**params, "_count": True}, 0)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.cache_ttl_s:
            return hit[1]
        body = self._get_json("/studies", {**params, "format": "json", "countTotal": "true", "pageSize": 1,
                                           "fields": "protocolSection.identificationModule.nctId"})
        total = body.get("totalCount")
        if not isinstance(total, int):
            raise CTGovError("count query returned no totalCount")
        self._cache[key] = (time.monotonic(), total)
        return total

    def _paginate(self, params: dict[str, Any], max_records: int, field_str: str) -> FetchResult:
        base = {**params, "format": "json", "countTotal": "true", "fields": field_str}
        studies: list[dict[str, Any]] = []
        total: Optional[int] = None
        token: Optional[str] = None

        while len(studies) < max_records:
            page = dict(base, pageSize=min(MAX_PAGE_SIZE, max_records - len(studies)))
            if token:
                page["pageToken"] = token
            body = self._get_json("/studies", page)
            if total is None:
                total = body.get("totalCount")
            studies.extend(body.get("studies") or [])
            token = body.get("nextPageToken")
            if not token:
                break

        result = FetchResult(
            studies=studies[:max_records],
            total_count=total,
            truncated=bool(token) or (total is not None and total > len(studies)),
            params=dict(params),
        )
        return result

    # ------------------------------------------------------------------ #
    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        delay = 1.0
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self._http.get(path, params=params)
            except httpx.TransportError as e:  # network blip
                last_err = e
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except json.JSONDecodeError as e:
                        raise CTGovError(f"non-JSON response from ClinicalTrials.gov: {e}") from e
                if resp.status_code == 400:
                    # bad query syntax is not retryable; surface the API's own message
                    raise CTGovError(f"ClinicalTrials.gov rejected the query (400): {resp.text[:300]}")
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise CTGovError(f"ClinicalTrials.gov returned HTTP {resp.status_code}")
                last_err = CTGovError(f"HTTP {resp.status_code}")
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            if attempt < self.max_retries:
                log.warning("ctgov request failed (%s); retrying in %.1fs", last_err, delay)
                time.sleep(delay)
                delay = min(delay * 2, 16)
        raise CTGovError(f"ClinicalTrials.gov unavailable after retries: {last_err}")

    def _throttle(self) -> None:
        if self.min_interval_s <= 0:
            return
        with self._rate_lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self.min_interval_s
        if wait > 0:
            time.sleep(wait)

    @staticmethod
    def _cache_key(params: dict[str, Any], max_records: int) -> str:
        raw = json.dumps({"p": params, "n": max_records}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def close(self) -> None:
        self._http.close()
