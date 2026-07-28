import os

NAME = "cerebras"


def generate(system_prompt: str, user_prompt: str) -> str | None:
    from pathlib import Path
    from dotenv import load_dotenv

    _e1 = Path(__file__).parent / ".env"
    _e2 = Path(__file__).parent.parent / ".env"
    _e3 = Path(__file__).parent.parent.parent / ".env"
    for e in (_e1, _e2, _e3):
        if e.exists():
            load_dotenv(e)
    load_dotenv()

    api_key = os.environ.get("CEREBRAS_API_KEY")
    if not api_key:
        return None

    from cerebras.cloud.sdk import Cerebras

    client = Cerebras(api_key=api_key)
    response = client.chat.completions.create(
        model="gpt-oss-120b",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=1500,
        temperature=0,   # deterministic: same setup -> same decision
    )
    return response.choices[0].message.content
