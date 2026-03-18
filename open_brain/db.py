"""Supabase database client for the knowledge graph."""
from __future__ import annotations

import hashlib
from typing import Any

from supabase import create_client, Client

from open_brain.config import OpenBrainConfig


def get_client(cfg: OpenBrainConfig) -> Client:
    return create_client(cfg.supabase.url, cfg.supabase.service_role_key)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def upsert_source(
    client: Client,
    raw_content: str,
    source_type: str,
    origin: str,
    title: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Insert a source if content_hash is new. Returns the source row or None if duplicate."""
    h = content_hash(raw_content)

    existing = client.table("sources").select("id").eq("content_hash", h).execute()
    if existing.data:
        return None

    row = {
        "source_type": source_type,
        "origin": origin,
        "title": title,
        "raw_content": raw_content,
        "content_hash": h,
        "status": "pending",
        "metadata": metadata or {},
    }
    result = client.table("sources").insert(row).execute()
    return result.data[0] if result.data else None


def upsert_entity(
    client: Client,
    name: str,
    entity_type: str,
    description: str,
    embedding: list[float],
    aliases: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert a new entity, or return existing if name already exists.

    Handles the unique constraint on lower(name) gracefully by falling
    back to a name-only lookup when the insert fails. This covers cases
    where the same entity is extracted with different types across chunks.
    """
    row = {
        "name": name,
        "entity_type": entity_type,
        "description": description,
        "embedding": embedding,
        "aliases": aliases or [],
        "metadata": metadata or {},
    }
    try:
        result = client.table("entities").insert(row).execute()
        return result.data[0]
    except Exception:
        # Unique constraint on lower(name) — entity exists, look up by name only
        existing = (
            client.table("entities")
            .select("*")
            .ilike("name", name)
            .limit(1)
            .execute()
        )
        if existing.data:
            return existing.data[0]
        # Re-raise if not a constraint issue
        raise


def merge_entity(
    client: Client,
    existing_id: str,
    new_alias: str,
    new_description: str | None = None,
    new_embedding: list[float] | None = None,
) -> None:
    """Merge a new entity name into an existing entity as an alias."""
    existing = client.table("entities").select("*").eq("id", existing_id).execute()
    if not existing.data:
        return

    entity = existing.data[0]
    aliases = entity.get("aliases", []) or []
    if new_alias not in aliases:
        aliases.append(new_alias)

    updates: dict[str, Any] = {"aliases": aliases}

    if new_description and len(new_description) > len(entity.get("description", "")):
        updates["description"] = new_description
        if new_embedding:
            updates["embedding"] = new_embedding

    client.table("entities").update(updates).eq("id", existing_id).execute()


def insert_relation(
    client: Client,
    source_entity_id: str,
    target_entity_id: str,
    relation_type: str,
    description: str,
    source_id: str | None = None,
    weight: float = 1.0,
) -> dict[str, Any] | None:
    """Insert a relation between two entities. Skips if same edge already exists."""
    # Dedup: check for existing (source, target, type) edge
    existing = (
        client.table("relations")
        .select("id")
        .eq("source_entity", source_entity_id)
        .eq("target_entity", target_entity_id)
        .eq("relation_type", relation_type)
        .limit(1)
        .execute()
    )
    if existing.data:
        return None  # edge already exists

    row = {
        "source_entity": source_entity_id,
        "target_entity": target_entity_id,
        "relation_type": relation_type,
        "description": description,
        "weight": weight,
        "source_id": source_id,
    }
    result = client.table("relations").insert(row).execute()
    return result.data[0]


def insert_observation(
    client: Client,
    content: str,
    embedding: list[float],
    observation_type: str,
    entity_ids: list[str],
    source_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Insert an observation linked to entities. Skips exact duplicates and near-duplicates (0.95+ similarity)."""
    # Dedup layer 1: exact content hash
    obs_hash = content_hash(content)
    existing = (
        client.table("observations")
        .select("id")
        .eq("content_hash", obs_hash)
        .limit(1)
        .execute()
    )
    if existing.data:
        return None  # exact duplicate

    # Dedup layer 2: semantic similarity check (0.95 threshold)
    similar = client.rpc(
        "search_knowledge",
        {
            "query_embedding": embedding,
            "match_count": 1,
            "filter_entity_type": None,
            "filter_observation_type": None,
        },
    ).execute()

    for r in similar.data or []:
        if r["result_type"] == "observation" and r["similarity"] >= 0.95:
            return None  # near-duplicate

    row = {
        "content": content,
        "content_hash": obs_hash,
        "embedding": embedding,
        "observation_type": observation_type,
        "entity_ids": entity_ids,
        "source_id": source_id,
        "metadata": metadata or {},
    }
    result = client.table("observations").insert(row).execute()
    return result.data[0]


def search_similar_entities(
    client: Client,
    embedding: list[float],
    threshold: float = 0.85,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Find entities similar to the given embedding via dedicated entity-only RPC.

    Uses search_similar_entities RPC which only queries the entities table
    (not observations or tasks), making it ~3x faster than search_knowledge.
    """
    result = client.rpc(
        "search_similar_entities",
        {
            "query_embedding": embedding,
            "match_count": limit,
            "similarity_threshold": threshold,
        },
    ).execute()

    return result.data or []


def mark_source_extracted(client: Client, source_id: str) -> None:
    client.table("sources").update({"status": "extracted"}).eq("id", source_id).execute()


def mark_source_failed(client: Client, source_id: str, error: str) -> None:
    client.table("sources").update({
        "status": "failed",
        "metadata": {"error": error},
    }).eq("id", source_id).execute()


def get_failed_sources(
    client: Client, source_type: str | None = None, limit: int = 0
) -> list[dict[str, Any]]:
    """Get sources with status='failed' for retry."""
    query = client.table("sources").select("*").eq("status", "failed")
    if source_type:
        query = query.eq("source_type", source_type)
    query = query.order("created_at", desc=False)
    if limit:
        query = query.limit(limit)
    return query.execute().data or []
