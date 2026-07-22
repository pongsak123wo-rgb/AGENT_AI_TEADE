import os

NAME = "gemini"


def generate(system_prompt: str, user_prompt: str) -> str | None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    import google.generativeai as genai

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash", system_instruction=system_prompt)
    # temperature=0 → greedy decoding: the same prompt yields the same
    # answer (as close to deterministic as the API allows), so a setup
    # can't flip buy/sell run-to-run and results become reproducible.
    response = model.generate_content(
        user_prompt,
        generation_config={"temperature": 0.0, "top_p": 1.0, "top_k": 1},
    )
    return response.text
