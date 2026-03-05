-- Open Brain: Tasks + Sync State
-- Migration 004: GTD-style task system + connector sync tracking

-- ─── TASKS ───
create table if not exists tasks (
    id uuid primary key default gen_random_uuid(),
    title text not null,
    description text default '',
    status text not null default 'inbox'
        check (status in ('inbox', 'next', 'waiting', 'someday', 'done')),
    priority int not null default 0
        check (priority between 0 and 4),
    category text not null default 'personal'
        check (category in ('personal', 'professional')),
    due_date date,
    context text default '',
    project text default '',
    entity_ids uuid[] default '{}',
    embedding vector(1536),
    source_id uuid references sources(id) on delete set null,
    completed_at timestamptz,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create index if not exists idx_tasks_status on tasks (status);
create index if not exists idx_tasks_category on tasks (category);
create index if not exists idx_tasks_priority on tasks (priority);
create index if not exists idx_tasks_due_date on tasks (due_date);
create index if not exists idx_tasks_entity_ids on tasks using gin (entity_ids);
create index if not exists idx_tasks_embedding on tasks
    using hnsw (embedding vector_cosine_ops);

create trigger tasks_updated_at
    before update on tasks
    for each row execute function update_updated_at();

-- ─── SYNC STATE (for connector daily sync) ───
create table if not exists sync_state (
    id text primary key,
    last_synced_at timestamptz not null default now(),
    cursor_data jsonb default '{}'::jsonb,
    metadata jsonb default '{}'::jsonb,
    updated_at timestamptz default now()
);

create trigger sync_state_updated_at
    before update on sync_state
    for each row execute function update_updated_at();

-- ─── UPDATE search_knowledge to include tasks ───
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

    union all

    select
        'task'::text as result_type,
        t.id as result_id,
        t.title as name,
        coalesce(t.description, '') as content,
        t.category as entity_type,
        t.status as observation_type,
        1 - (t.embedding <=> query_embedding) as similarity,
        jsonb_build_object(
            'priority', t.priority,
            'due_date', t.due_date,
            'project', t.project,
            'context', t.context,
            'completed_at', t.completed_at
        ) as metadata,
        t.entity_ids,
        t.source_id
    from tasks t
    where t.embedding is not null
        and t.status != 'done'

    order by similarity desc
    limit match_count;
end;
$$;

-- ─── UPDATE get_entity_context to include tasks ───
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
            ) order by o.created_at desc)
            from (
                select *
                from observations
                where target_entity_id = any(entity_ids)
                order by created_at desc
                limit 50
            ) o
        ), '[]'::jsonb)
    );

    -- Include open tasks linked to this entity
    result := result || jsonb_build_object(
        'tasks',
        coalesce((
            select jsonb_agg(jsonb_build_object(
                'id', t.id,
                'title', t.title,
                'status', t.status,
                'priority', t.priority,
                'category', t.category,
                'due_date', t.due_date,
                'project', t.project,
                'created_at', t.created_at
            ) order by t.priority desc, t.created_at desc)
            from tasks t
            where target_entity_id = any(t.entity_ids)
            and t.status != 'done'
        ), '[]'::jsonb)
    );

    return result;
end;
$$;
