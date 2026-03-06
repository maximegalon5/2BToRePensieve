# Code Review Fixes Roadmap

**Date:** 2026-03-04
**Source:** Full code review + 16-test stress test of live MCP server

All 16 live tests passed. The following issues were identified in the code review.

## Critical (fix first)

### 1. MCP server: per-request server factory
- **File:** `supabase/functions/mcp-server/index.ts`
- **Problem:** Global `McpServer` instance gets `server.connect(transport)` called on every request. Under concurrent use, transports leak across sessions.
- **Fix:** Extract tool registration into a `createServer()` factory function. Call it per request.

### 2. Slack capture: add signing secret verification
- **File:** `supabase/functions/slack-capture/index.ts`
- **Problem:** No auth at all. Any POST is accepted and forwarded to ingest.
- **Fix:** Verify `X-Slack-Signature` using HMAC-SHA256 with `SLACK_SIGNING_SECRET`. Add secret to `.env.example` and docs.

### 3. Ingest function: add auth check
- **File:** `supabase/functions/ingest/index.ts`
- **Problem:** Public endpoint with `--no-verify-jwt`. Anyone who knows the URL can write to the knowledge graph.
- **Fix:** Check `Authorization: Bearer <SUPABASE_SERVICE_ROLE_KEY>` before processing.

## Important

### 4. Embedding API error handling
- **Files:** `ingest/index.ts` (embedBatch), `mcp-server/index.ts` (embedQuery)
- **Fix:** Check `res.ok` and `data.data` existence before accessing `.map()` / `[0].embedding`.

### 5. Docs: wrong CLI flags
- **File:** `docs/setup-channels.md`
- **Fix:** Change `--file` to `--in` for chatgpt_conversations and claude_conversations examples.

### 6. Docs: local_sync description
- **File:** `docs/setup-channels.md`
- **Fix:** Remove `--interval` flag, describe as one-shot scanner (re-runnable via cron).

### 7. Missing ijson dependency
- **File:** `requirements.txt`
- **Fix:** Add `ijson>=3.0.0`.

### 8. Unquoted NOTION_DATABASE_ID
- **File:** `.github/workflows/daily-sync.yml`
- **Fix:** Quote `"${{ secrets.NOTION_DATABASE_ID }}"`.

### 9. get_entity null handling
- **File:** `supabase/functions/mcp-server/index.ts`
- **Fix:** Check `error` from `supabase.rpc()` and handle null context.

### 10. Missing seed.sql
- **File:** `supabase/config.toml`
- **Fix:** Create empty `supabase/seed.sql` or set `enabled = false`.

### 11. Stale verification docs
- **File:** `docs/setup-supabase.md`
- **Fix:** Update curl example to match SDK-based server, or document MCP client connection as verification.

## Feature: Telegram Bot Missing Capabilities

The following MCP server tools have no Telegram handler yet. Each needs a handler function in `telegram-capture/index.ts` and a corresponding intent in the intent detection system.

### 12. Telegram: get_entity handler
- **MCP tool:** `get_entity`
- **Example:** "tell me about React", "look up Sarah"
- **Needs:** `getEntityForTelegram(nameOrId)` function, intent detection entry

### 13. Telegram: explore_neighborhood handler
- **MCP tool:** `explore_neighborhood`
- **Example:** "what's connected to React?", "show me relationships for Supabase"
- **Needs:** `exploreNeighborhoodForTelegram(entityName)` function, intent detection entry

### 14. Telegram: list_entities handler
- **MCP tool:** `list_entities`
- **Example:** "show me all my projects", "list people", "what concepts have I saved?"
- **Needs:** `listEntitiesForTelegram(entityType?)` function, intent detection entry

### 15. Telegram: list_thoughts handler
- **MCP tool:** `list_thoughts`
- **Example:** "what did I save yesterday?", "show recent telegram captures"
- **Needs:** `listThoughtsForTelegram(sourceType?, days?)` function, intent detection entry

### 16. Telegram: get_source handler
- **MCP tool:** `get_source`
- **Example:** "find the YouTube link about RAG", "what was that Notion page about deployment?"
- **Needs:** `getSourceForTelegram(search, sourceType?)` function, intent detection entry
