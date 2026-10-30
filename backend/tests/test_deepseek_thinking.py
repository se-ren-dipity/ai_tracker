import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from backend.collector import insight_agent, insight_editor, model_agent, model_discovery, model_research, model_review
from backend.tests.test_insights import result
from backend.tests.test_model_review import decision


def completion(content=None, *, tools=None, reasoning="reasoning fixture", finish="stop"):
    message = {"role": "assistant", "content": json.dumps(content) if isinstance(content, dict) else content,
               "reasoning_content": reasoning}
    if tools:
        message["tool_calls"] = tools
    return {"id": "test", "object": "chat.completion", "created": 0, "model": "deepseek-chat",
            "choices": [{"index": 0, "finish_reason": finish, "message": message}]}


class BackgroundThinkingTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def mock_api(self, module, responses):
        sent, remaining = [], iter(responses)

        def handle(request):
            sent.append({"body": json.loads(request.content), "timeout": request.extensions["timeout"]})
            return httpx.Response(200, json=next(remaining))

        async with AsyncOpenAI(api_key="test-key", base_url="https://deepseek.test", max_retries=0,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))) as client:
            with patch.object(module, "get_client", return_value=client):
                yield sent

    def assert_thinking(self, request, token_limit, timeout=300):
        self.assertEqual(request["body"]["thinking"], {"type": "enabled"})
        self.assertEqual(request["body"]["reasoning_effort"], "high")
        self.assertEqual(request["body"]["max_tokens"], token_limit)
        self.assertNotIn("temperature", request["body"])
        self.assertEqual(request["timeout"]["read"], timeout)

    async def test_background_json_tasks_send_thinking_settings_on_the_wire(self):
        cases = [
            (insight_agent, lambda: insight_agent.analyze_batch([
                {"id": 1, "title": "Example", "body_text": "Example v2 增加流式接口。", "links": "[]"}], []),
             {"items": [result().model_dump()]}, 65536, 600),
            (insight_editor, lambda: insight_editor.edit_batch([{"id": 1}]),
             {"items": [{"id": 1, "title": "模型发布", "summary": "增加接口", "paragraphs": ["导语", "正文"]}]}, 65536, 600),
            (model_discovery, lambda: model_discovery.extract_batch([
                {"source_item_id": 1, "title": "Release", "body": "Example v2 is released."}]),
             {"items": [{"source_item_id": 1, "candidates": [{"name": "Example v2", "quote": "Example v2 is released."}],
                         "reason": "发布模型"}]}, 65536, 600),
            (model_review, lambda: model_review.judge_batch([{"item_id": 1}], []),
             {"items": [decision().model_dump()]}, 12192, 300),
            (model_agent, lambda: model_agent._extract_benchmarks("MMLU score: 88.2. " * 20, "Example v2"),
             {"MMLU": 88.2}, 8992, 300),
        ]
        for module, run, output, token_limit, timeout in cases:
            with self.subTest(task=module.__name__):
                async with self.mock_api(module, [completion(output)]) as sent:
                    self.assertTrue(await run())
                self.assertEqual(len(sent), 1)
                self.assert_thinking(sent[0], token_limit, timeout)

    async def test_research_preserves_reasoning_across_tool_rounds(self):
        quote = "Example v2 has a 1M token context window."
        source = {"id": 1, "url": "https://example.com/model", "title": "Model", "content": quote}
        tool = {"id": "call1", "type": "function", "function": {
            "name": "read_page", "arguments": json.dumps({"url": source["url"]})}}
        findings = {"identity": [{"source_id": 1, "quote": quote}], "updates": [
            {"source_id": 1, "quote": quote, "field": "context_length", "value": 1000000}], "unresolved": []}

        async def execute(session, name, arguments):
            session.sources = [source]
            return {"sources": [source]}

        for initial_reasoning in ("plan fixture", ""):
            with self.subTest(initial_reasoning=initial_reasoning):
                responses = [completion(tools=[tool], reasoning=initial_reasoning, finish="tool_calls"),
                             completion("Ready", reasoning="review fixture"), completion(findings)]
                async with self.mock_api(model_research, responses) as sent:
                    with patch.object(model_research.ResearchSession, "execute", execute):
                        output = await model_research.research_model({"name": "Example v2"}, {})
                self.assertEqual(output["updates"][0]["value"], 1000000)
                self.assertEqual(len(sent), 3)
                for request, limit in zip(sent, [11192, 11192, 12192]):
                    self.assert_thinking(request, limit)
                self.assertEqual(sent[1]["body"]["messages"][2]["reasoning_content"], initial_reasoning)
                self.assertEqual(sent[1]["body"]["messages"][2]["tool_calls"], [tool])
                self.assertEqual([message["reasoning_content"] for message in sent[2]["body"]["messages"]
                                  if message["role"] == "assistant"], [initial_reasoning, "review fixture"])
                self.assertNotIn("tools", sent[2]["body"])

    async def test_truncated_research_planning_cannot_report_success(self):
        async with self.mock_api(model_research, [completion("", finish="length")]) as sent:
            with self.assertRaisesRegex(ValueError, "被截断"):
                await model_research.research_model({"name": "Example v2"}, {})
        self.assertEqual(len(sent), 1)

    async def test_truncated_benchmark_json_is_not_saved_as_a_valid_result(self):
        async with self.mock_api(model_agent, [completion({"MMLU": 88.2}, finish="length")]):
            self.assertEqual(await model_agent._extract_benchmarks("Model card " * 30, "Example v2"), {})
