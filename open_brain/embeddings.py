"""Embedding client supporting both OpenRouter (cloud) and local LM Studio."""
from __future__ import annotations

from openai import OpenAI

from open_brain.config import OpenBrainConfig


def get_cloud_embedder(cfg: OpenBrainConfig) -> tuple[OpenAI, str]:
    """Return (client, model) for OpenRouter embeddings."""
    client = OpenAI(
        base_url=cfg.openrouter.base_url,
        api_key=cfg.openrouter.api_key,
    )
    return client, cfg.openrouter.embedding_model


def get_local_embedder(cfg: OpenBrainConfig) -> tuple[OpenAI, str]:
    """Return (client, model) for local LM Studio embeddings."""
    client = OpenAI(
        base_url=cfg.local_embed.base_url,
        api_key=cfg.local_embed.api_key,
    )
    return client, cfg.local_embed.model


def embed_texts(
    client: OpenAI,
    model: str,
    texts: list[str],
    batch_size: int = 64,
) -> list[list[float]]:
    """Embed a list of texts in batches. Returns list of embedding vectors."""
    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        response = client.embeddings.create(model=model, input=batch)
        all_embeddings.extend([e.embedding for e in response.data])

    return all_embeddings


def embed_single(client: OpenAI, model: str, text: str) -> list[float]:
    """Embed a single text. Returns one embedding vector."""
    response = client.embeddings.create(model=model, input=[text])
    return response.data[0].embedding
