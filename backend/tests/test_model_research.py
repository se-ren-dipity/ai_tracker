import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from backend.collector import model_research as research


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    def test_nondisclosure_requires_evidence_and_availability_uses_separate_fields(self):
        content = "Example v2 is a closed model. Parameters are not disclosed. The API is in preview."
        findings = research.Findings(updates=[
            {"field":"weight_access", "value":"closed", "source_id":1, "quote":"Example v2 is a closed model."},
            {"field":"api_availability", "value":"preview", "source_id":1, "quote":"The API is in preview."},
            {"field":"weight_access", "value":"Apache-2.0", "source_id":1, "quote":"Example v2 is a closed model."}], field_statuses=[
            {"field":"params_b", "state":"undisclosed", "source_id":1, "quote":"Parameters are not disclosed."},
            {"field":"license", "state":"undisclosed"},
            {"field":"context_length", "state":"not_found"}])
        result = research.validate_findings(findings, [{"id":1, "url":"https://example.com", "content":content}])
        self.assertEqual(len(result['updates']), 2)
        self.assertEqual([row['field'] for row in result['field_statuses']], ['params_b', 'context_length'])

    def test_quotes_and_numeric_values_must_match_actual_content(self):
        content = "Example v2 has 9B total parameters and a 128K token context."
        findings = research.Findings(identity=[{"source_id": 1, "quote": content}], updates=[
            {"field": "params_b", "value": 9, "source_id": 1, "quote": content},
            {"field": "context_length", "value": 131072, "source_id": 1, "quote": content},
            {"field": "params_b", "value": 900, "source_id": 1, "quote": content},
            {"field": "license", "value": "MIT", "source_id": 2, "quote": "Released under the MIT license"}])
        result = research.validate_findings(findings, [{"id": 1, "url": "https://example.com/model", "content": content}], "Example v2")
        self.assertEqual(len(result["updates"]), 2)
        self.assertEqual(len(result["identity"]), 1)
        self.assertEqual(len(result["unresolved"]), 2)
        self.assertEqual(research.validate_findings(findings, [{"id": 1, "url": "https://example.com/model", "content": content}], "Example v3")["identity"], [])

    def test_search_summaries_are_not_body_evidence_and_estimates_are_not_facts(self):
        quote = "Example model has an estimated 600 billion parameters."
        findings = research.Findings(updates=[{"field": "params_b", "value": 600, "source_id": 1, "quote": quote}])
        for page in ({"id": 1, "content": "", "summary": quote}, {"id": 1, "content": quote, "url": "https://example.com"}):
            self.assertEqual(research.validate_findings(findings, [page])["updates"], [])

    def test_fill_only_missing_fields_and_refine_dates_within_same_month(self):
        draft = {"params_b": 9, "context_length": None, "release_date": "2026-10-01", "release_precision": "month"}
        findings = {"updates": [{"field": "params_b", "value": 10}, {"field": "context_length", "value": 131072},
                               {"field": "release_date", "value": "2026-09-30"}, {"field": "release_date", "value": "2026-10-13"}]}
        output, applied = research.apply_findings(draft, findings)
        self.assertEqual(output["params_b"], 9)
        self.assertEqual(output["context_length"], 131072)
        self.assertEqual(output["release_date"], "2026-10-13")
        self.assertEqual(output["release_precision"], "day")
        self.assertEqual(len(applied), 2)
        self.assertIsNone(draft["context_length"])

    async def test_unknown_private_urls_are_never_fetched_and_search_is_bounded(self):
        client = MagicMock()
        client.post = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"results": []}))
        session = research.ResearchSession(client, {"name": "Example"}, {})
        with patch.object(research, "get_client", return_value=SimpleNamespace(api_key="test-key")):
            for url in ["http://127.0.0.1/secrets", "http://localhost", "http://169.254.169.254", "file:///a", "https://example.com/not-discovered"]:
                self.assertIn("error", await session.fetch(url))
            for i in range(5):
                await session.search(f"Example query {i}")
        self.assertEqual(client.post.await_count, research.MAX_SEARCHES)
        self.assertTrue(all("api.deepseek.com" in call.args[0] for call in client.post.call_args_list))

    async def test_transient_search_failure_retries_but_forbidden_does_not(self):
        response = httpx.Response(429, request=httpx.Request("POST", "https://api.deepseek.com/v1/tools/search"))
        success = httpx.Response(200, json={"results": []}, request=response.request)
        client = MagicMock()
        client.post = AsyncMock(side_effect=[response, success])
        session = research.ResearchSession(client, {"name": "Example"}, {})
        with patch.object(research, "get_client", return_value=SimpleNamespace(api_key="test-key")), patch.object(research.asyncio, "sleep", AsyncMock()):
            self.assertEqual(await session.search("Example model"), {"sources": []})
            self.assertEqual(client.post.await_count, 2)
            client.post = AsyncMock(return_value=httpx.Response(403, request=response.request))
            self.assertIn("error", await session.execute("search_web", {"query": "Example model 2"}))
            client.post.assert_awaited_once()

    async def test_agent_calls_read_tool_and_returns_traceable_enrichment(self):
        quote = "Example v2 has a 1M token context window."
        source = {"id": 1, "url": "https://example.com/model", "title": "Model", "content": quote}
        tool_call = SimpleNamespace(id="call1", function=SimpleNamespace(name="read_page", arguments=json.dumps({"url": source["url"]})))
        def message(calls=None, content=""):
            return SimpleNamespace(tool_calls=calls, content=content, model_dump=lambda **kwargs: {"role": "assistant", "content": content})
        findings = {"identity": [{"source_id": 1, "quote": quote}], "updates": [{"source_id": 1, "quote": quote, "field": "context_length", "value": 1000000}], "unresolved": ["参数量未公开"]}
        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(message=message([tool_call]))]),
            SimpleNamespace(choices=[SimpleNamespace(message=message(content="Enough evidence"))]),
            SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=message(content=json.dumps(findings)))])])
        async def execute(session, name, arguments):
            session.sources = [source]
            session.queries = ["Example v2 official"]
            return {"sources": [source]}
        with patch.object(research, "get_client", return_value=client), patch.object(research.ResearchSession, "execute", execute):
            result = await research.research_model({"name": "Example v2"}, {})
        self.assertEqual(result["updates"][0]["value"], 1000000)
        self.assertTrue(result["identity"])
        self.assertEqual(result["sources"][0]["url"], source["url"])


if __name__ == "__main__":
    unittest.main()
