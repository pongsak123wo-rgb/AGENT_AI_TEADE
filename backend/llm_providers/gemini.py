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
    # gemini-2.0-flash, NOT a "thinking" model: gemini-3.x-flash silently
    # generates hidden reasoning tokens that are billed as (expensive)
    # output — a real invoice showed ~2000 output tokens per call for a
    # ~150-token JSON answer, driving most of the cost. 2.0-flash is ~6x
    # cheaper ($0.10/$0.40), has no thinking tokens, and its answers were
    # fine before. max_output_tokens also hard-caps runaway output.
    model = genai.GenerativeModel(os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
                                  system_instruction=system_prompt)
    response = model.generate_content(
        user_prompt,
        generation_config={"temperature": 0.0, "top_p": 1.0, "top_k": 1,
                           "max_output_tokens": 512},
    )

    # Record spend from the API's REAL token counts. Reading usage_metadata
    # reliably matters: when it silently fell back to a chars/4 estimate the
    # guard undercounted the bill ~24x. The fallback estimate now also counts
    # the system prompt (previously omitted) so it can't wildly underread.
    try:
        um = getattr(response, "usage_metadata", None)
        in_tok = int(getattr(um, "prompt_token_count", 0) or 0)
        out_tok = int(getattr(um, "candidates_token_count", 0) or 0)
        if in_tok <= 0:
            in_tok = cost_guard.estimate_tokens(system_prompt + user_prompt)
        if out_tok <= 0:
            out_tok = cost_guard.estimate_tokens(response.text or "")
        cost_guard.record(in_tok, out_tok)
    except Exception:
        pass

    return response.text
