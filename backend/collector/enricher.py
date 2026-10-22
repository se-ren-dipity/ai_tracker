"""Shared DeepSeek client for information analysis and model enrichment."""
import os

from openai import AsyncOpenAI

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"
_client = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.getenv("DEEPSEEK_API_KEY", ""),
                              base_url=DEEPSEEK_BASE_URL, timeout=60, max_retries=0)
    return _client
