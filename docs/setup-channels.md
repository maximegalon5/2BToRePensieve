# Channel Setup Guides

Each channel is optional — set up only what you need.

---

## MCP Server (ChatGPT, Claude, Cursor)

The MCP server is deployed as a Supabase Edge Function. It exposes 12 tools via the MCP protocol.

### Claude Code / Cursor

Add to `.claude/mcp.json` or `.cursor/mcp.json`:

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

### ChatGPT

Use a ChatGPT MCP connector. Set the server URL with the key as a query parameter:

```
https://your-project.supabase.co/functions/v1/mcp-server?key=YOUR_ACCESS_KEY
```

---

## Telegram Bot

A conversational bot that captures messages, searches your brain, and manages tasks.

### 1. Create the Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot`, follow prompts
3. Save the **bot token** (format: `1234567890:ABC-DEF...`)

### 2. Set Secrets

```bash
supabase secrets set \
  TELEGRAM_BOT_TOKEN=your-bot-token \
  TELEGRAM_WEBHOOK_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))") \
  TELEGRAM_ALLOWED_USERS=your-telegram-user-id
```

> Find your user ID: message [@userinfobot](https://t.me/userinfobot) on Telegram.

### 3. Deploy & Register Webhook

```bash
supabase functions deploy telegram-capture --no-verify-jwt
```

Register the webhook with Telegram:

```bash
curl -X POST "https://api.telegram.org/botYOUR_BOT_TOKEN/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://your-project.supabase.co/functions/v1/telegram-capture",
    "secret_token": "YOUR_WEBHOOK_SECRET"
  }'
```

### 4. Usage

- Send any message to save it to your brain
- `/task Buy groceries` — add a task
- `/task p3 #work @computer Fix bug` — task with priority, project, context
- `/tasks` — list active tasks
- `/tasks next` — filter by status
- `/done groceries` — complete a task
- `/stats` — brain statistics
- `/help` — full command list

---

## Email Capture (Resend)

Captures inbound emails and PDF attachments.

### 1. Set Up Resend

1. Create a [Resend](https://resend.com) account
2. Add and verify your domain under **Domains**
3. Configure **Inbound emails** — set the MX records as instructed
4. Create a webhook endpoint pointing to your Edge Function

### 2. Set Secrets

```bash
supabase secrets set \
  RESEND_API_KEY=re_your-key \
  RESEND_WEBHOOK_SECRET=whsec_your-secret
```

### 3. Deploy

```bash
supabase functions deploy email-capture --no-verify-jwt
```

### 4. Configure Resend Webhook

In the Resend dashboard, add a webhook:
- **URL**: `https://your-project.supabase.co/functions/v1/email-capture`
- **Events**: `email.received`

### 5. Usage

Forward or send emails to your Resend domain. The function:
- Verifies the Svix webhook signature
- Fetches the full email via Resend API
- Extracts text from PDF attachments
- Ingests everything into the knowledge graph

---

## Notion Sync

Syncs pages from a Notion database.

### 1. Create a Notion Integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. Create a new integration, give it read access
3. Copy the **Internal Integration Token**

### 2. Share Your Database

1. Open the Notion database you want to sync
2. Click **Share** > invite your integration
3. Copy the database ID from the URL: `notion.so/your-workspace/DATABASE_ID?v=...`

### 3. Run Manually

```bash
python -m open_brain.connectors.notion_database \
  --database-id YOUR_DATABASE_ID \
  --token YOUR_NOTION_TOKEN \
  --sync \
  --limit 50
```

### 4. Daily Sync (GitHub Actions)

Set these GitHub Actions secrets:
- `NOTION_API_TOKEN`
- `NOTION_DATABASE_ID`
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
- `OPENROUTER_API_KEY`

The workflow runs daily at 6 AM UTC.

---

## YouTube Sync

Extracts transcripts from YouTube playlist videos.

### 1. Create a Playlist

Create a public or unlisted YouTube playlist with videos you want to ingest.

### 2. Run Manually

```bash
python -m open_brain.connectors.youtube \
  --playlist "https://www.youtube.com/playlist?list=YOUR_PLAYLIST_ID" \
  --sync \
  --limit 10
```

### 3. Daily Sync (GitHub Actions)

Set `YOUTUBE_PLAYLIST_URL` as a GitHub Actions secret. The workflow handles the rest.

> **Note:** Videos without available transcripts (auto-generated or manual) will be skipped.

---

## ChatGPT Conversations

Ingests exported ChatGPT conversation history.

### 1. Export from ChatGPT

1. Go to **Settings > Data controls > Export data**
2. Wait for the email, download the ZIP
3. Extract — find `conversations.json`

### 2. Ingest

```bash
python -m open_brain.connectors.chatgpt_conversations \
  --in path/to/conversations.json \
  --limit 50
```

The connector uses content-hash deduplication — safe to re-run. Already-ingested conversations are skipped automatically. Use `--limit` to process in batches (e.g. 50 at a time) or omit it to process everything.

---

## Claude Conversations

Ingests exported Claude conversation history.

### 1. Export from Claude

1. Go to **Settings > Export data**
2. Download the ZIP, extract `conversations.json`

### 2. Ingest

```bash
python -m open_brain.connectors.claude_conversations \
  --in path/to/conversations.json \
  --limit 50
```

Same dedup behavior as ChatGPT — safe to re-run, duplicates are skipped.

---

## Local Files

Ingest markdown and text files from your filesystem.

### Bulk Ingest

```bash
python -m open_brain.connectors.local_bulk \
  --paths ~/Documents/notes ~/Desktop/research \
  --extensions .md .txt \
  --limit 100
```

### Folder Sync (One-Shot)

```bash
python -m open_brain.connectors.local_sync \
  --watch-dir ~/Documents/brain-inbox
```

Scans the directory once and ingests all new files. Uses content-hash deduplication — safe to re-run. Already-ingested files are skipped automatically.

To include ChatGPT/Claude conversation exports in the same scan:

```bash
python -m open_brain.connectors.local_sync \
  --watch-dir ~/Documents/exports \
  --include-conversations
```

Use `--dry-run` to preview what would be processed without ingesting.

---

## Slack Bot

Captures messages from Slack channels.

### 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Create a new app, add the `chat:write` and `channels:history` scopes
3. Install to your workspace

### 2. Set Secrets

```bash
supabase secrets set SLACK_BOT_TOKEN=xoxb-your-token
```

### 3. Deploy & Configure Events

```bash
supabase functions deploy slack-capture --no-verify-jwt
```

In Slack app settings, enable **Event Subscriptions**:
- **Request URL**: `https://your-project.supabase.co/functions/v1/slack-capture`
- Subscribe to `message.channels` events

### 4. Invite Bot

Invite the bot to channels you want to capture: `/invite @your-bot`

---

## WhatsApp Export

Ingests WhatsApp chat export files.

### 1. Export Chat

In WhatsApp: open chat > **More** > **Export chat** > **Without media**

### 2. Ingest

```bash
python -m open_brain.connectors.whatsapp_export \
  path/to/WhatsApp_Chat.txt \
  --group-size 20
```

Groups messages into batches of 20 for context-preserving ingestion.
