"""Tests for the OpenFIGI adapter with mocked HTTP. Nothing here touches the network."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tornado.httpclient import HTTPClientError

from cointoss.instrument import FigiResolution, ReferenceKind
from cointoss.sources.openfigi import (
    ANONYMOUS_BATCH,
    API_KEY_ENV,
    MappingJob,
    OpenFigiClient,
)

GOOG_RECORD = {
    "figi": "BBG009S3NB21",
    "name": "ALPHABET INC-CL C",
    "ticker": "GOOG",
    "exchCode": "US",
    "compositeFIGI": "BBG009S3NB30",
    "shareClassFIGI": "BBG009S3NB21",
    "securityType": "Common Stock",
    "marketSector": "Equity",
}


def _mock_client(bodies: list[Any]) -> MagicMock:
    """Mock AsyncHTTPClient returning each body in turn, recording the requests made."""
    responses = []
    for body in bodies:
        response = MagicMock()
        response.body = json.dumps(body).encode()
        responses.append(response)
    client = MagicMock()
    client.fetch = AsyncMock(side_effect=responses)
    return client


async def test_batches_requests_and_sends_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Jobs are chunked to the anonymous limit; a key raises the limit and rides in a header."""
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    jobs = [MappingJob(idType="TICKER", idValue=f"T{i}", exchCode="US") for i in range(12)]
    client = _mock_client(
        [[{"warning": "No identifier found."}] * 10, [{"data": [GOOG_RECORD]}] * 2]
    )
    with patch("cointoss.sources.openfigi.AsyncHTTPClient", return_value=client):
        results = await OpenFigiClient().map_identifiers(jobs)
    assert len(results) == len(jobs)
    sent = [json.loads(call.kwargs["body"]) for call in client.fetch.call_args_list]
    assert [len(batch) for batch in sent] == [ANONYMOUS_BATCH, 2]
    assert sent[0][0] == {"idType": "TICKER", "idValue": "T0", "exchCode": "US"}
    assert "X-OPENFIGI-APIKEY" not in client.fetch.call_args_list[0].kwargs["headers"]

    keyed = _mock_client([[{"data": [GOOG_RECORD]}] * 12])
    with patch("cointoss.sources.openfigi.AsyncHTTPClient", return_value=keyed):
        await OpenFigiClient(api_key="secret").map_identifiers(jobs)
    assert keyed.fetch.call_count == 1
    assert keyed.fetch.call_args.kwargs["headers"]["X-OPENFIGI-APIKEY"] == "secret"


async def test_parses_records_into_identity_references(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hit yields the composite FIGI as anchor and the share class FIGI as a secondary."""
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    client = _mock_client([[{"data": [GOOG_RECORD]}, {"warning": "No identifier found."}]])
    with patch("cointoss.sources.openfigi.AsyncHTTPClient", return_value=client):
        results = await OpenFigiClient().map_tickers(["GOOG", "NOSUCH"])
    hit, miss = results
    assert hit.resolution is FigiResolution.RESOLVED
    refs = hit.data[0].equity_references()
    assert [(r.kind, r.value) for r in refs] == [
        (ReferenceKind.COMPOSITE_FIGI, "BBG009S3NB30"),
        (ReferenceKind.SHARE_CLASS_FIGI, "BBG009S3NB21"),
    ]
    assert hit.data[0].crypto_references()[0].kind is ReferenceKind.ASSET_FIGI
    assert miss.resolution is FigiResolution.NOT_FOUND
    assert miss.data == []


async def test_failures_degrade_to_not_attempted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rate limits and transport errors must not raise; ingestion continues unresolved."""
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    jobs = [MappingJob(idType="TICKER", idValue=f"T{i}") for i in range(3)]
    failing = MagicMock()
    failing.fetch = AsyncMock(side_effect=HTTPClientError(429, "Too Many Requests"))
    with patch("cointoss.sources.openfigi.AsyncHTTPClient", return_value=failing):
        results = await OpenFigiClient().map_identifiers(jobs)
    assert [r.resolution for r in results] == [FigiResolution.NOT_ATTEMPTED] * 3
    assert all(r.error for r in results)


async def test_short_response_still_lines_up_with_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A truncated response must not silently shift results onto the wrong jobs."""
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    jobs = [MappingJob(idType="TICKER", idValue=t) for t in ("A", "B", "C")]
    client = _mock_client([[{"data": [GOOG_RECORD]}]])
    with patch("cointoss.sources.openfigi.AsyncHTTPClient", return_value=client):
        results = await OpenFigiClient().map_identifiers(jobs)
    assert [r.resolution for r in results] == [
        FigiResolution.RESOLVED,
        FigiResolution.NOT_ATTEMPTED,
        FigiResolution.NOT_ATTEMPTED,
    ]
