import os
from pathlib import Path
from dotenv import load_dotenv

NAME = "cerebras"


def generate(system_prompt: str, user_prompt: str) -> str | None:
    _e1 = Path(__file__).parent / ".env"
    _e2 = Path(__file__).parent.parent / ".env"
    _e3 = Path(__file__).parent.parent.parent / ".env"
    for e in (_e1, _e2, _e3):
        if e.exists():
            load_dotenv(e, override=True)
    load_dotenv(override=True)

    api_key = os.environ.get("CEREBRAS_API_KEY")
    if not api_key:
        return None

    try:
        from cerebras.cloud.sdk import Cerebras
    except Exception:
        return None

    try:
        client = Cerebras(api_key=api_key)
        response = client.chat.completions.create(
            model="llama3.1-8b",
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
        print(f"[Cerebras Error]: {err}")
        return None

    return None
