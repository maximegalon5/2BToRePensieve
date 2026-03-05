-- Migration 003: Comprehensive deduplication constraints
-- Adds unique constraints and content hashing for observations and relations

-- ─── OBSERVATIONS: content_hash column + unique index ───
-- Prevents storing identical observation text multiple times
alter table observations add column if not exists content_hash text;

-- Backfill existing observations with SHA-256 hashes
-- (Uses encode + digest from pgcrypto; if not available, we'll handle in app layer)
create extension if not exists pgcrypto;

update observations
set content_hash = encode(digest(content, 'sha256'), 'hex')
where content_hash is null;

-- Unique index on content_hash (allows nulls for legacy rows if backfill fails)
create unique index if not exists idx_observations_content_hash
    on observations (content_hash)
    where content_hash is not null;

-- ─── RELATIONS: unique constraint on (source_entity, target_entity, relation_type) ───
-- Prevents duplicate edges in the knowledge graph

-- First, remove any existing duplicates (keep the earliest)
delete from relations r1
using relations r2
where r1.id > r2.id
  and r1.source_entity = r2.source_entity
  and r1.target_entity = r2.target_entity
  and r1.relation_type = r2.relation_type;

create unique index if not exists idx_relations_unique_edge
    on relations (source_entity, target_entity, relation_type);

-- ─── ENTITIES: case-insensitive name + type uniqueness ───
-- Prevents creating "OpenAI" and "openai" as separate entities
-- Only applies within the same entity_type to allow "Python (tool)" vs "Python (concept)"

-- First, merge duplicates: keep the one with more relations
-- (handled in app layer during migration, just add the constraint)
create unique index if not exists idx_entities_name_type_unique
    on entities (lower(name), entity_type);
