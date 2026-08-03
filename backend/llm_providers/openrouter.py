"""OpenRouter free-tier provider.

Adds another FREE cloud LLM so the pipeline doesn't fall back to the rule
engine every time Cerebras/Groq are rate-limited. OpenRouter exposes several
`:free` models (no billing, no card) behind an OpenAI-compatible endpoint;
we call it over plain urllib so no extra package is needed. Set
OPENROUTER_API_KEY on the VPS (get one free at openrouter.ai/keys).

Paid models are never used — only the `:free` list below.
"""
import json
import os
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

NAME = "openrouter"

_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
# Free, no-billing models that currently EXIST on OpenRouter (verified against
# /api/v1/models — the old llama-3.3/deepseek/qwen :free IDs were retired and
# 404'd, which silently returned None). First that answers wins; order ≈
# quality. Re-check /api/v1/models if these ever start 404'ing again.
_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openai/gpt-oss-20b:free",
    "google/gemma-4-31b-it:free",
]


def generate(system_prompt: str, user_prompt: str) -> str | None:
    for e in (Path(__file__).parent / ".env",
              Path(__file__).parent.parent / ".env",
              Path(__file__).parent.parent.parent / ".env"):
        if e.exists():
            load_dotenv(e, override=True)
    load_dotenv(override=True)

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # OpenRouter asks for these; harmless if generic.
        "HTTP-Referer": "https://github.com/pongsak123wo-rgb/AGENT_AI_TEADE",
        "X-Title": "trading-room-ai",
    }
    for model in _MODELS:
        body = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": 1500,
        }).encode("utf-8")
        try:
            req = urllib.request.Request(_ENDPOINT, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choices = data.get("choices") or []
            if choices:
                content = (choices[0].get("message") or {}).get("content")
                if content:
                    return content
        except Exception as err:
            # 429 = this free model is rate-limited right now → try the next.
            if "429" in str(err) or "rate" in str(err).lower():
                continue
            print(f"[OpenRouter Error {model}]: {err}")
            continue
    return None
