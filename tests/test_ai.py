import json
import unittest
from unittest.mock import patch

import httpx

from ai_sql_agent.ai import ProviderError, create_provider


class ChatCompletionsTests(unittest.TestCase):
    def test_planning_and_reporting_use_the_selected_provider(self):
        providers = (
            ("openai", "https://api.openai.com/v1/chat/completions", "max_completion_tokens"),
            ("xai", "https://api.x.ai/v1/chat/completions", "max_tokens"),
            ("openrouter", "https://openrouter.ai/api/v1/chat/completions", "max_tokens"),
        )
        for provider_id, endpoint, token_parameter in providers:
            with self.subTest(provider=provider_id):
                requests = []

                def respond(request):
                    self.assertEqual(str(request.url), endpoint)
                    self.assertEqual(request.headers["Authorization"], f"Bearer {provider_id}-test-key")
                    payload = json.loads(request.content)
                    requests.append(payload)
                    if len(requests) == 1:
                        content = {"sql": "SELECT total FROM sales", "clarification": ""}
                    else:
                        content = {"observations": ["One row returned."], "chart": {"type": "bar", "x": "total"}}
                    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})

                client_factory = httpx.Client
                with patch("ai_sql_agent.ai.httpx.Client", side_effect=lambda **kwargs: client_factory(
                    transport=httpx.MockTransport(respond), **kwargs,
                )):
                    provider = create_provider(provider_id, f"{provider_id}-test-key", "chosen-model")
                self.addCleanup(provider.close)

                sql = provider.plan("Show sales", {"dialect": "sqlite"}, [], [], previous_sql="SELECT 0", error="bad SQL")
                self.assertEqual(sql, "SELECT total FROM sales")
                report = provider.analyze("Show sales", sql, {"row_count": 1})
                self.assertEqual(report.insights, "- One row returned.")
                self.assertEqual(report.chart, {"type": "bar", "x": "total"})

                for request in requests:
                    self.assertEqual(request["model"], "chosen-model")
                    self.assertEqual(request["response_format"], {"type": "json_object"})
                    self.assertEqual(request[token_parameter], 4096)
                planning_data = json.loads(requests[0]["messages"][1]["content"])
                self.assertEqual(planning_data["repair"], {"previous_sql": "SELECT 0", "database_error": "bad SQL"})
                if provider_id == "openrouter":
                    self.assertEqual(requests[0]["provider"], {"require_parameters": True})

    def test_provider_errors_do_not_expose_response_bodies(self):
        for status in (401, 429):
            with self.subTest(status=status):
                client = httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, text="sensitive response body"),
                ))
                with patch("ai_sql_agent.ai.httpx.Client", return_value=client):
                    provider = create_provider("openai", "test-key")
                self.addCleanup(provider.close)
                with self.assertRaises(ProviderError) as caught:
                    provider.plan("Show sales", {}, [], [])
                self.assertNotIn("sensitive response body", str(caught.exception))
                self.assertIn("OpenAI", str(caught.exception))
