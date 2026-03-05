# Architecture

## Overview

2BToRePensieve is a personal knowledge graph that runs on Supabase (Postgres + pgvector) with LLM-powered extraction via OpenRouter. It's designed to be accessed from any AI assistant via the MCP protocol.

## Components

### 1. Ingestion Pipeline (`open_brain/ingest.py`)

Every piece of content goes through the same pipeline regardless of source:

```
Content → Dedup Check → Store Source → LLM Extraction → Entity Resolution → Store Graph
```

**Per-chunk cost breakdown:**
- 1 LLM call: Knowledge extraction (entities, relations, observations)
- 1 embedding call: Batch embed all entities
- 0-1 LLM calls: Batch merge confirmation (only if similar entities found)
- 1 embedding call: Batch embed all observations
- ~13 DB queries: Dedup checks, similarity searches, inserts

### 2. Knowledge Extraction (`open_brain/extraction/`)

The extractor uses a structured prompt to extract:
- **Entities** — people, concepts, projects, tools, decisions, events, places, organizations
- **Relations** — directed connections between entities (e.g., "works_on", "mentioned_in")
- **Observations** — specific facts, decisions, insights linked to entities

### 3. Entity Resolution (`open_brain/extraction/entity_resolver.py`)

Prevents duplicate entities by:
1. Batch embedding all extracted entities (1 API call)
2. Searching for similar existing entities via `search_similar_entities` RPC (DB only)
3. Asking the LLM to confirm/reject merges in a single batch call (0-1 API call)

### 4. Dedup System (5 layers)

| Layer | What | How |
|-------|------|-----|
| Source dedup | Prevents re-ingesting same content | SHA-256 content hash |
| Entity dedup | Prevents "Python" vs "python" | Case-insensitive unique index |
| Entity merge | Prevents "ML" vs "Machine Learning" | Embedding similarity + LLM confirmation |
| Relation dedup | Prevents duplicate edges | Unique index on (source, target, type) |
| Observation dedup | Prevents duplicate facts | Content hash + 0.95 semantic similarity |

### 5. MCP Server (`supabase/functions/mcp-server/`)

A Supabase Edge Function implementing the MCP protocol over HTTP. Features:
- 12 tools for search, capture, task management, and exploration
- LLM reranking: over-fetches 3x candidates, scores relevance 0-10, returns top N
- Dual auth: Bearer token (Claude/Cursor) + query param (ChatGPT)

### 6. Edge Functions (`supabase/functions/`)

| Function | Purpose |
|----------|---------|
| `ingest` | Universal ingestion — receives content, runs extraction pipeline |
| `mcp-server` | MCP protocol server with 12 tools |
| `telegram-capture` | Telegram bot webhook — captures, searches, replies |
| `email-capture` | Resend inbound email webhook — captures emails + PDF attachments |
| `slack-capture` | Slack event webhook — captures channel messages |

### 7. Python Connectors (`open_brain/connectors/`)

CLI tools that fetch content from various sources and feed it through the ingestion pipeline:

| Connector | Input | Features |
|-----------|-------|----------|
| `chatgpt_conversations.py` | JSON export | Conversation parsing |
| `claude_conversations.py` | JSON export | Conversation parsing |
| `notion_database.py` | Notion API | Incremental sync with cursor |
| `youtube.py` | Playlist URL | Transcript extraction, PDF from description |
| `local_bulk.py` | Directory path | Batch file ingestion |
| `local_sync.py` | Directory path | Continuous folder watch |
| `whatsapp_export.py` | Chat export .txt | Message grouping |
| `pdf_ingest.py` | PDF files | Text extraction + chunking |

### 8. Daily Sync (`.github/workflows/daily-sync.yml`)

GitHub Actions cron job running at 6 AM UTC:
- **Notion sync**: 50 pages per run, incremental (tracks last sync cursor)
- **YouTube sync**: 10 videos per run, incremental (tracks processed video IDs)

## Data Flow

```
Source Content
     │
     ▼
[Content Hash Dedup] ──duplicate──> Skip
     │
     ▼
[Store in sources table]
     │
     ▼
[LLM: Extract entities, relations, observations]
     │
     ├──> [Batch embed entities]
     │         │
     │         ▼
     │    [Search similar entities (DB)]
     │         │
     │         ▼
     │    [LLM: Batch confirm merges]
     │         │
     │         ├──merge──> [Update existing entity aliases]
     │         └──new────> [Insert new entity]
     │
     ├──> [Dedup + insert relations]
     │
     └──> [Batch embed observations]
               │
               ▼
          [Hash + semantic dedup]
               │
               ▼
          [Insert observations]
```

## Tech Stack

| Component | Technology |
|-----------|------------|
| Database | Supabase (PostgreSQL 17) |
| Vector search | pgvector with HNSW indexes |
| LLM | OpenRouter (gpt-4o-mini default) |
| Embeddings | OpenRouter (text-embedding-3-small default) |
| Edge Functions | Deno (Supabase Edge Runtime) |
| Python | 3.13+ with supabase, openai, httpx |
| CI/CD | GitHub Actions |
| Protocol | MCP (Model Context Protocol) |
