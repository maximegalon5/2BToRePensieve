"""Entity resolution: batch embed + batch merge confirmation.

Optimized flow per chunk:
1. Batch embed all entity texts (1 API call)
2. Search for candidates per entity (DB calls only — dedicated entity RPC)
3. Batch LLM merge confirmation for ALL candidates at once (0-1 LLM call)
4. Upsert or merge each entity (DB calls only)
"""
from __future__ import annotations

import json

from openai import OpenAI
from supabase import Client

from open_brain.db import upsert_entity, merge_entity
from open_brain.embeddings import embed_texts
from open_brain.extraction.extractor import ExtractedEntity


def resolve_entities_batch(
    db: Client,
    embed_client: OpenAI,
    embed_model: str,
    chat_client: OpenAI,
    chat_model: str,
    entities: list[ExtractedEntity],
    similarity_threshold: float = 0.85,
) -> dict[str, str]:
    """Resolve all extracted entities in batch.

    Returns {entity_name: entity_uuid} mapping.

    API calls: 1 embedding batch + 0-1 LLM merge confirmation.
    """
    if not entities:
        return {}

    # 1. Batch embed all entities (1 API call instead of N)
    entity_texts = [
        f"{e.name}: {e.description}" if e.description else e.name
        for e in entities
    ]
    embeddings = embed_texts(embed_client, embed_model, entity_texts)

    # 2. Search for candidates per entity (DB calls only, dedicated entity RPC)
    candidates: list[tuple[ExtractedEntity, list[float], dict | None]] = []
    for entity, embedding in zip(entities, embeddings):
        top_match = _search_entity_candidates(db, embedding, similarity_threshold)
        candidates.append((entity, embedding, top_match))

    # 3. Batch LLM merge confirmation (0-1 LLM call total)
    merge_pairs = [
        (entity, match)
        for entity, _emb, match in candidates
        if match is not None
    ]

    merge_decisions: dict[str, bool] = {}
    if merge_pairs:
        merge_decisions = _batch_llm_confirm_merges(
            chat_client, chat_model, merge_pairs
        )

    # 4. Apply decisions: merge or create
    entity_name_to_id: dict[str, str] = {}
    for entity, embedding, top_match in candidates:
        if top_match is not None and merge_decisions.get(entity.name, False):
            # Merge into existing entity
            merge_entity(
                db,
                existing_id=top_match["id"],
                new_alias=entity.name,
                new_description=entity.description,
                new_embedding=embedding,
            )
            entity_name_to_id[entity.name] = top_match["id"]
        else:
            # Create new entity
            result = upsert_entity(
                db,
                name=entity.name,
                entity_type=entity.entity_type,
                description=entity.description,
                embedding=embedding,
            )
            entity_name_to_id[entity.name] = result["id"]

    return entity_name_to_id


def _search_entity_candidates(
    db: Client,
    embedding: list[float],
    threshold: float,
) -> dict | None:
    """Search for similar entities using dedicated entity-only RPC.

    Returns the top match dict or None.
    """
    result = db.rpc(
        "search_similar_entities",
        {
            "query_embedding": embedding,
            "match_count": 1,
            "similarity_threshold": threshold,
        },
    ).execute()

    if result.data:
        return result.data[0]
    return None


def _batch_llm_confirm_merges(
    client: OpenAI,
    model: str,
    pairs: list[tuple[ExtractedEntity, dict]],
) -> dict[str, bool]:
    """Confirm all entity merges in a single LLM call.

    Input: list of (new_entity, existing_entity_dict) pairs.
    Returns: {entity_name: should_merge} mapping.
    """
    if not pairs:
        return {}

    # Build a single prompt with all candidate pairs
    numbered = []
    for i, (new_entity, existing) in enumerate(pairs, 1):
        aliases = ", ".join(existing.get("aliases") or []) or "(none)"
        numbered.append(
            f"{i}. NEW: \"{new_entity.name}\" ({new_entity.entity_type}) — {new_entity.description or '(no desc)'}\n"
            f"   EXISTING: \"{existing.get('name', '')}\" ({existing.get('entity_type', '')}) — {existing.get('description', '') or '(no desc)'} — aliases: {aliases}"
        )

    prompt = (
        "For each pair below, decide if the NEW entity is the same thing as the EXISTING entity.\n"
        "Consider name similarity, type, and description. Answer with a JSON array of booleans, "
        "one per pair, in order.\n\n"
        + "\n".join(numbered)
        + '\n\nRespond with ONLY a JSON array, e.g. [true, false, true]\nNo other text.'
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=200,
        )

        raw = response.choices[0].message.content or "[]"
        match = None
        # Try to extract a JSON array
        import re
        m = re.search(r'\[[\w\s,]+\]', raw)
        if m:
            match = m.group(0)

        if match:
            decisions = json.loads(match)
            if isinstance(decisions, list) and len(decisions) == len(pairs):
                return {
                    pair[0].name: bool(d)
                    for pair, d in zip(pairs, decisions)
                }

        # Fallback: couldn't parse — default to no merge (conservative)
        return {pair[0].name: False for pair in pairs}

    except Exception:
        # On any error, default to no merge (conservative)
        return {pair[0].name: False for pair in pairs}


# Keep the old single-entity resolve for backward compatibility
def resolve_entity(
    db: Client,
    embed_client: OpenAI,
    embed_model: str,
    chat_client: OpenAI,
    chat_model: str,
    entity: ExtractedEntity,
    similarity_threshold: float = 0.85,
) -> str:
    """Resolve a single entity. Delegates to batch with size 1."""
    result = resolve_entities_batch(
        db, embed_client, embed_model,
        chat_client, chat_model,
        [entity], similarity_threshold,
    )
    return result[entity.name]
