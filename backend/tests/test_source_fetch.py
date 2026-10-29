import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from backend.collector import source_fetch as source


class SourceFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        setting = patch.object(source, "cache_dir", return_value=Path(directory.name))
        setting.start()
        self.addCleanup(setting.stop)
        self.url = "https://example.com/feed?token=private-token"

    async def fetch(self, responses):
        client = AsyncMock()
        client.get.side_effect = responses
        with patch.object(source.httpx, "AsyncClient") as factory, patch.object(source.asyncio, "sleep", AsyncMock()):
            factory.return_value.__aenter__.return_value = client
            value = await source.fetch_source("Source", self.url, json.loads)
        return value, client.get.await_count

    async def test_transient_failure_recovers_and_valid_payload_is_cached(self):
        response = httpx.Response(200, json={"value": 1}, request=httpx.Request("GET", self.url))
        value, attempts = await self.fetch([httpx.ConnectError("offline"), response])
        self.assertEqual(attempts, 2)
        self.assertEqual(value, {"value": 1})
        self.assertEqual(source.source_status()[0]["status"], "ok")

    async def test_offline_cache_is_labelled_stale_and_does_not_claim_freshness(self):
        source.save_record(self.url, {"name": "Source", "content": '{"value": 1}', "fetched_at": "2026-10-26T00:00:00+00:00", "status": "ok"})
        value, attempts = await self.fetch([httpx.ConnectError("offline")] * 3)
        status = source.source_status()[0]
        self.assertEqual(value, {"value": 1})
        self.assertEqual(attempts, 3)
        self.assertEqual(status["status"], "cached")
        self.assertEqual(status["fetched_at"], "2026-10-26T00:00:00+00:00")
        self.assertNotIn("private-token", json.dumps(status))
        self.assertNotIn("content", status)

    async def test_invalid_live_data_cannot_replace_valid_cache(self):
        source.save_record(self.url, {"name": "Source", "content": '[1]', "fetched_at": "old", "status": "ok"})
        value, attempts = await self.fetch([httpx.Response(200, text="<html>Error</html>", request=httpx.Request("GET", self.url))])
        self.assertEqual(value, [1])
        self.assertEqual(attempts, 1)
        self.assertEqual(source.read_record(self.url)["content"], '[1]')

    async def test_no_cache_failure_is_persisted_and_raised(self):
        with self.assertRaises(httpx.ConnectError):
            await self.fetch([httpx.ConnectError("offline")] * 3)
        self.assertEqual(source.source_status()[0]["status"], "failed")

    async def test_recovery_clears_error(self):
        source.save_record(self.url, {"name": "Source", "status": "failed", "error": "ConnectError"})
        await self.fetch([httpx.Response(200, json=[], request=httpx.Request("GET", self.url))])
        self.assertEqual(source.source_status()[0]["status"], "ok")
        self.assertIsNone(source.source_status()[0]["error"])
