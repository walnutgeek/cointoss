"""OpenFIGI v3 mapping API client.

Batch mapping only: identifiers in, FIGI records out. Failures -- transport, rate limit, empty
match -- come back as a `FigiResolution` other than `RESOLVED` rather than as an exception, so
an upstream outage degrades ingestion into unresolved Instruments instead of halting it.

Nothing here is cached. Caching a namespace callable requires every parameter to resolve to a
`KnownType` with `simple_type=True`, because the parameters become the cache table's primary
key. Both entry points take a collection, and no collection type qualifies -- not `list[str]`,
and not `Universe`. FIGI mappings are cached one layer up instead, keyed by a universe's name
and date rather than by its contents -- see ADR-0006. The module tests enforce that nothing
here claims a cache it cannot have.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, ClassVar

from lythonic.compose.namespace import NamespaceFragment, nsnode
from lythonic.universe import Universe
from pydantic import BaseModel, ConfigDict
from tornado.httpclient import AsyncHTTPClient

from cointoss.instrument import ExternalReference, FigiResolution

log = logging.getLogger(__name__)

MAPPING_URL = "https://api.openfigi.com/v3/mapping"
API_KEY_ENV = "OPENFIGI_API_KEY"

# OpenFIGI caps a mapping request at 10 jobs anonymously and 100 with an API key.
ANONYMOUS_BATCH = 10
KEYED_BATCH = 100


# -- Request and response models --


class MappingJob(BaseModel):
    """One mapping request. `idType`/`idValue` are OpenFIGI's own vocabulary."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")
    idType: str
    idValue: str
    exchCode: str | None = None
    micCode: str | None = None
    currency: str | None = None
    marketSecDes: str | None = None
    securityType: str | None = None
    securityType2: str | None = None

    def payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class FigiRecord(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")
    figi: str
    name: str | None = None
    ticker: str | None = None
    exchCode: str | None = None
    compositeFIGI: str | None = None
    shareClassFIGI: str | None = None
    uniqueID: str | None = None
    securityType: str | None = None
    securityType2: str | None = None
    securityDescription: str | None = None
    marketSector: str | None = None

    def equity_references(self) -> list[ExternalReference]:
        """External References for a listed equity: composite FIGI anchors, share class rides along.

        Venue-level and currency-pair FIGIs describe markets, so they are not identity references.
        """
        refs: list[ExternalReference] = []
        if self.compositeFIGI:
            refs.append(ExternalReference.composite_figi(self.compositeFIGI))
        if self.shareClassFIGI:
            refs.append(ExternalReference.share_class_figi(self.shareClassFIGI))
        return refs

    def crypto_references(self) -> list[ExternalReference]:
        """External References for crypto: the asset-level FIGI is the anchor."""
        return [ExternalReference.asset_figi(self.figi)]


class MappingResult(BaseModel):
    """One entry of a mapping response, positionally aligned with the job that produced it.

    `attempted` is local bookkeeping, not part of the wire format: it distinguishes a request
    OpenFIGI answered with no match from one that never reached OpenFIGI at all.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")
    data: list[FigiRecord] = []
    warning: str | None = None
    error: str | None = None
    attempted: bool = True

    @property
    def resolution(self) -> FigiResolution:
        if self.data:
            return FigiResolution.RESOLVED
        return FigiResolution.NOT_FOUND if self.attempted else FigiResolution.NOT_ATTEMPTED


# -- Client --


class OpenFigiClient(NamespaceFragment):
    """Async HTTP client for the OpenFIGI v3 mapping endpoint.

    Args:
        api_key: OpenFIGI API key. Falls back to the `OPENFIGI_API_KEY` environment variable;
            without one the endpoint still works at the smaller anonymous batch size.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key: str | None = api_key or os.environ.get(API_KEY_ENV) or None

    @property
    def batch_size(self) -> int:
        return KEYED_BATCH if self._api_key else ANONYMOUS_BATCH

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-OPENFIGI-APIKEY"] = self._api_key
        return headers

    async def _post(self, batch: list[MappingJob]) -> list[MappingResult]:
        """POST one batch, degrading any failure into not-attempted results for that batch."""
        client = AsyncHTTPClient()
        body = json.dumps([job.payload() for job in batch])
        try:
            response = await client.fetch(
                MAPPING_URL, method="POST", headers=self._headers(), body=body
            )
            parsed = json.loads(response.body)
            return [MappingResult.model_validate(item) for item in parsed]
        except Exception as exc:
            log.warning("OpenFIGI mapping failed for %d jobs: %s", len(batch), exc)
            return [MappingResult(error=str(exc), attempted=False) for _ in batch]

    @nsnode(tags=["api"])
    async def map_identifiers(self, jobs: list[MappingJob]) -> list[MappingResult]:
        """Map identifiers to FIGI records, one result per job in the order given.

        Jobs are split into batches of `batch_size`. A batch that fails yields not-attempted
        results without affecting the others.
        """
        results: list[MappingResult] = []
        for start in range(0, len(jobs), self.batch_size):
            batch = jobs[start : start + self.batch_size]
            batch_results = await self._post(batch)
            # A short or malformed response must still line up with the jobs that caused it.
            if len(batch_results) != len(batch):
                log.warning(
                    "OpenFIGI returned %d results for %d jobs", len(batch_results), len(batch)
                )
                batch_results = batch_results[: len(batch)] + [
                    MappingResult(error="missing result", attempted=False)
                    for _ in range(len(batch) - len(batch_results))
                ]
            results.extend(batch_results)
        return results

    @nsnode(tags=["api"])
    async def map_tickers(
        self, tickers: Universe | list[str], exch_code: str = "US"
    ) -> list[MappingResult]:
        """Map exchange tickers, the common case for listed equities.

        Takes a `Universe`, so the ticker set is ordered and duplicate-free: a duplicate is a
        caller mistake that would otherwise cost a wasted job and return a redundant result.
        Results stay positional against the universe order.
        """
        return await self.map_identifiers(
            [MappingJob(idType="TICKER", idValue=t, exchCode=exch_code) for t in Universe(tickers)]
        )
