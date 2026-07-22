import os

NAME = "cerebras"


def generate(system_prompt: str, user_prompt: str) -> str | None:
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
