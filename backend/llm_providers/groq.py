import os
from pathlib import Path
from dotenv import load_dotenv

NAME = "groq"


def generate(system_prompt: str, user_prompt: str) -> str | None:
    _e1 = Path(__file__).parent / ".env"
    _e2 = Path(__file__).parent.parent / ".env"
    _e3 = Path(__file__).parent.parent.parent / ".env"
    for e in (_e1, _e2, _e3):
        if e.exists():
            load_dotenv(e, override=True)
    load_dotenv(override=True)

    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        return None

    try:
        from groq import Groq
    except Exception:
        return None

    models = [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
    ]

    client = Groq(api_key=api_key)
    for m in models:
        try:
            response = client.chat.completions.create(
                model=m,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=1500,
                temperature=0,
            )
            if response and response.choices:
                return response.choices[0].message.content
        except Exception as err:
            if "rate_limit" in str(err).lower() or "429" in str(err):
                continue
            print(f"[Groq Error {m}]: {err}")
            continue

    return None
