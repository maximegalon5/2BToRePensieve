from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class SupabaseConfig:
    url: str
    anon_key: str
    service_role_key: str
    db_url: str  # Direct Postgres connection string


@dataclass
class OpenRouterConfig:
    api_key: str
    base_url: str = "https://openrouter.ai/api/v1"
    embedding_model: str = "openai/text-embedding-3-small"
    chat_model: str = "openai/gpt-4o-mini"


@dataclass
class LocalEmbedConfig:
    base_url: str = "http://127.0.0.1:1234/v1"
    api_key: str = "lmstudio-local"
    model: str = ""


@dataclass
class OpenBrainConfig:
    supabase: SupabaseConfig
    openrouter: OpenRouterConfig
    local_embed: LocalEmbedConfig
    embedding_dimensions: int = 1536


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def load_open_brain_config() -> OpenBrainConfig:
    return OpenBrainConfig(
        supabase=SupabaseConfig(
            url=_env("SUPABASE_URL"),
            anon_key=_env("SUPABASE_ANON_KEY"),
            service_role_key=_env("SUPABASE_SERVICE_ROLE_KEY"),
            db_url=_env("SUPABASE_DB_URL"),
        ),
        openrouter=OpenRouterConfig(
            api_key=_env("OPENROUTER_API_KEY"),
            embedding_model=_env("OPENROUTER_EMBED_MODEL", "openai/text-embedding-3-small"),
            chat_model=_env("OPENROUTER_CHAT_MODEL", "openai/gpt-4o-mini"),
        ),
        local_embed=LocalEmbedConfig(
            base_url=_env("LMSTUDIO_EMBED_BASE_URL", "http://127.0.0.1:1234/v1"),
            api_key=_env("LMSTUDIO_EMBED_API_KEY", "lmstudio-local"),
            model=_env("LMSTUDIO_EMBED_MODEL_1536", ""),
        ),
    )
