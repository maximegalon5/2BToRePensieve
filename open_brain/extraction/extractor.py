"""Extract entities, relations, and observations from source content using an LLM."""
from __future__ import annotations

import json
from dataclasses import dataclass

from openai import OpenAI

from open_brain.extraction.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_USER_TEMPLATE,
)


@dataclass
class ExtractedEntity:
    name: str
    entity_type: str
    description: str


@dataclass
class ExtractedRelation:
    source: str
    target: str
    relation_type: str
    description: str


@dataclass
class ExtractedObservation:
    content: str
    observation_type: str
    entities: list[str]


@dataclass
class ExtractionResult:
    entities: list[ExtractedEntity]
    relations: list[ExtractedRelation]
    observations: list[ExtractedObservation]


def extract_knowledge(
    client: OpenAI,
    model: str,
    content: str,
    source_type: str,
    title: str = "",
    max_content_chars: int = 12000,
) -> ExtractionResult:
    """Extract structured knowledge from source content via LLM."""
    truncated = content[:max_content_chars]

    user_msg = EXTRACTION_USER_TEMPLATE.format(
        source_type=source_type,
        title=title or "(untitled)",
        content=truncated,
    )

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.1,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content or "{}"
    return _parse_extraction(raw)


def _parse_extraction(raw_json: str) -> ExtractionResult:
    """Parse LLM JSON output into typed dataclasses. Lenient on malformed input."""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return ExtractionResult(entities=[], relations=[], observations=[])

    entities = []
    for e in data.get("entities", []):
        if isinstance(e, dict) and "name" in e:
            entities.append(ExtractedEntity(
                name=e["name"],
                entity_type=e.get("type", "concept"),
                description=e.get("description", ""),
            ))

    relations = []
    for r in data.get("relations", []):
        if isinstance(r, dict) and "source" in r and "target" in r:
            relations.append(ExtractedRelation(
                source=r["source"],
                target=r["target"],
                relation_type=r.get("type", "related_to"),
                description=r.get("description", ""),
            ))

    observations = []
    for o in data.get("observations", []):
        if isinstance(o, dict) and "content" in o:
            observations.append(ExtractedObservation(
                content=o["content"],
                observation_type=o.get("type", "fact"),
                entities=o.get("entities", []),
            ))

    return ExtractionResult(
        entities=entities,
        relations=relations,
        observations=observations,
    )
