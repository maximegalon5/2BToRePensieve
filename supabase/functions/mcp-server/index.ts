// Open Brain MCP Server — Supabase Edge Function
// Exposes knowledge graph tools via MCP protocol over HTTP

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const OPENROUTER_KEY = Deno.env.get("OPENROUTER_API_KEY")!;
const EMBED_MODEL =
  Deno.env.get("OPENROUTER_EMBED_MODEL") || "openai/text-embedding-3-small";
const CHAT_MODEL =
  Deno.env.get("OPENROUTER_CHAT_MODEL") || "openai/gpt-4o-mini";

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);
const ACCESS_KEY = Deno.env.get("OPEN_BRAIN_ACCESS_KEY") || "";

// --- Embedding helper ---
async function embedQuery(text: string): Promise<number[]> {
  const res = await fetch("https://openrouter.ai/api/v1/embeddings", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${OPENROUTER_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ model: EMBED_MODEL, input: [text] }),
  });
  const data = await res.json();
  return data.data[0].embedding;
}

// --- LLM Reranking ---

interface SearchRow {
  result_type: string;
  result_id: string;
  name: string | null;
  content: string | null;
  entity_type: string | null;
  observation_type: string | null;
  similarity: number;
  metadata: Record<string, unknown> | null;
  entity_ids: string[] | null;
  source_id: string | null;
}

async function rerankWithLLM(
  query: string,
  candidates: SearchRow[],
  topN: number,
): Promise<SearchRow[]> {
  if (candidates.length === 0) return [];
  if (candidates.length <= topN) return candidates;

  // Build numbered list of candidates for the LLM
  const numbered = candidates.map((r, i) => {
    const label = r.result_type === "entity"
      ? `[entity: ${r.entity_type}] ${r.name}: ${(r.content || "").slice(0, 200)}`
      : r.result_type === "task"
      ? `[task: ${r.observation_type}] ${r.name}: ${(r.content || "").slice(0, 200)}`
      : `[observation: ${r.observation_type}] ${(r.content || "").slice(0, 300)}`;
    return `${i + 1}. ${label}`;
  }).join("\n");

  const prompt = `You are a relevance scoring engine for a personal knowledge graph. Given a search query and numbered results, score each result's relevance to the query from 0 to 10.

10 = directly answers or is the core subject of the query
7-9 = highly relevant, closely related
4-6 = somewhat relevant, tangentially related
1-3 = barely relevant
0 = not relevant at all

Query: "${query}"

Results:
${numbered}

Respond with ONLY a JSON array of scores in order, e.g. [8, 3, 10, 5, ...]
No other text.`;

  try {
    const res = await fetch("https://openrouter.ai/api/v1/chat/completions", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${OPENROUTER_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: CHAT_MODEL,
        messages: [{ role: "user", content: prompt }],
        temperature: 0,
        max_tokens: 500,
      }),
    });

    const data = await res.json();
    const raw = data.choices?.[0]?.message?.content || "";

    // Parse scores — extract JSON array from response
    const match = raw.match(/\[[\d\s,.]+\]/);
    if (!match) {
      console.error("Rerank: could not parse scores, falling back to similarity order");
      return candidates.slice(0, topN);
    }

    const scores: number[] = JSON.parse(match[0]);
    if (scores.length !== candidates.length) {
      console.error(`Rerank: got ${scores.length} scores for ${candidates.length} candidates, falling back`);
      return candidates.slice(0, topN);
    }

    // Attach scores and sort by relevance (descending), break ties with similarity
    const scored = candidates.map((r, i) => ({
      ...r,
      relevance_score: scores[i] || 0,
    }));
    scored.sort((a, b) =>
      b.relevance_score - a.relevance_score || b.similarity - a.similarity
    );

    return scored.slice(0, topN);
  } catch (err) {
    console.error("Rerank failed, falling back to similarity order:", err);
    return candidates.slice(0, topN);
  }
}

// --- Tool implementations ---

async function searchBrain(args: Record<string, unknown>) {
  const query = args.query as string;
  const limit = (args.limit as number) || 20;
  const rerank = args.rerank !== false; // default true

  // Over-fetch candidates for reranking (3x limit, min 30, max 60)
  const fetchCount = rerank ? Math.min(Math.max(limit * 3, 30), 60) : limit;
  const embedding = await embedQuery(query);

  const { data, error } = await supabase.rpc("search_knowledge", {
    query_embedding: embedding,
    match_count: fetchCount,
    filter_entity_type: (args.entity_type as string) || null,
    filter_observation_type: (args.observation_type as string) || null,
  });

  if (error) return { error: error.message };

  // Rerank if enabled
  let ranked = (data || []) as SearchRow[];
  if (rerank && ranked.length > 0) {
    ranked = await rerankWithLLM(query, ranked, limit);
  } else {
    ranked = ranked.slice(0, limit);
  }

  const results = [];
  for (const row of ranked) {
    const item: Record<string, unknown> = { ...row };

    if (row.result_type === "observation" && row.entity_ids?.length > 0) {
      const { data: entities } = await supabase
        .from("entities")
        .select("id, name, entity_type")
        .in("id", row.entity_ids);
      item.linked_entities = entities;
    }

    if (row.source_id) {
      const { data: source } = await supabase
        .from("sources")
        .select("id, source_type, origin, title")
        .eq("id", row.source_id)
        .single();
      item.source = source;
    }

    results.push(item);
  }

  return { results, reranked: rerank };
}

async function getEntity(args: Record<string, unknown>) {
  const nameOrId = args.name_or_id as string;

  let { data: entity } = await supabase
    .from("entities")
    .select("*")
    .eq("id", nameOrId)
    .single();

  if (!entity) {
    const { data: byName } = await supabase
      .from("entities")
      .select("*")
      .ilike("name", `%${nameOrId}%`)
      .limit(1);
    entity = byName?.[0];
  }

  if (!entity) return { error: `Entity not found: ${nameOrId}` };

  const { data: context } = await supabase.rpc("get_entity_context", {
    target_entity_id: entity.id,
    depth: 1,
  });

  return context;
}

async function exploreNeighborhood(args: Record<string, unknown>) {
  const entityId = args.entity_id as string;
  const depth = (args.depth as number) || 1;

  const { data } = await supabase.rpc("get_entity_context", {
    target_entity_id: entityId,
    depth,
  });

  return data;
}

async function addThought(args: Record<string, unknown>) {
  const content = args.content as string;
  const sourceType = (args.source_type as string) || "mcp";
  const title = (args.title as string) || "";
  const captureType = (args.capture_type as string) || "general";
  const metadata = (args.metadata as Record<string, unknown>) || {};

  // Store capture_type in metadata
  metadata.capture_type = captureType;

  // Call the ingest Edge Function
  const res = await fetch(`${SUPABASE_URL}/functions/v1/ingest`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${SUPABASE_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      content,
      source_type: sourceType,
      origin: "mcp",
      title,
      metadata,
    }),
  });

  return await res.json();
}

async function listEntities(args: Record<string, unknown>) {
  const entityType = args.entity_type as string | undefined;
  const limit = (args.limit as number) || 50;
  const sort = (args.sort as string) || "recent";

  let query = supabase
    .from("entities")
    .select("id, name, entity_type, description, aliases, created_at");

  if (entityType) query = query.eq("entity_type", entityType);

  if (sort === "alphabetical") {
    query = query.order("name", { ascending: true });
  } else {
    query = query.order("created_at", { ascending: false });
  }

  const { data, error } = await query.limit(limit);
  if (error) return { error: error.message };
  return { entities: data };
}

async function listThoughts(args: Record<string, unknown>) {
  const sourceType = args.source_type as string | undefined;
  const captureType = args.capture_type as string | undefined;
  const days = (args.days as number) || 7;
  const limit = (args.limit as number) || 20;
  const search = args.search as string | undefined;

  const since = new Date();
  since.setDate(since.getDate() - days);

  let query = supabase
    .from("sources")
    .select("id, title, source_type, origin, created_at, raw_content, metadata")
    .gte("created_at", since.toISOString())
    .order("created_at", { ascending: false });

  if (sourceType) query = query.eq("source_type", sourceType);
  if (captureType) query = query.eq("metadata->>capture_type", captureType);
  if (search) query = query.ilike("title", `%${search}%`);

  const { data, error } = await query.limit(limit);
  if (error) return { error: error.message };

  const thoughts = [];
  for (const src of data || []) {
    const { count } = await supabase
      .from("observations")
      .select("id", { count: "exact", head: true })
      .eq("source_id", src.id);

    thoughts.push({
      id: src.id,
      title: src.title || "(untitled)",
      source_type: src.source_type,
      capture_type: (src.metadata as Record<string, unknown>)?.capture_type || null,
      origin: src.origin,
      created_at: src.created_at,
      preview: (src.raw_content || "").slice(0, 200),
      observation_count: count || 0,
    });
  }

  return { thoughts, total: thoughts.length };
}

// --- Task implementations ---

async function addTask(args: Record<string, unknown>) {
  const title = args.title as string;
  const description = (args.description as string) || "";
  const status = (args.status as string) || "inbox";
  const priority = (args.priority as number) || 0;
  const category = (args.category as string) || "personal";
  const dueDate = (args.due_date as string) || null;
  const context = (args.context as string) || "";
  const project = (args.project as string) || "";

  // Embed for semantic search
  const embeddingText = `${title}${description ? ": " + description : ""}${project ? " [" + project + "]" : ""}`;
  const embedding = await embedQuery(embeddingText);

  // Try to link to existing entities by project name
  const entityIds: string[] = [];
  if (project) {
    const { data: projectEntity } = await supabase
      .from("entities")
      .select("id")
      .ilike("name", project)
      .limit(1);
    if (projectEntity?.[0]) {
      entityIds.push(projectEntity[0].id);
    }
  }

  const { data, error } = await supabase
    .from("tasks")
    .insert({
      title,
      description,
      status,
      priority,
      category,
      due_date: dueDate,
      context,
      project,
      entity_ids: entityIds,
      embedding,
    })
    .select()
    .single();

  if (error) return { error: error.message };
  return { task: data };
}

async function listTasks(args: Record<string, unknown>) {
  const status = args.status as string | undefined;
  const category = args.category as string | undefined;
  const project = args.project as string | undefined;
  const limit = (args.limit as number) || 20;
  const includeDone = (args.include_done as boolean) || false;

  let query = supabase
    .from("tasks")
    .select(
      "id, title, description, status, priority, category, due_date, context, project, entity_ids, created_at, updated_at, completed_at",
    );

  if (status) {
    query = query.eq("status", status);
  } else if (!includeDone) {
    query = query.neq("status", "done");
  }

  if (category) query = query.eq("category", category);
  if (project) query = query.ilike("project", `%${project}%`);

  query = query
    .order("priority", { ascending: false })
    .order("created_at", { ascending: false });

  const { data, error } = await query.limit(limit);
  if (error) return { error: error.message };

  // Enrich with linked entity names
  const tasks = [];
  for (const task of data || []) {
    const enriched: Record<string, unknown> = { ...task };
    if (task.entity_ids?.length > 0) {
      const { data: entities } = await supabase
        .from("entities")
        .select("id, name, entity_type")
        .in("id", task.entity_ids);
      enriched.linked_entities = entities;
    }
    tasks.push(enriched);
  }

  return { tasks, count: tasks.length };
}

async function updateTask(args: Record<string, unknown>) {
  const taskId = args.task_id as string;
  const updates: Record<string, unknown> = {};

  if (args.title !== undefined) updates.title = args.title;
  if (args.description !== undefined) updates.description = args.description;
  if (args.status !== undefined) {
    updates.status = args.status;
    if (args.status === "done") {
      updates.completed_at = new Date().toISOString();
    }
  }
  if (args.priority !== undefined) updates.priority = args.priority;
  if (args.category !== undefined) updates.category = args.category;
  if (args.due_date !== undefined) updates.due_date = args.due_date;
  if (args.context !== undefined) updates.context = args.context;
  if (args.project !== undefined) updates.project = args.project;

  // Re-embed if title or description changed
  if (updates.title || updates.description) {
    const { data: existing } = await supabase
      .from("tasks")
      .select("title, description, project")
      .eq("id", taskId)
      .single();

    const newTitle = (updates.title as string) || existing?.title || "";
    const newDesc =
      (updates.description as string) || existing?.description || "";
    const proj = (updates.project as string) || existing?.project || "";
    const embeddingText = `${newTitle}${newDesc ? ": " + newDesc : ""}${proj ? " [" + proj + "]" : ""}`;
    updates.embedding = await embedQuery(embeddingText);
  }

  const { data, error } = await supabase
    .from("tasks")
    .update(updates)
    .eq("id", taskId)
    .select()
    .single();

  if (error) return { error: error.message };
  return { task: data };
}

async function completeTask(args: Record<string, unknown>) {
  const taskId = args.task_id as string;

  const { data, error } = await supabase
    .from("tasks")
    .update({ status: "done", completed_at: new Date().toISOString() })
    .eq("id", taskId)
    .select()
    .single();

  if (error) return { error: error.message };
  return { task: data, message: `✅ "${data.title}" completed` };
}

async function getSource(args: Record<string, unknown>) {
  const search = args.search as string;
  const sourceType = args.source_type as string | undefined;
  const limit = (args.limit as number) || 5;

  let query = supabase
    .from("sources")
    .select("id, title, source_type, origin, created_at, metadata")
    .ilike("title", `%${search}%`)
    .order("created_at", { ascending: false });

  if (sourceType) query = query.eq("source_type", sourceType);

  const { data, error } = await query.limit(limit);
  if (error) return { error: error.message };
  return { sources: data };
}

async function thoughtStats(_args: Record<string, unknown>) {
  const { count: totalSources } = await supabase
    .from("sources")
    .select("id", { count: "exact", head: true });
  const { count: totalEntities } = await supabase
    .from("entities")
    .select("id", { count: "exact", head: true });
  const { count: totalRelations } = await supabase
    .from("relations")
    .select("id", { count: "exact", head: true });
  const { count: totalObservations } = await supabase
    .from("observations")
    .select("id", { count: "exact", head: true });

  const { data: sourcesByType } = await supabase
    .from("sources")
    .select("source_type")
    .order("source_type");

  const typeBreakdown: Record<string, number> = {};
  for (const s of sourcesByType || []) {
    const t = s.source_type || "unknown";
    typeBreakdown[t] = (typeBreakdown[t] || 0) + 1;
  }

  const { data: entitiesByType } = await supabase
    .from("entities")
    .select("entity_type");

  const entityTypeBreakdown: Record<string, number> = {};
  for (const e of entitiesByType || []) {
    const t = e.entity_type || "unknown";
    entityTypeBreakdown[t] = (entityTypeBreakdown[t] || 0) + 1;
  }

  let topEntities = null;
  try {
    const rpcResult = await supabase.rpc("get_top_connected_entities", {
      result_limit: 10,
    });
    topEntities = rpcResult.data;
  } catch {
    // RPC not available yet, skip
  }

  const since7d = new Date();
  since7d.setDate(since7d.getDate() - 7);
  const sinceStr = since7d.toISOString();

  const { count: recent7dSources } = await supabase
    .from("sources")
    .select("id", { count: "exact", head: true })
    .gte("created_at", sinceStr);
  const { count: recent7dEntities } = await supabase
    .from("entities")
    .select("id", { count: "exact", head: true })
    .gte("created_at", sinceStr);
  const { count: recent7dObs } = await supabase
    .from("observations")
    .select("id", { count: "exact", head: true })
    .gte("created_at", sinceStr);

  return {
    totals: {
      sources: totalSources || 0,
      entities: totalEntities || 0,
      relations: totalRelations || 0,
      observations: totalObservations || 0,
    },
    sources_by_type: typeBreakdown,
    entity_types: entityTypeBreakdown,
    top_connected_entities: topEntities || [],
    last_7_days: {
      sources: recent7dSources || 0,
      entities: recent7dEntities || 0,
      observations: recent7dObs || 0,
    },
  };
}

// --- MCP Protocol ---

const TOOLS = [
  {
    name: "search_brain",
    description:
      "Semantic search across the knowledge graph with LLM reranking. Over-fetches candidates by embedding similarity, then uses an LLM to score relevance to your query and returns the most relevant results. Reranking is on by default.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "What to search for" },
        limit: { type: "number", description: "Max results to return after reranking (default 20)" },
        entity_type: { type: "string", description: "Filter by entity type" },
        observation_type: {
          type: "string",
          description: "Filter by observation type",
        },
        rerank: {
          type: "boolean",
          description: "Enable LLM reranking for better relevance (default true). Set false for faster raw similarity results.",
        },
      },
      required: ["query"],
    },
  },
  {
    name: "get_entity",
    description:
      "Look up a specific entity by name or ID. Returns the entity, all its relations, and all observations about it.",
    inputSchema: {
      type: "object",
      properties: {
        name_or_id: { type: "string", description: "Entity name or UUID" },
      },
      required: ["name_or_id"],
    },
  },
  {
    name: "explore_neighborhood",
    description:
      "From an entity, traverse N hops of relations. Shows everything connected to it.",
    inputSchema: {
      type: "object",
      properties: {
        entity_id: { type: "string", description: "Entity UUID" },
        depth: {
          type: "number",
          description: "Hops to traverse (default 1)",
        },
      },
      required: ["entity_id"],
    },
  },
  {
    name: "add_thought",
    description: `Capture a thought, decision, insight, or any content into the knowledge graph. Extracts entities, relations, and observations automatically.

Use these structured formats for best results:

• DECISION: "Decided [X] because [Y]. Context: [Z]"
• PERSON NOTE: "[Name] — [role/context]. Key detail: [X]"
• INSIGHT: "Realized [X] while [doing Y]. Implication: [Z]"
• MEETING DEBRIEF: "Met with [who] about [topic]. Outcome: [X]. Action: [Y]"
• AI SAVE: "From [AI/source]: [key takeaway]. Application: [X]"

You can also capture freeform text — the above are just guides for richer extraction.`,
    inputSchema: {
      type: "object",
      properties: {
        content: {
          type: "string",
          description: "The thought or content to capture. Use structured formats above for best extraction.",
        },
        source_type: {
          type: "string",
          description: "Source: mcp, slack, telegram, email, notion, youtube, etc. Defaults to 'mcp'.",
        },
        capture_type: {
          type: "string",
          enum: ["decision", "person_note", "insight", "meeting", "ai_save", "general"],
          description: "Optional capture category. Defaults to 'general' if omitted.",
        },
        title: { type: "string", description: "Optional title for the thought" },
      },
      required: ["content"],
    },
  },
  {
    name: "list_entities",
    description: "Browse entities in the knowledge graph by type or recency.",
    inputSchema: {
      type: "object",
      properties: {
        entity_type: {
          type: "string",
          description:
            "Filter: person, concept, project, tool, decision, event, place, organization",
        },
        limit: { type: "number", description: "Max results (default 50)" },
        sort: { type: "string", description: "Sort: recent or alphabetical" },
      },
    },
  },
  {
    name: "list_thoughts",
    description:
      "Browse recent thoughts and captured content. Filter by source type, capture type, timeframe, or keyword.",
    inputSchema: {
      type: "object",
      properties: {
        source_type: {
          type: "string",
          description:
            "Filter by source: slack, mcp, telegram, email, chatgpt_conversation, claude_conversation, notion, youtube",
        },
        capture_type: {
          type: "string",
          enum: ["decision", "person_note", "insight", "meeting", "ai_save", "general"],
          description: "Filter by capture category (e.g. 'decision' to see all decisions)",
        },
        days: {
          type: "number",
          description: "How many days back to look (default 7)",
        },
        limit: { type: "number", description: "Max results (default 20)" },
        search: {
          type: "string",
          description: "Keyword filter on title",
        },
      },
    },
  },
  {
    name: "thought_stats",
    description:
      "Get aggregate statistics about your knowledge graph: total counts, breakdowns by type, most connected entities, and recent activity.",
    inputSchema: {
      type: "object",
      properties: {},
    },
  },
  {
    name: "add_task",
    description:
      "Create a new task. Supports GTD statuses (inbox/next/waiting/someday/done), priority (0-4), and personal/professional categories. Tasks are embedded for semantic search and auto-linked to knowledge graph entities.",
    inputSchema: {
      type: "object",
      properties: {
        title: { type: "string", description: "Task title" },
        description: {
          type: "string",
          description: "Optional details about the task",
        },
        status: {
          type: "string",
          enum: ["inbox", "next", "waiting", "someday"],
          description: "GTD status (default: inbox)",
        },
        priority: {
          type: "number",
          description: "Priority 0-4 (0=none, 1=low, 2=medium, 3=high, 4=urgent)",
        },
        category: {
          type: "string",
          enum: ["personal", "professional"],
          description: "Category (default: personal)",
        },
        due_date: {
          type: "string",
          description: "Due date in YYYY-MM-DD format",
        },
        context: {
          type: "string",
          description: "GTD context: @home, @work, @errands, @computer, etc.",
        },
        project: {
          type: "string",
          description:
            "Project name. Auto-links to matching entity in knowledge graph.",
        },
      },
      required: ["title"],
    },
  },
  {
    name: "list_tasks",
    description:
      "List tasks with filters. Shows active tasks by default (excludes done). Includes linked knowledge graph entities for context.",
    inputSchema: {
      type: "object",
      properties: {
        status: {
          type: "string",
          enum: ["inbox", "next", "waiting", "someday", "done"],
          description: "Filter by specific status",
        },
        category: {
          type: "string",
          enum: ["personal", "professional"],
          description: "Filter by category",
        },
        project: {
          type: "string",
          description: "Filter by project name (partial match)",
        },
        include_done: {
          type: "boolean",
          description: "Include completed tasks (default: false)",
        },
        limit: { type: "number", description: "Max results (default 20)" },
      },
    },
  },
  {
    name: "update_task",
    description:
      "Update a task's status, priority, description, or other fields. Re-embeds automatically if title/description change.",
    inputSchema: {
      type: "object",
      properties: {
        task_id: { type: "string", description: "Task UUID" },
        title: { type: "string", description: "New title" },
        description: { type: "string", description: "New description" },
        status: {
          type: "string",
          enum: ["inbox", "next", "waiting", "someday", "done"],
        },
        priority: { type: "number", description: "Priority 0-4" },
        category: {
          type: "string",
          enum: ["personal", "professional"],
        },
        due_date: { type: "string", description: "YYYY-MM-DD or null" },
        context: { type: "string" },
        project: { type: "string" },
      },
      required: ["task_id"],
    },
  },
  {
    name: "complete_task",
    description: "Mark a task as done. Sets status to 'done' and records completion timestamp.",
    inputSchema: {
      type: "object",
      properties: {
        task_id: { type: "string", description: "Task UUID to complete" },
      },
      required: ["task_id"],
    },
  },
  {
    name: "get_source",
    description:
      "Find source content by title keyword. Returns the origin URL — use this to recall YouTube links, Notion page URLs, or any source URL. Filter by source_type for targeted lookup.",
    inputSchema: {
      type: "object",
      properties: {
        search: {
          type: "string",
          description: "Keyword to search in source titles",
        },
        source_type: {
          type: "string",
          description:
            "Filter: youtube, notion_page, telegram, email, chatgpt_conversation, claude_conversation, mcp",
        },
        limit: { type: "number", description: "Max results (default 5)" },
      },
      required: ["search"],
    },
  },
];

const TOOL_HANDLERS: Record<
  string,
  (args: Record<string, unknown>) => Promise<unknown>
> = {
  search_brain: searchBrain,
  get_entity: getEntity,
  explore_neighborhood: exploreNeighborhood,
  add_thought: addThought,
  list_entities: listEntities,
  list_thoughts: listThoughts,
  thought_stats: thoughtStats,
  add_task: addTask,
  list_tasks: listTasks,
  update_task: updateTask,
  complete_task: completeTask,
  get_source: getSource,
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, {
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
      },
    });
  }

  // Access key authentication — supports both:
  //   1. Authorization: Bearer <key>  (Claude Code, Cursor)
  //   2. ?key=<key> query parameter    (ChatGPT MCP connector)
  if (ACCESS_KEY) {
    const authHeader = req.headers.get("Authorization") || "";
    const bearerToken = authHeader.replace("Bearer ", "");
    const url = new URL(req.url);
    const queryToken = url.searchParams.get("key") || "";
    const token = bearerToken || queryToken;

    if (token !== ACCESS_KEY) {
      return new Response(
        JSON.stringify({
          jsonrpc: "2.0",
          id: null,
          error: { code: -32000, message: "Unauthorized" },
        }),
        {
          status: 401,
          headers: {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
          },
        },
      );
    }
  }

  const body = await req.json();
  const { method, params, id } = body;

  let result;

  switch (method) {
    case "initialize":
      result = {
        protocolVersion: "2024-11-05",
        serverInfo: { name: "open-brain", version: "0.1.0" },
        capabilities: { tools: {} },
      };
      break;

    case "tools/list":
      result = { tools: TOOLS };
      break;

    case "tools/call": {
      const toolName = params?.name;
      const toolArgs = params?.arguments || {};
      const handler = TOOL_HANDLERS[toolName];

      if (!handler) {
        result = {
          content: [{ type: "text", text: `Unknown tool: ${toolName}` }],
          isError: true,
        };
      } else {
        try {
          const data = await handler(toolArgs);
          result = {
            content: [
              { type: "text", text: JSON.stringify(data, null, 2) },
            ],
          };
        } catch (err: unknown) {
          const message =
            err instanceof Error ? err.message : String(err);
          result = {
            content: [{ type: "text", text: `Error: ${message}` }],
            isError: true,
          };
        }
      }
      break;
    }

    default:
      result = {
        error: { code: -32601, message: `Method not found: ${method}` },
      };
  }

  return new Response(JSON.stringify({ jsonrpc: "2.0", id, result }), {
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
    },
  });
});
