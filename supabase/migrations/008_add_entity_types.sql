-- Migration 008: Add technology, event, decision entity types
-- Expands from 6 to 9 canonical types to reduce constraint violations
-- during LLM extraction.

-- Step 1: Drop old constraint
ALTER TABLE entities DROP CONSTRAINT IF EXISTS entities_type_check;

-- Step 2: Re-classify existing entities that fit the new types better
-- (These were previously lumped into 'concept' or 'tool' by migration 005)

UPDATE entities SET entity_type = 'technology'
WHERE entity_type = 'concept'
  AND lower(name) ~ '(^python$|^javascript$|^typescript$|^rust$|^go$|^react$|^vue$|^angular$|^docker$|^kubernetes$|^terraform$|^graphql$|^postgres|^redis$|^kafka$|^langchain$|^pytorch$|^tensorflow$|pgvector|supabase|openai|gpt-|claude|llama|whisper|ollama|qdrant|chroma|pinecone|weaviate|neo4j)';

UPDATE entities SET entity_type = 'event'
WHERE entity_type = 'concept'
  AND (lower(name) LIKE '%conference%'
    OR lower(name) LIKE '%summit%'
    OR lower(name) LIKE '%gtc %'
    OR lower(name) LIKE '%keynote%'
    OR lower(name) LIKE '%launch%'
    OR lower(name) LIKE '%announcement%');

UPDATE entities SET entity_type = 'decision'
WHERE entity_type = 'concept'
  AND (lower(name) LIKE '%decision%'
    OR lower(name) LIKE '%chose %'
    OR lower(name) LIKE '%switch to%'
    OR lower(name) LIKE '%migrate to%');

-- Step 3: Add expanded constraint
ALTER TABLE entities ADD CONSTRAINT entities_type_check
  CHECK (entity_type IN (
    'person', 'organization', 'project', 'concept',
    'tool', 'content', 'technology', 'event', 'decision'
  ));
