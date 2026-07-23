import os

import cost_guard

NAME = "gemini"


def generate(system_prompt: str, user_prompt: str) -> str | None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    # Monthly spend cap — if this month's estimated Gemini cost already hit
    # the budget, skip Gemini so the caller falls back to the free
    # Groq/Cerebras providers. Prevents the bill from ever running past
    # GEMINI_MONTHLY_BUDGET_THB regardless of Google's own quota settings.
    if not cost_guard.can_spend():
        return None

    import google.generativeai as genai

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-3.6-flash", system_instruction=system_prompt)
    # temperature=0 → greedy decoding: the same prompt yields the same
    # answer (as close to deterministic as the API allows), so a setup
    # can't flip buy/sell run-to-run and results become reproducible.
    response = model.generate_content(
        user_prompt,
        generation_config={"temperature": 0.0, "top_p": 1.0, "top_k": 1},
    )

    # Record estimated spend from the API's real token counts when present,
    # else a chars/4 estimate.
    try:
        um = getattr(response, "usage_metadata", None)
        in_tok = getattr(um, "prompt_token_count", None) or cost_guard.estimate_tokens(system_prompt + user_prompt)
        out_tok = getattr(um, "candidates_token_count", None) or cost_guard.estimate_tokens(response.text or "")
        cost_guard.record(in_tok, out_tok)
    except Exception:
        pass

    return response.text
