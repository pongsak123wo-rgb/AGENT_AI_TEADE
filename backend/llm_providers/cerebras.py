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

    api_key = os.environ.get("CEREBRAS_API_KEY", "").strip()
    if not api_key:
        return None

    try:
        from cerebras.cloud.sdk import Cerebras
    except Exception:
        return None

    models = ["llama-3.3-70b", "llama3.1-70b", "llama3.1-8b"]

    try:
        client = Cerebras(api_key=api_key)
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
                if response and response.choices and response.choices[0].message.content:
                    return response.choices[0].message.content
            except Exception as e:
                continue
    except Exception as err:
        print(f"[Cerebras Error]: {err}")
        return None

    return None
