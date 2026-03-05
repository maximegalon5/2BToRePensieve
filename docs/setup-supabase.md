# Supabase Setup Guide

## 1. Create Project

1. Go to [supabase.com](https://supabase.com) and create a free account
2. Click **New Project**
3. Choose a name, set a database password, select a region close to you
4. Wait for the project to provision (~2 minutes)

## 2. Get Your Credentials

From your project's **Settings > API**:

- **Project URL** — `https://your-project.supabase.co`
- **anon (public) key** — for client-side access (not used by the pipeline)
- **service_role key** — for server-side access (used by everything)

From **Settings > Database > Connection String**:

- **Direct connection** — for pg_dump backups
- **Connection pooler** (Transaction mode) — for Python connectors

## 3. Run Migrations

Go to **SQL Editor** in your Supabase dashboard. Run each migration file in order:

### Migration 1: Core Schema
Paste and run `supabase/migrations/001_create_knowledge_graph.sql`

This creates:
- `sources` table — raw ingested content
- `entities` table — people, concepts, projects (with vector embeddings)
- `relations` table — directed edges between entities
- `observations` table — facts and insights (with vector embeddings)
- `search_knowledge` RPC — unified semantic search
- `get_entity_context` RPC — entity detail with full context

### Migration 2: Stats Functions
Paste and run `supabase/migrations/002_add_stats_functions.sql`

Adds `get_top_connected_entities` for analytics.

### Migration 3: Dedup Constraints
Paste and run `supabase/migrations/003_add_dedup_constraints.sql`

Adds:
- `content_hash` column on observations
- Unique index on relation edges `(source, target, type)`
- Case-insensitive unique index on entity `(name, type)`

### Migration 4: Tasks + Sync State
Paste and run `supabase/migrations/004_add_tasks_and_sync.sql`

Adds:
- `tasks` table — GTD task management with embeddings
- `sync_state` table — incremental sync cursor tracking
- Updated `search_knowledge` to include tasks
- Updated `get_entity_context` to include tasks

### Migration 5: Entity Search RPC
Paste and run `supabase/migrations/005_add_search_similar_entities.sql`

Adds `search_similar_entities` — fast entity-only similarity search used during ingestion.

## 4. Deploy Edge Functions

Install the Supabase CLI:

```bash
npm install -g supabase
```

Link your project:

```bash
supabase link --project-ref your-project-ref
```

Set the required secrets:

```bash
supabase secrets set \
  OPENROUTER_API_KEY=sk-or-v1-your-key \
  OPEN_BRAIN_ACCESS_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
```

Deploy all functions:

```bash
supabase functions deploy ingest --no-verify-jwt
supabase functions deploy mcp-server --no-verify-jwt
```

Deploy optional channel functions (only if you're using them):

```bash
supabase functions deploy telegram-capture --no-verify-jwt
supabase functions deploy email-capture --no-verify-jwt
supabase functions deploy slack-capture --no-verify-jwt
```

> **Note:** `--no-verify-jwt` is needed because the MCP server uses its own access key authentication, and webhooks (Telegram, Email, Slack) handle their own verification.

## 5. Verify

Test the MCP server:

```bash
curl -X POST https://your-project.supabase.co/functions/v1/mcp-server \
  -H "Authorization: Bearer YOUR_ACCESS_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

You should see a JSON response listing all 12 tools.
