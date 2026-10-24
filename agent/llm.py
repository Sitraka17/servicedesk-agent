"""Chat model selection. DEMO_LLM picks the provider and model, e.g.

  gemini:gemini-3.5-flash      (needs GOOGLE_API_KEY)
  ollama:qwen3:4b              (local, needs `ollama pull qwen3:4b`)
  anthropic:claude-haiku-4-5   (needs ANTHROPIC_API_KEY)

Without DEMO_LLM: Gemini if GOOGLE_API_KEY is set, otherwise Ollama.
"""

import os


def default_spec() -> str:
    if os.environ.get("DEMO_LLM"):
        return os.environ["DEMO_LLM"]
    if os.environ.get("GOOGLE_API_KEY"):
        return "gemini:gemini-3.5-flash"
    return "ollama:qwen3:4b"


def rate_limiter():
    """Client-side throttle. DEMO_RPM=4 keeps under the Gemini free tier (5 requests per minute)."""
    rpm = float(os.environ.get("DEMO_RPM", "0"))
    if not rpm:
        return None
    from langchain_core.rate_limiters import InMemoryRateLimiter

    return InMemoryRateLimiter(requests_per_second=rpm / 60, check_every_n_seconds=0.5, max_bucket_size=1)


def make_llm(spec: str | None = None):
    spec = spec or default_spec()
    provider, _, model = spec.partition(":")
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=model, temperature=0, max_retries=1, rate_limiter=rate_limiter())
    if provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(model=model, temperature=0, reasoning=True)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model, temperature=0)
    raise ValueError(f"Unknown provider in DEMO_LLM={spec!r}")
