# 2BToRePensieve

> **Status (2026-03-18):** Active development. See [Release Notes](#release-notes) for the latest changes.

**A cloud-hosted personal knowledge graph you can talk to from any AI assistant.**

> *Second Brain + Total Recall + Pensieve* — capture everything, forget nothing, recall instantly.

2BToRePensieve builds a persistent knowledge graph from your notes, conversations, emails, YouTube videos, and Notion pages. It extracts entities, relationships, and observations automatically, then makes everything searchable via semantic search with LLM reranking — accessible from ChatGPT, Claude, Cursor, or any MCP-compatible client.

---

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│                        INPUT CHANNELS                           │
│  ChatGPT  Claude  Notion  YouTube  Telegram  Email  Local Files │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                    ┌──────▼──────┐
                    │   Ingest    │  LLM extraction + embedding
                    │  Pipeline   │  (2-3 LLM calls per chunk)
                    └──────┬──────┘
                           │
              ┌────────────▼────────────────┐
              │      Supabase + pgvector    │
              │  ┌────────┐  ┌───────────┐  │
              │  │Entities│──│ Relations │  │
              │  └────┬───┘  └───────────┘  │
              │  ┌────▼──────┐ ┌─────────┐  │
              │  │Observations│ │  Tasks  │  │
              │  └────────────┘ └─────────┘  │
              └────────────┬────────────────┘
                           │
                    ┌──────▼──────┐
                    │ MCP Server  │  12 tools, LLM reranking
                    └──────┬──────┘
                           │
              ┌────────────▼────────────────┐
              │       ACCESS POINTS         │
              │  ChatGPT  Claude  Cursor    │
              │  Telegram Bot  Any MCP app  │
              └─────────────────────────────┘
```

## Features

- **Knowledge Graph** — Entities, relations, and observations extracted automatically from any text
- **Semantic Search** — pgvector cosine similarity + LLM reranking for high-relevance results
- **12 MCP Tools** — search, add thoughts, manage tasks, explore entities, view stats
- **GTD Task System** — inbox/next/waiting/someday/done with priorities and projects
- **7 Input Channels** — ChatGPT, Claude, Notion, YouTube, Telegram, Email, local files
- **5-Layer Dedup** — Content hash, semantic similarity, entity name+type, relation edges, observation hash
- **Daily Sync** — GitHub Actions for Notion, local Task Scheduler for YouTube (cloud IPs blocked by YouTube)
- **Batched Pipeline** — 2-3 LLM calls + 2 embedding calls per chunk (not per entity)

## Quick Start

### 1. Set up Supabase

Create a [Supabase](https://supabase.com) project (free tier works). Run the migrations in order:

```bash
# In Supabase SQL Editor, run each file in supabase/migrations/:
# 001_create_knowledge_graph.sql
# 002_add_stats_functions.sql
# 003_add_dedup_constraints.sql
# 004_add_tasks_and_sync.sql
# 005_add_search_similar_entities.sql
```

### 2. Set up OpenRouter

Create an [OpenRouter](https://openrouter.ai) account and add credits. Get your API key.

Default models (configurable):
- **Chat/Extraction:** `openai/gpt-4o-mini` (~$0.15/1M input tokens)
- **Embeddings:** `openai/text-embedding-3-small` (~$0.02/1M tokens)

### 3. Deploy Edge Functions

```bash
# Install Supabase CLI
npm i -g supabase

# Link your project
supabase link --project-ref your-project-ref

# Set secrets
supabase secrets set \
  OPENROUTER_API_KEY=sk-or-v1-your-key \
  OPEN_BRAIN_ACCESS_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")

# Deploy all functions
supabase functions deploy ingest --no-verify-jwt
supabase functions deploy mcp-server --no-verify-jwt
supabase functions deploy telegram-capture --no-verify-jwt
supabase functions deploy email-capture --no-verify-jwt
supabase functions deploy slack-capture --no-verify-jwt
```

### 4. Connect Your AI Client

#### Claude Code / Cursor

Add to your MCP config (`.claude/mcp.json` or `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "open-brain": {
      "type": "url",
      "url": "https://your-project.supabase.co/functions/v1/mcp-server",
      "headers": {
        "Authorization": "Bearer YOUR_ACCESS_KEY"
      }
    }
  }
}
```

#### ChatGPT

Use a ChatGPT MCP connector plugin. Set the server URL to:
```
https://your-project.supabase.co/functions/v1/mcp-server?key=YOUR_ACCESS_KEY
```

### 5. Install Python Dependencies

```bash
pip install supabase openai httpx python-dotenv yt-dlp youtube-transcript-api PyMuPDF
```

### 6. Configure Environment

```bash
cp .env.example .env
# Edit .env with your credentials
```

## Documentation

- **[Usage Guide](docs/usage-guide.md)** — How to use the system day to day: searching, adding thoughts, managing tasks, importing content
- **[Channel Setup](docs/setup-channels.md)** — How to configure each input channel (Telegram, Email, Notion, YouTube, etc.)
- **[Supabase Setup](docs/setup-supabase.md)** — Database and Edge Function setup
- **[Architecture](docs/architecture.md)** — Technical design and data flow

## MCP Tools

| Tool | Description |
|------|-------------|
| `search_brain` | Semantic search with LLM reranking |
| `get_entity` | Look up entity by name/ID with full context |
| `explore_neighborhood` | Traverse entity relations N hops deep |
| `add_thought` | Capture any content into the knowledge graph |
| `list_entities` | Browse entities by type or recency |
| `list_thoughts` | Browse recent captures with filters |
| `thought_stats` | Aggregate stats: counts, types, top entities |
| `add_task` | Create GTD task with priority/project/context |
| `list_tasks` | List tasks with status/category/project filters |
| `update_task` | Update any task field |
| `complete_task` | Mark task done |
| `get_source` | Find source content by title keyword |

## Connectors

| Connector | Type | How |
|-----------|------|-----|
| **ChatGPT** | Python CLI | Export conversations JSON, ingest via `chatgpt_conversations.py` |
| **Claude** | Python CLI | Export conversations JSON, ingest via `claude_conversations.py` |
| **Notion** | Python CLI + Cron | Syncs database pages with incremental cursor via `notion_database.py` |
| **YouTube** | Python CLI + Cron | Extracts transcripts from playlist videos via `youtube.py` |
| **Telegram** | Edge Function | Bot captures messages, searches brain, replies with context |
| **Email** | Edge Function | Resend inbound webhook captures emails + PDF attachments |
| **Slack** | Edge Function | Bot captures channel messages |
| **Local Files** | Python CLI | Bulk ingest .md/.txt files via `local_bulk.py` or watch folder via `local_sync.py` |

## Project Structure

```
2BToRePensieve/
├── open_brain/                    # Python package
│   ├── config.py                  # Environment-based configuration
│   ├── db.py                      # Supabase client + all DB operations
│   ├── embeddings.py              # Cloud (OpenRouter) + local (LM Studio) embeddings
│   ├── ingest.py                  # Core ingestion pipeline
│   ├── chunking.py                # Text chunking with sentence-boundary splitting
│   ├── extraction/
│   │   ├── extractor.py           # LLM knowledge extraction
│   │   ├── entity_resolver.py     # Batch entity resolution + merge confirmation
│   │   └── prompts.py             # LLM prompt templates
│   ├── connectors/
│   │   ├── chatgpt_conversations.py
│   │   ├── claude_conversations.py
│   │   ├── notion_database.py
│   │   ├── youtube.py
│   │   ├── local_bulk.py
│   │   ├── local_sync.py
│   │   ├── whatsapp_export.py
│   │   └── pdf_ingest.py
│   └── backup/
│       └── backup.py              # pg_dump + JSONL export
├── supabase/
│   ├── config.toml
│   ├── migrations/                # Run these in order
│   │   ├── 001_create_knowledge_graph.sql
│   │   ├── 002_add_stats_functions.sql
│   │   ├── 003_add_dedup_constraints.sql
│   │   ├── 004_add_tasks_and_sync.sql
│   │   └── 005_add_search_similar_entities.sql
│   └── functions/
│       ├── ingest/                # Universal ingestion Edge Function
│       ├── mcp-server/            # MCP protocol server (12 tools)
│       ├── telegram-capture/      # Telegram bot webhook
│       ├── email-capture/         # Resend inbound email webhook
│       └── slack-capture/         # Slack event webhook
├── scripts/
│   └── sync-youtube.ps1           # Local YouTube sync (Task Scheduler)
└── .github/
    └── workflows/
        └── daily-sync.yml         # Cron: Notion daily sync
```

## Database Schema

**6 tables**, **3 RPC functions**, **pgvector HNSW indexes**:

- `sources` — Raw ingested content with content_hash dedup
- `entities` — People, concepts, projects, tools, decisions, events (with embeddings)
- `relations` — Directed edges between entities
- `observations` — Facts, insights, decisions linked to entities (with embeddings)
- `tasks` — GTD task system with embeddings for semantic search
- `sync_state` — Cursor tracking for incremental connector sync

RPC functions:
- `search_knowledge` — Union search across entities + observations + tasks
- `get_entity_context` — Full entity context with relations, observations, tasks
- `search_similar_entities` — Fast entity-only similarity search for ingestion
- `get_top_connected_entities` — Most connected entities by relation count

## Ingestion Pipeline

Each chunk goes through this optimized pipeline:

1. **Dedup check** — SHA-256 content hash (DB only)
2. **Store source** — Insert raw content (DB only)
3. **Extract knowledge** — 1 LLM call extracts entities, relations, observations
4. **Batch embed entities** — 1 API call for all entity texts
5. **Search candidates** — DB calls to `search_similar_entities` RPC
6. **Batch merge confirmation** — 0-1 LLM call for all merge candidates
7. **Upsert entities** — Create new or merge into existing (DB only)
8. **Store relations** — Dedup by (source, target, type) edge (DB only)
9. **Batch embed observations** — 1 API call for all observation texts
10. **Dedup + store observations** — Hash + semantic dedup (DB only)

**Total: 2-3 LLM calls + 2 embedding calls per chunk.**

## Cost Estimate

With `gpt-4o-mini` + `text-embedding-3-small` via OpenRouter:

| Activity | Estimated Cost |
|----------|---------------|
| Ingest 100 pages/articles | ~$0.10-0.30 |
| Daily Notion sync (50 pages) | ~$0.05-0.15 |
| Daily YouTube sync (10 videos) | ~$0.05-0.20 |
| 100 MCP searches with reranking | ~$0.02-0.05 |
| Telegram: 50 messages/day | ~$0.03-0.08 |

**Typical monthly cost: $5-15** for moderate personal use.

## Daily Sync

### Notion (GitHub Actions)

The included workflow runs daily at 6 AM UTC:

- Syncs pages from a Notion database, 50 pages per run
- Two-phase sync: re-ingests modified pages, then backfills un-ingested pages
- Safe limit for the 6-hour GitHub Actions timeout: ~300 pages per run

Set these GitHub Actions secrets:
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
- `OPENROUTER_API_KEY`
- `NOTION_API_TOKEN`, `NOTION_DATABASE_ID`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_NOTIFY_CHAT_ID` (optional, for notifications)

### YouTube (Local Task Scheduler)

> **Why not GitHub Actions?** YouTube blocks transcript requests from cloud provider IPs (AWS, GCP, Azure). All GitHub Actions runners use cloud IPs, so every transcript fetch fails with `RequestBlocked`. See [YouTube IP Blocking](#youtube-ip-blocking) for details and alternatives.

YouTube sync runs locally via Windows Task Scheduler using your home IP:

```powershell
# Register the scheduled task (run once)
$repoRoot = "C:\path	oBToRePensieve"
$scriptPath = Join-Path $repoRoot "scripts\sync-youtube.ps1"

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`"" `
    -WorkingDirectory $repoRoot

$trigger = New-ScheduledTaskTrigger -Daily -At "6:00AM"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName "OpenBrain-YouTube-Sync" `
    -Description "Daily YouTube playlist sync for knowledge graph" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings
```

The `-WakeToRun` flag wakes the computer from sleep to run the sync, then it goes back to sleep.

**Before running:** Edit `scripts/sync-youtube.ps1` and set your playlist URL.

## Inspiration

Inspired by [Nate B. Jones' Open Brain guide](https://promptkit.natebjones.com/20260224_uq1_guide_main), which demonstrated the core idea: Supabase + OpenRouter + MCP to give every AI tool you use the same persistent memory via a single URL.

The problem is simple — your knowledge lives in too many places. Zotero, browser bookmarks, Notion, YouTube watch-later playlists, ChatGPT conversations, Claude chats, Slack threads, emails. None of them talk to each other, and none of them are accessible when you're working in a different tool.

2BToRePensieve takes the Open Brain concept and extends it from a Slack capture + 4 MCP tools into a full knowledge graph with:
- **Entity extraction and resolution** — not just storing text, but building a graph of people, concepts, projects, and their relationships
- **7 input channels** instead of just Slack — ChatGPT, Claude, Notion, YouTube, Telegram, Email, local files
- **12 MCP tools** — search, capture, entity exploration, task management, stats
- **Batched pipeline** — optimized from N LLM calls per entity down to 2-3 calls per chunk
- **5-layer dedup** — content hash, semantic similarity, entity merge, relation dedup, observation dedup
- **GTD task system** — embedded in the knowledge graph for cross-referencing
- **Daily automated sync** — GitHub Actions cron for Notion and YouTube

The name combines **Second Brain**, **Total Recall**, and **Pensieve** (Harry Potter) — one ring to rule them all.

## Extensions & Ideas

Ways to extend this that we haven't built yet:

| Extension | Description |
|-----------|-------------|
| **Browser extension** | Capture highlights, bookmarks, and full pages as you browse |
| **Voice capture** | Whisper transcription from voice memos (phone app or Telegram voice messages) |
| **Calendar integration** | Auto-ingest meeting notes from Google Calendar / Outlook |
| **RSS/newsletter** | Ingest articles from RSS feeds or email newsletters |
| **Twitter/X bookmarks** | Sync saved tweets and threads |
| **Readwise** | Import highlights from Kindle, articles, podcasts |
| **Graph visualization** | D3.js or Obsidian-style graph view of entities and relations |
| **Spaced repetition** | Surface forgotten knowledge on a schedule |
| **Conflict detection** | Flag contradictory observations across sources |
| **Multi-user** | Shared knowledge graphs with access control |
| **Self-hosted LLM** | Run extraction with Ollama/llama.cpp instead of OpenRouter |
| **Webhooks out** | Trigger actions when new entities/observations match patterns |

## YouTube IP Blocking

YouTube's transcript API blocks requests from cloud provider IPs. This affects any CI/CD runner (GitHub Actions, GitLab CI, CircleCI, etc.) because they all use cloud infrastructure.

**Symptoms:**
- `RequestBlocked` or `IpBlocked` exception from `youtube-transcript-api`
- Error: "YouTube is blocking requests from your IP"
- All transcript fetches fail, 0 videos ingested

**Solutions (pick one):**

| Approach | Pros | Cons |
|----------|------|------|
| **Local Task Scheduler** (recommended) | Simple, free, uses home IP | PC must be on/sleeping (not off) |
| **Self-hosted GitHub Actions runner** | Same workflow file, logs in GitHub UI | Must keep agent running |
| **Residential proxy** | Works from any CI/CD | Costs money, adds complexity |
| **Cookie authentication** | Quick fix from cloud | Risks account ban, cookies expire |

This project uses the **Local Task Scheduler** approach via `scripts/sync-youtube.ps1`.

## Known Issues

Issues identified during code review (2026-03-04). Fixes in progress.

### Security

| # | Severity | Component | Issue |
|---|----------|-----------|-------|
| 1 | Critical | `mcp-server` | Global `McpServer` instance reconnected per request — may leak state under concurrent sessions. Fix: create server per request via factory function. |
| 2 | Critical | `slack-capture` | No Slack signing secret verification — any POST to the endpoint is accepted. Fix: add HMAC-SHA256 signature check with `SLACK_SIGNING_SECRET`. |
| 3 | Critical | `ingest` | No authentication — the endpoint is callable by anyone if the URL is known. Fix: validate service role key in Authorization header. |
| 4 | Important | `ingest`, `mcp-server` | Embedding API errors (rate limit, bad key) crash with unguarded `.data` access. Fix: check `res.ok` and `data.data` before use. |

### Code & Config

| # | Severity | Component | Issue |
|---|----------|-----------|-------|
| 5 | ~~Important~~ | `mcp-server` | ~~`get_entity` silently returns `null` on RPC error instead of an error message.~~ **Fixed.** Root cause: SQL bug in `get_entity_context` RPC (ORDER BY outside jsonb_agg) + swallowed error in TypeScript. |
| 6 | Important | `config.toml` | References `seed.sql` that doesn't exist — `supabase db reset` will fail locally. |
| 7 | ~~Important~~ | `requirements.txt` | ~~Missing `ijson` dependency — ChatGPT connector fails on fresh install.~~ **Fixed.** |
| 8 | ~~Important~~ | `daily-sync.yml` | ~~`NOTION_DATABASE_ID` injected unquoted into shell command.~~ **Fixed.** |

### Documentation

| # | Severity | Component | Issue |
|---|----------|-----------|-------|
| 9 | ~~Important~~ | `setup-channels.md` | ~~ChatGPT/Claude connector examples use `--file` flag — actual flag is `--in`.~~ **Fixed.** |
| 10 | ~~Important~~ | `setup-channels.md` | ~~`local_sync` documented as continuous watcher with `--interval` flag — it's actually a one-shot scanner.~~ **Fixed.** |
| 11 | Important | `setup-supabase.md` | Verification curl uses old hand-rolled JSON-RPC format — stale after SDK rewrite. |

## V2.0 Roadmap

What's planned for the next major version:

- **Multimodal ingestion** — images (OCR + vision LLM descriptions), audio (Whisper transcription), screenshots, diagrams
- **Agentic workflows** — the knowledge graph reasons over itself: auto-link related observations, suggest connections, generate weekly digests
- **Temporal awareness** — "What did I know about X last month?" vs "What do I know now?" — versioned observations with time-travel queries
- **Confidence scoring** — track observation reliability: primary source vs hearsay vs LLM-generated, with confidence decay over time
- **Graph RAG** — multi-hop retrieval: "What do my colleagues think about the tools I'm considering for the project?" traverses person→opinion→tool→project
- **Mobile app** — native iOS/Android for quick capture with photo, voice, and location context
- **Federated sync** — merge knowledge graphs across devices/instances without a central server
- **Plugin system** — drop-in connector SDK so anyone can build new input channels

## Release Notes

### v0.3.0 (2026-03-18)

**Notion backfill sync fix**
- The `--sync` flag previously only queried Notion for pages modified after `last_edited_time`, which meant un-ingested pages in the backlog were never picked up. Now fetches all pages and locally filters into two groups: (a) pages modified since last sync (re-ingest) and (b) pages never ingested (backfill). Prioritizes modified pages, then fills the backlog up to `--limit`.

**YouTube sync moved to local execution**
- YouTube blocks transcript requests from cloud provider IPs (all GitHub Actions runners). Moved YouTube sync to a local Windows Task Scheduler script (`scripts/sync-youtube.ps1`) that uses your home IP.
- Added `--cookies` flag to `youtube.py` for optional cookie-based authentication.
- Fixed `UnicodeEncodeError` crash on Windows when video titles contain emoji/unicode characters.

**Other fixes**
- N+1 query fix + HNSW search optimization (20x speedup)
- MCP server concurrency fix (per-request server instances)
- Defensive error handling on all edge function API calls
- Tiered PDF extraction (unpdf v1.4 + OpenAI vision fallback)
- Telegram notification for daily sync results

### v0.2.0 (2026-03-05)

Initial public release with core knowledge graph, 7 input channels, 12 MCP tools, and daily sync via GitHub Actions.

## License

MIT
