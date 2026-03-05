# Usage Guide

You've set everything up. Now what?

This guide covers how to use 2BToRePensieve day to day — searching your knowledge, adding thoughts, managing tasks, and importing content from different sources.

---

## Talking to Your Brain

Once the MCP server is connected to your AI client (Claude Code, Cursor, ChatGPT), you can interact with your knowledge graph using natural language. The AI has access to 12 tools and will choose the right one based on what you ask.

You don't need to memorize tool names. Just ask questions naturally.

---

## Searching

**Ask your AI assistant:**

- "What do I know about [topic]?"
- "Find everything related to [person/project/concept]"
- "What decisions have I made about [topic]?"
- "Search for [keyword]"

**What happens behind the scenes:**

The `search_brain` tool converts your query into an embedding vector, finds the most similar entities and observations in the database, then uses an LLM to rerank results by actual relevance to your question. Results include the source they came from so you can trace back to the original conversation, email, video, or note.

**Filtering results:**

You can ask for specific types:
- "Find all **people** related to [topic]" — filters by entity type
- "What **decisions** have been made about [topic]?" — filters by observation type
- "What do my **YouTube videos** say about [topic]?" — the AI identifies YouTube-sourced results from the search output (source type filtering in search is WIP; semantically this still works because every result includes its source origin)

---

## Adding Thoughts

**Ask your AI assistant:**

- "Remember that I decided to use Postgres instead of MongoDB for this project"
- "Save this: met with Sarah today, agreed to delay the launch by 2 weeks"
- "Note: the API rate limit is 100 requests per minute"

**Structured formats that produce better extraction:**

| Format | Example |
|--------|---------|
| Decision | "Decided to use React over Vue because of the larger ecosystem" |
| Person note | "Sarah Chen — leads the data team. Key detail: prefers async communication" |
| Insight | "Realized the bottleneck is in the embedding step, not the LLM call" |
| Meeting debrief | "Met with the investors about Series A. Outcome: they want to see 3 months of growth. Action: prepare Q2 metrics deck" |

You can also just paste raw text, URLs, or notes. The system extracts entities, relations, and observations automatically.

---

## Managing Tasks

### Adding tasks

- "Add a task: set up Notion daily sync"
- "Create a task: review Q2 roadmap, priority 2, project Open Brain"
- "Task: buy groceries" (the AI will use `add_task`)

### Viewing tasks

- "What are my tasks?"
- "Show my tasks for the Open Brain project"
- "What's in my inbox?"

### Updating tasks

- "Change the due date on [task] to March 15"
- "Set [task] to in progress"
- "Move [task] to next"

### Completing tasks

- "Mark [task] as done"
- "Complete the Notion sync task"

### Task statuses

| Status | Meaning |
|--------|---------|
| `inbox` | Captured, not yet triaged |
| `next` | Committed to doing soon |
| `waiting` | Blocked on something external |
| `someday` | Want to do eventually, not now |
| `done` | Completed |

### Task priorities

| Priority | Meaning |
|----------|---------|
| 0 | None (default) |
| 1 | Low |
| 2 | Medium |
| 3 | High |
| 4 | Urgent |

---

## Exploring Entities

**Ask your AI assistant:**

- "Tell me about [person/project/concept]" — uses `get_entity` to pull full context
- "What's connected to [entity]?" — uses `explore_neighborhood` to traverse relations
- "List all the people in my knowledge graph"
- "What are my most recent projects?"

### Entity types

The system automatically categorizes extracted entities. Common types include: person, project, concept, tool, organization, product, place, decision, event, document, biomarker, hormone, and many others.

---

## Checking Stats

- "How much is in my brain?"
- "Show brain stats"
- "What are my most connected entities?"

This returns total counts (sources, entities, relations, observations), breakdowns by type, and the most connected entities in your graph.

---

## Importing Content

### One-time imports (run locally)

These commands ingest historical data. They use content-hash deduplication — safe to re-run. Already-ingested content is skipped automatically.

#### ChatGPT conversations

1. In ChatGPT: **Settings > Data controls > Export data**
2. Wait for the email, download the ZIP, extract it
3. Run:

```bash
python -m open_brain.connectors.chatgpt_conversations \
  --in path/to/conversations.json
```

Use `--limit 50` to process in batches, or omit to process everything at once.

#### Claude conversations

1. In Claude: **Settings > Export data**
2. Download the ZIP, extract `conversations.json`
3. Run:

```bash
python -m open_brain.connectors.claude_conversations \
  --in path/to/conversations.json
```

#### Local files (markdown, text, PDFs)

```bash
python -m open_brain.connectors.local_sync \
  --watch-dir ~/Documents/notes
```

Supports `.md`, `.txt`, `.rst`, `.org`, and `.pdf` files. Scans recursively.

To include conversation exports in the same scan:

```bash
python -m open_brain.connectors.local_sync \
  --watch-dir ~/Documents/exports \
  --include-conversations
```

Use `--dry-run` to preview what would be processed without ingesting.

#### YouTube (single video)

```bash
python -m open_brain.connectors.youtube \
  https://www.youtube.com/watch?v=VIDEO_ID
```

#### WhatsApp chat export

1. In WhatsApp: open chat > **More > Export chat > Without media**
2. Run:

```bash
python -m open_brain.connectors.whatsapp_export \
  path/to/WhatsApp_Chat.txt \
  --group-size 20
```

### Ongoing sync (automated)

These run daily via GitHub Actions. No manual intervention needed once configured.

| Channel | What it does | Config needed |
|---------|-------------|---------------|
| **Notion** | Syncs pages from a database, 50 pages per run, incremental | `NOTION_API_TOKEN`, `NOTION_DATABASE_ID` |
| **YouTube** | Syncs videos from a playlist, 10 per run, extracts transcripts | `YOUTUBE_PLAYLIST_URL` |

### Real-time capture (always on)

These run as Supabase Edge Functions. Content is ingested as it arrives.

| Channel | How to use |
|---------|-----------|
| **Telegram** | Message your bot — text is captured and searchable. Use `/task` to add tasks, `/tasks` to list them. |
| **Email** | Forward emails to your Resend inbound address. PDFs are extracted automatically. |
| **Slack** | Invite the bot to a channel. All messages are captured. |

---

## Tips

- **Re-running is safe.** All connectors use content-hash deduplication. If you run the same import twice, duplicates are skipped.
- **Start with search.** After importing, try searching for something you know is in your data. This confirms the pipeline worked.
- **Check stats after import.** Ask "show brain stats" to see updated counts.
- **Thoughts are flexible.** You can capture anything — a decision, a link, a quote, a half-formed idea. The system extracts structure from unstructured text.
- **Tasks link to knowledge.** Tasks created through the system are embedded in the knowledge graph and searchable alongside everything else.
- **Sources are traceable.** Every entity and observation links back to where it came from. Ask "where did this come from?" and the AI can show the source.

---

## Common Questions

**How long does ingestion take?**

Each chunk requires 2-3 LLM calls and 2 embedding calls. A single conversation or page takes a few seconds. A full ChatGPT export of 300+ conversations can take 30-60 minutes depending on length.

**What if ingestion fails partway through?**

Re-run the same command. Already-ingested content is skipped. It picks up where it left off.

**Can I delete something from the knowledge graph?**

Not yet via MCP tools. You can delete directly from Supabase (entities, observations, relations, sources tables) using the dashboard or SQL.

**How much does it cost to run?**

Typical monthly cost is $5-15 for moderate personal use. See the cost table in the README for per-activity estimates.

**Can I use a different LLM?**

Yes. Change `OPENROUTER_CHAT_MODEL` and `OPENROUTER_EMBED_MODEL` in your `.env` or GitHub Actions secrets. Any model available on OpenRouter works. For local models, the embedding client supports LM Studio.
