"""Core ingestion pipeline: source -> extract -> resolve -> store.

Optimized for minimal LLM/API calls:
- Batch entity embeddings (1 API call for all entities)
- Batch observation embeddings (1 API call for all observations)
- Batch entity merge confirmation (1 LLM call for all candidates)
- Dedicated entity-only similarity search (skips observations/tasks)
"""
from __future__ import annotations

from typing import Any

from openai import OpenAI
from supabase import Client

from open_brain import db
from open_brain.embeddings import embed_texts, embed_single
from open_brain.extraction.extractor import extract_knowledge
from open_brain.extraction.entity_resolver import (
    resolve_entities_batch,
)


def ingest_content(
    supabase_client: Client,
    embed_client: OpenAI,
    embed_model: str,
    chat_client: OpenAI,
    chat_model: str,
    content: str,
    source_type: str,
    origin: str,
    title: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full ingestion pipeline for a single piece of content.

    Returns summary of what was extracted and stored.
    """
    source = db.upsert_source(
        supabase_client, content, source_type, origin, title, metadata
    )
    if source is None:
        return {"status": "duplicate", "message": "Content already ingested"}

    source_id = source["id"]

    try:
        # 1. Extract knowledge (1 LLM call)
        extraction = extract_knowledge(
            chat_client, chat_model, content, source_type, title
        )

        # 2. Resolve entities in batch (1 embedding call + 0-1 LLM calls)
        entity_name_to_id = resolve_entities_batch(
            supabase_client,
            embed_client, embed_model,
            chat_client, chat_model,
            extraction.entities,
        )

        # 3. Store relations (DB calls only, no LLM)
        for relation in extraction.relations:
            source_eid = entity_name_to_id.get(relation.source)
            target_eid = entity_name_to_id.get(relation.target)
            if source_eid and target_eid:
                db.insert_relation(
                    supabase_client,
                    source_entity_id=source_eid,
                    target_entity_id=target_eid,
                    relation_type=relation.relation_type,
                    description=relation.description,
                    source_id=source_id,
                )

        # 4. Batch embed all observations (1 embedding call)
        if extraction.observations:
            obs_texts = [obs.content for obs in extraction.observations]
            obs_embeddings = embed_texts(embed_client, embed_model, obs_texts)

            for obs, obs_embedding in zip(extraction.observations, obs_embeddings):
                obs_entity_ids = [
                    entity_name_to_id[name]
                    for name in obs.entities
                    if name in entity_name_to_id
                ]
                db.insert_observation(
                    supabase_client,
                    content=obs.content,
                    embedding=obs_embedding,
                    observation_type=obs.observation_type,
                    entity_ids=obs_entity_ids,
                    source_id=source_id,
                    metadata=metadata,
                )

        db.mark_source_extracted(supabase_client, source_id)

        return {
            "status": "success",
            "source_id": source_id,
            "entities_count": len(extraction.entities),
            "relations_count": len(extraction.relations),
            "observations_count": len(extraction.observations),
            "entity_map": entity_name_to_id,
        }

    except Exception as e:
        db.mark_source_failed(supabase_client, source_id, str(e))
        return {"status": "failed", "source_id": source_id, "error": str(e)}


def retry_extraction(
    supabase_client: Client,
    embed_client: OpenAI,
    embed_model: str,
    chat_client: OpenAI,
    chat_model: str,
    source: dict[str, Any],
) -> dict[str, Any]:
    """Re-run extraction on an existing source. Skips source insert and content embedding."""
    source_id = source["id"]
    content = source["raw_content"]
    source_type = source["source_type"]
    title = source.get("title", "")
    metadata = source.get("metadata")

    try:
        extraction = extract_knowledge(
            chat_client, chat_model, content, source_type, title
        )

        entity_name_to_id = resolve_entities_batch(
            supabase_client,
            embed_client, embed_model,
            chat_client, chat_model,
            extraction.entities,
        )

        for relation in extraction.relations:
            source_eid = entity_name_to_id.get(relation.source)
            target_eid = entity_name_to_id.get(relation.target)
            if source_eid and target_eid:
                db.insert_relation(
                    supabase_client,
                    source_entity_id=source_eid,
                    target_entity_id=target_eid,
                    relation_type=relation.relation_type,
                    description=relation.description,
                    source_id=source_id,
                )

        if extraction.observations:
            obs_texts = [obs.content for obs in extraction.observations]
            obs_embeddings = embed_texts(embed_client, embed_model, obs_texts)

            for obs, obs_embedding in zip(extraction.observations, obs_embeddings):
                obs_entity_ids = [
                    entity_name_to_id[name]
                    for name in obs.entities
                    if name in entity_name_to_id
                ]
                db.insert_observation(
                    supabase_client,
                    content=obs.content,
                    embedding=obs_embedding,
                    observation_type=obs.observation_type,
                    entity_ids=obs_entity_ids,
                    source_id=source_id,
                    metadata=metadata,
                )

        db.mark_source_extracted(supabase_client, source_id)

        return {
            "status": "success",
            "source_id": source_id,
            "entities_count": len(extraction.entities),
            "relations_count": len(extraction.relations),
            "observations_count": len(extraction.observations),
        }

    except Exception as e:
        db.mark_source_failed(supabase_client, source_id, str(e))
        return {"status": "failed", "source_id": source_id, "error": str(e)}
