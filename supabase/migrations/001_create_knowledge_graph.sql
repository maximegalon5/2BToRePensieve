-- Open Brain: Knowledge Graph Schema
-- Run this in Supabase SQL Editor or via `supabase db push`

-- Enable pgvector extension
create extension if not exists vector;

-- ─── SOURCES ───
create table if not exists sources (
    id uuid primary key default gen_random_uuid(),
    source_type text not null,
    origin text not null,
    title text,
    raw_content text not null,
    content_hash text not null,
    status text not null default 'pending',
    metadata jsonb default '{}'::jsonb,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create unique index if not exists idx_sources_content_hash on sources (content_hash);
create index if not exists idx_sources_source_type on sources (source_type);
create index if not exists idx_sources_status on sources (status);
create index if not exists idx_sources_created_at on sources (created_at);

-- ─── ENTITIES ───
create table if not exists entities (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    entity_type text not null,
    description text not null default '',
    embedding vector(1536),
    aliases text[] default '{}',
    metadata jsonb default '{}'::jsonb,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create index if not exists idx_entities_embedding on entities
    using hnsw (embedding vector_cosine_ops);
create index if not exists idx_entities_type on entities (entity_type);
create index if not exists idx_entities_aliases on entities using gin (aliases);
create index if not exists idx_entities_metadata on entities using gin (metadata);
create index if not exists idx_entities_name on entities (name);

-- ─── RELATIONS ───
create table if not exists relations (
    id uuid primary key default gen_random_uuid(),
    source_entity uuid not null references entities(id) on delete cascade,
    target_entity uuid not null references entities(id) on delete cascade,
    relation_type text not null,
    description text not null default '',
    weight float default 1.0,
    metadata jsonb default '{}'::jsonb,
    source_id uuid references sources(id) on delete set null,
    created_at timestamptz default now()
);

create index if not exists idx_relations_source_entity on relations (source_entity);
create index if not exists idx_relations_target_entity on relations (target_entity);
create index if not exists idx_relations_type on relations (relation_type);
create index if not exists idx_relations_source_id on relations (source_id);

-- ─── OBSERVATIONS ───
create table if not exists observations (
    id uuid primary key default gen_random_uuid(),
    content text not null,
    embedding vector(1536),
    observation_type text not null,
    entity_ids uuid[] default '{}',
    source_id uuid references sources(id) on delete set null,
    metadata jsonb default '{}'::jsonb,
    created_at timestamptz default now()
);

create index if not exists idx_observations_embedding on observations
    using hnsw (embedding vector_cosine_ops);
create index if not exists idx_observations_type on observations (observation_type);
create index if not exists idx_observations_entity_ids on observations using gin (entity_ids);
create index if not exists idx_observations_source_id on observations (source_id);
create index if not exists idx_observations_metadata on observations using gin (metadata);

-- ─── RPC: search_knowledge ───
create or replace function search_knowledge(
    query_embedding vector(1536),
    match_count int default 20,
    filter_entity_type text default null,
    filter_observation_type text default null
)
returns table (
    result_type text,
    result_id uuid,
    name text,
    content text,
    entity_type text,
    observation_type text,
    similarity float,
    metadata jsonb,
    entity_ids uuid[],
    source_id uuid
)
language plpgsql
as $$
begin
    return query
    select
        'observation'::text as result_type,
        o.id as result_id,
        null::text as name,
        o.content,
        null::text as entity_type,
        o.observation_type,
        1 - (o.embedding <=> query_embedding) as similarity,
        o.metadata,
        o.entity_ids,
        o.source_id
    from observations o
    where o.embedding is not null
        and (filter_observation_type is null or o.observation_type = filter_observation_type)

    union all

    select
        'entity'::text as result_type,
        e.id as result_id,
        e.name,
        e.description as content,
        e.entity_type,
        null::text as observation_type,
        1 - (e.embedding <=> query_embedding) as similarity,
        e.metadata,
        null::uuid[] as entity_ids,
        null::uuid as source_id
    from entities e
    where e.embedding is not null
        and (filter_entity_type is null or e.entity_type = filter_entity_type)

    order by similarity desc
    limit match_count;
end;
$$;

-- ─── RPC: get_entity_context ───
create or replace function get_entity_context(
    target_entity_id uuid,
    depth int default 1
)
returns jsonb
language plpgsql
as $$
declare
    result jsonb;
begin
    select to_jsonb(e.*) into result
    from entities e
    where e.id = target_entity_id;

    if result is null then
        return null;
    end if;

    result := result || jsonb_build_object(
        'outgoing_relations',
        coalesce((
            select jsonb_agg(jsonb_build_object(
                'relation_type', r.relation_type,
                'description', r.description,
                'weight', r.weight,
                'target', jsonb_build_object('id', e.id, 'name', e.name, 'type', e.entity_type)
            ))
            from relations r
            join entities e on e.id = r.target_entity
            where r.source_entity = target_entity_id
        ), '[]'::jsonb)
    );

    result := result || jsonb_build_object(
        'incoming_relations',
        coalesce((
            select jsonb_agg(jsonb_build_object(
                'relation_type', r.relation_type,
                'description', r.description,
                'weight', r.weight,
                'source', jsonb_build_object('id', e.id, 'name', e.name, 'type', e.entity_type)
            ))
            from relations r
            join entities e on e.id = r.source_entity
            where r.target_entity = target_entity_id
        ), '[]'::jsonb)
    );

    result := result || jsonb_build_object(
        'observations',
        coalesce((
            select jsonb_agg(jsonb_build_object(
                'id', o.id,
                'content', o.content,
                'type', o.observation_type,
                'metadata', o.metadata,
                'created_at', o.created_at
            ))
            from observations o
            where target_entity_id = any(o.entity_ids)
            order by o.created_at desc
            limit 50
        ), '[]'::jsonb)
    );

    return result;
end;
$$;

-- ─── Updated_at trigger ───
create or replace function update_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

create trigger sources_updated_at
    before update on sources
    for each row execute function update_updated_at();

create trigger entities_updated_at
    before update on entities
    for each row execute function update_updated_at();
