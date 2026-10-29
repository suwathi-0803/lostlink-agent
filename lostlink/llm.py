"""LLM factory: one place to switch between Groq, Gemini, OpenAI or a local Ollama model."""
from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel

from . import config

KEY_ENV = {"groq": "GROQ_API_KEY", "gemini": "GOOGLE_API_KEY", "openai": "OPENAI_API_KEY"}


class MissingAPIKey(RuntimeError):
    pass


def check_api_key() -> str | None:
    """Return an error message if the configured provider has no key, else None."""
    env = KEY_ENV.get(config.LLM_PROVIDER)
    if env and not os.getenv(env):
        return (f"No {env} found. Copy .env.example to .env and paste your free "
                f"{config.LLM_PROVIDER.title()} API key there, then restart the app.")
    return None


def get_chat_model() -> BaseChatModel:
    problem = check_api_key()
    if problem:
        raise MissingAPIKey(problem)
    provider, model = config.LLM_PROVIDER, config.LLM_MODEL

    if provider == "groq":
        from langchain_groq import ChatGroq
        kwargs = {}
        if "gpt-oss" in model:
            kwargs["reasoning_effort"] = os.getenv("LLM_REASONING_EFFORT", "low")
        return ChatGroq(model=model, temperature=0, max_retries=6, **kwargs)

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model, temperature=0, max_retries=6)

    if provider == "openai":
        from langchain_openai import ChatOpenAI  # pip install langchain-openai
        return ChatOpenAI(model=model, temperature=0, max_retries=6)

    if provider == "ollama":
        from langchain_ollama import ChatOllama  # pip install langchain-ollama
        return ChatOllama(model=model, temperature=0)

    raise ValueError(f"Unknown LLM_PROVIDER '{provider}'. Use groq, gemini, openai or ollama.")
