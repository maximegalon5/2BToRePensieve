# ADR-001: LLM-Based Intent Detection for Telegram Bot

**Date:** 2026-03-06
**Status:** Accepted
**Deciders:** V (maximegalon5), Claude Opus 4.6

## Context

The Telegram bot (@your_bot) for Open Brain was built as a conversational agent that saves messages to the knowledge graph and replies with contextual information. However, it treated every non-command message as a thought to save. When a user asked natural language questions like "show me my tasks" or "what do I know about Python?", the bot would:

1. Ingest the question as a new thought into the knowledge graph
2. Run a semantic vector search (unrelated to the actual intent)
3. Pass random search results to an LLM with a 250-character limit
4. Return an inconsistent, often unhelpful response

The bot already had slash commands (`/tasks`, `/task`, `/done`, `/stats`) that worked correctly, but users naturally communicate in free-form language, not commands.

## Decision

Add an LLM-based intent classification layer that runs before the ingest pipeline. Natural language messages are classified into one of 7 intents and routed to the appropriate existing handler. If classification is ambiguous, the bot shows a help prompt instead of silently saving the message.

### Intent Schema

| Intent | Handler | Example |
|---|---|---|
| `list_tasks` | `listTasksForTelegram(filter)` | "show my tasks", "outstanding tasks for 2BToRePensieve" |
| `add_task` | `addTaskFromTelegram(text)` | "add a task to fix the login bug" |
| `complete_task` | `completeTaskFromTelegram(search)` | "mark the groceries task as done" |
| `search_knowledge` | `searchBrain()` + `generateReply()` | "what do I know about React" |
| `stats` | `getStats()` | "how big is my brain" |
| `save_thought` | ingest pipeline | "I just learned that Python 3.13 has a JIT compiler" |
| `ambiguous` | show help text | "hey", "hmm", prompt injection attempts |

## Approach Selection

We evaluated two approaches using a programmatic A/B test:

### Approach A: Single LLM Call
One gpt-4o-mini call returns a JSON object with both the intent classification and extracted parameters:
```json
{"intent": "list_tasks", "params": {"filter": "#2BToRePensieve"}}
```

### Approach B: Two-Stage Classify Then Extract
First LLM call returns just the intent label. A second call (only for intents needing parameters) extracts the parameters.

### Test Methodology

We built a Python test harness (`tests/intent_detection/run_comparison.py`) that ran 41 test cases through both approaches via OpenRouter's gpt-4o-mini API. Test cases covered 8 categories:

- **Task queries** (5): "show my tasks", "any next actions?"
- **Knowledge queries** (5): "what do I know about React", "anything on Kubernetes?"
- **Thoughts to save** (5): "I just learned...", "Note to self..."
- **Add task** (5): "add a task to fix the login bug", "remind me to buy groceries"
- **Complete task** (3): "mark the groceries task as done", "I finished the login bug fix"
- **Stats** (3): "how big is my brain", "give me stats"
- **Ambiguous** (5): "hey", "hmm", single emoji
- **Edge cases + adversarial** (10): typos, prompt injection, mixed intents, very long messages

### Results

| Metric | Approach A | Approach B |
|--------|-----------|-----------|
| Intent accuracy | **37/41 (90.2%)** | 35/41 (85.4%) |
| Param accuracy | **38/41 (92.7%)** | 37/41 (90.2%) |
| Avg latency | 1.40s | 1.17s |
| Avg tokens/call | 450.7 | 292.5 |

**Approach A won on accuracy.** B was slightly faster and cheaper per call, but the 5-percentage-point accuracy gap made A the clear choice.

### Shared Failures (both approaches)

Both approaches struggled with implicit completion signals:
- "I finished the login bug fix" -> both classified as `save_thought` (expected `complete_task`)
- "done with the PR review" -> both classified as `save_thought` (expected `complete_task`)

These are genuinely ambiguous without context -- "I finished X" reads as a statement of fact, not a command. The slash command `/done` remains the reliable path for task completion.

### Where A Beat B

Approach A handled idiomatic phrasing better:
- "any next actions?" -> A: `list_tasks` (correct), B: `ambiguous` (wrong)
- "how big is my brain" -> A: `stats` (correct), B: `ambiguous` (wrong)

The richer system prompt in A (which includes parameter descriptions alongside intents) gives the model more context to interpret ambiguous phrasing.

## Consequences

### Positive
- Users can interact naturally ("show my tasks" instead of `/tasks`)
- Slash commands still work as exact-match shortcuts (processed first, before intent detection)
- Ambiguous messages get a helpful prompt instead of being silently saved
- Adversarial/injection attempts are caught and treated as ambiguous
- Single API call adds ~1.4s latency but provides correct routing

### Negative
- Every non-command message now costs one gpt-4o-mini API call (~450 tokens, ~$0.00003)
- 90.2% accuracy means ~1 in 10 messages may be misrouted
- Implicit task completion ("I finished X") doesn't work well -- users should use `/done` or explicit phrasing ("mark X as done")

### Future Work
Five MCP server capabilities have no Telegram handler yet and are not included in the intent schema:
- `get_entity`, `explore_neighborhood`, `list_entities`, `list_thoughts`, `get_source`

These are tracked in the [roadmap](../plans/2026-03-04-code-review-fixes-roadmap.md) (items 12-16). When handlers are built, the intent detection prompt expands to include them.

## References
- Design doc: [`docs/plans/2026-03-06-telegram-intent-detection-design.md`](../plans/2026-03-06-telegram-intent-detection-design.md)
- Implementation plan: [`docs/plans/2026-03-06-telegram-intent-detection-plan.md`](../plans/2026-03-06-telegram-intent-detection-plan.md)
- Test results: [`tests/intent_detection/comparison_results.json`](../../tests/intent_detection/comparison_results.json)
- Production code: [`supabase/functions/telegram-capture/index.ts`](../../supabase/functions/telegram-capture/index.ts)
