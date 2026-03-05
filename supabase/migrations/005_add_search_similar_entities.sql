-- Migration 005: Dedicated entity similarity search
-- This RPC searches ONLY the entities table (not observations or tasks),
-- making it ~3x faster for entity resolution during ingestion.

CREATE OR REPLACE FUNCTION search_similar_entities(
  query_embedding vector(1536),
  match_count int DEFAULT 5,
  similarity_threshold float DEFAULT 0.85
)
RETURNS TABLE (
  id uuid,
  name text,
  entity_type text,
  description text,
  aliases text[],
  similarity float
)
LANGUAGE sql STABLE
AS $$
  SELECT
    e.id,
    e.name,
    e.entity_type,
    e.description,
    e.aliases,
    (1 - (e.embedding <=> query_embedding))::float AS similarity
  FROM entities e
  WHERE e.embedding IS NOT NULL
    AND (1 - (e.embedding <=> query_embedding)) >= similarity_threshold
  ORDER BY e.embedding <=> query_embedding
  LIMIT match_count;
$$;
