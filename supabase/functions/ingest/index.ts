// Open Brain Universal Ingest — Supabase Edge Function
// Receives content from any connector, extracts knowledge, writes to graph.
//
// Optimized for minimal LLM/API calls per chunk:
// - 1 LLM call for knowledge extraction
// - 1 embedding call for all entities (batched)
// - 1 embedding call for all observations (batched)
// - 0-1 LLM call for batch entity merge confirmation
// - Dedicated entity-only similarity search (skips observations/tasks)

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const OPENROUTER_KEY = Deno.env.get("OPENROUTER_API_KEY")!;
const EMBED_MODEL =
  Deno.env.get("OPENROUTER_EMBED_MODEL") || "openai/text-embedding-3-small";
const CHAT_MODEL =
  Deno.env.get("OPENROUTER_CHAT_MODEL") || "openai/gpt-4o-mini";

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

// --- Helpers ---

async function hashContent(text: string): Promise<string> {
  const encoder = new TextEncoder();
  const data = encoder.encode(text);
  const hashBuffer = await crypto.subtle.digest("SHA-256", data);
  const hashArray = Array.from(new Uint8Array(hashBuffer));
  return hashArray.map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function embedBatch(texts: string[]): Promise<number[][]> {
  if (texts.length === 0) return [];
  const res = await fetch("https://openrouter.ai/api/v1/embeddings", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${OPENROUTER_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ model: EMBED_MODEL, input: texts }),
  });
  const data = await res.json();
  return data.data.map((d: { embedding: number[] }) => d.embedding);
}

async function extractKnowledge(
  content: string,
  sourceType: string,
  title: string,
) {
  const systemPrompt = `You are a knowledge extraction engine. Given source content, extract structured knowledge as JSON.

Extract:
1. Entities — people, concepts, projects, tools, decisions, events, places, organizations
2. Relations — directed connections between entities
3. Observations — specific claims, facts, decisions, preferences, action items, insights

Rules:
- Entity names should be canonical (e.g., "Python" not "python language")
- Each observation should be a single, self-contained statement
- Be thorough but precise — extract what is actually stated, not inferred
- If the source is conversational, extract the key knowledge, not every utterance

Respond with valid JSON only. No markdown fences. Schema:
{"entities": [{"name": "string", "type": "string", "description": "string"}], "relations": [{"source": "string", "target": "string", "type": "string", "description": "string"}], "observations": [{"content": "string", "type": "string", "entities": ["string"]}]}`;

  const userMsg = `Source type: ${sourceType}\nTitle: ${title || "(untitled)"}\n\nContent:\n${content.slice(0, 12000)}`;

  const res = await fetch("https://openrouter.ai/api/v1/chat/completions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${OPENROUTER_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: CHAT_MODEL,
      messages: [
        { role: "system", content: systemPrompt },
        { role: "user", content: userMsg },
      ],
      temperature: 0.1,
      response_format: { type: "json_object" },
    }),
  });

  const data = await res.json();
  const raw = data.choices?.[0]?.message?.content || "{}";

  try {
    return JSON.parse(raw);
  } catch {
    return { entities: [], relations: [], observations: [] };
  }
}

interface EntityCandidate {
  id: string;
  name: string;
  entity_type: string;
  description: string;
  aliases: string[];
  similarity: number;
}

async function batchConfirmMerges(
  pairs: Array<{
    newName: string;
    newType: string;
    newDesc: string;
    existing: EntityCandidate;
  }>,
): Promise<Record<string, boolean>> {
  if (pairs.length === 0) return {};

  const numbered = pairs
    .map((p, i) => {
      const aliases = (p.existing.aliases || []).join(", ") || "(none)";
      return (
        `${i + 1}. NEW: "${p.newName}" (${p.newType}) — ${p.newDesc || "(no desc)"}\n` +
        `   EXISTING: "${p.existing.name}" (${p.existing.entity_type}) — ${p.existing.description || "(no desc)"} — aliases: ${aliases}`
      );
    })
    .join("\n");

  const prompt =
    `For each pair below, decide if the NEW entity is the same thing as the EXISTING entity.\n` +
    `Consider name similarity, type, and description. Answer with a JSON array of booleans, one per pair, in order.\n\n` +
    numbered +
    `\n\nRespond with ONLY a JSON array, e.g. [true, false, true]\nNo other text.`;

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
        max_tokens: 200,
      }),
    });

    const data = await res.json();
    const raw = data.choices?.[0]?.message?.content || "[]";
    const match = raw.match(/\[[\w\s,]+\]/);
    if (match) {
      const decisions: boolean[] = JSON.parse(match[0]);
      if (decisions.length === pairs.length) {
        const result: Record<string, boolean> = {};
        pairs.forEach((p, i) => {
          result[p.newName] = !!decisions[i];
        });
        return result;
      }
    }
    // Fallback: no merge (conservative)
    const result: Record<string, boolean> = {};
    pairs.forEach((p) => {
      result[p.newName] = false;
    });
    return result;
  } catch {
    const result: Record<string, boolean> = {};
    pairs.forEach((p) => {
      result[p.newName] = false;
    });
    return result;
  }
}

// --- Main handler ---

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, {
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
      },
    });
  }

  try {
    const { content, source_type, origin, title, metadata } = await req.json();

    if (!content) {
      return Response.json({ error: "content is required" }, { status: 400 });
    }

    // 1. Dedup check (DB only)
    const contentHash = await hashContent(content);
    const { data: existing } = await supabase
      .from("sources")
      .select("id")
      .eq("content_hash", contentHash);

    if (existing && existing.length > 0) {
      return Response.json({
        status: "duplicate",
        message: "Content already ingested",
      });
    }

    // 2. Store source (DB only)
    const { data: source, error: sourceErr } = await supabase
      .from("sources")
      .insert({
        source_type: source_type || "unknown",
        origin: origin || "",
        title: title || "",
        raw_content: content,
        content_hash: contentHash,
        status: "pending",
        metadata: metadata || {},
      })
      .select()
      .single();

    if (sourceErr)
      throw new Error(`Source insert failed: ${sourceErr.message}`);

    // 3. Extract knowledge (1 LLM call)
    const extraction = await extractKnowledge(
      content,
      source_type || "unknown",
      title || "",
    );

    const entities = extraction.entities || [];
    const relations = extraction.relations || [];
    const observations = extraction.observations || [];

    // 4. Batch embed all entity texts (1 API call instead of N)
    const entityTexts = entities.map(
      (e: { name: string; description?: string }) =>
        e.description ? `${e.name}: ${e.description}` : e.name,
    );
    const entityEmbeddings = await embedBatch(entityTexts);

    // 5. Search for entity candidates (DB calls only — dedicated entity RPC)
    const entityCandidates: Array<EntityCandidate | null> = [];
    for (let i = 0; i < entities.length; i++) {
      const { data: similar } = await supabase.rpc(
        "search_similar_entities",
        {
          query_embedding: entityEmbeddings[i],
          match_count: 1,
          similarity_threshold: 0.85,
        },
      );
      entityCandidates.push(similar?.[0] || null);
    }

    // 6. Batch LLM merge confirmation (0-1 LLM call total)
    const mergePairs: Array<{
      newName: string;
      newType: string;
      newDesc: string;
      existing: EntityCandidate;
    }> = [];

    for (let i = 0; i < entities.length; i++) {
      if (entityCandidates[i]) {
        mergePairs.push({
          newName: entities[i].name,
          newType: entities[i].type || "concept",
          newDesc: entities[i].description || "",
          existing: entityCandidates[i]!,
        });
      }
    }

    const mergeDecisions = await batchConfirmMerges(mergePairs);

    // 7. Apply entity decisions: merge or create (DB calls only)
    const entityNameToId: Record<string, string> = {};

    for (let i = 0; i < entities.length; i++) {
      const entity = entities[i];
      const candidate = entityCandidates[i];

      if (candidate && mergeDecisions[entity.name]) {
        // Merge: add alias
        const aliases = candidate.aliases || [];
        if (!aliases.includes(entity.name)) {
          aliases.push(entity.name);
          await supabase
            .from("entities")
            .update({ aliases })
            .eq("id", candidate.id);
        }
        entityNameToId[entity.name] = candidate.id;
      } else {
        // Create new entity
        const { data: newEntity } = await supabase
          .from("entities")
          .insert({
            name: entity.name,
            entity_type: entity.type || "concept",
            description: entity.description || "",
            embedding: entityEmbeddings[i],
          })
          .select()
          .single();

        if (newEntity) {
          entityNameToId[entity.name] = newEntity.id;
        }
      }
    }

    // 8. Store relations — dedup: skip if same edge exists (DB calls only)
    for (const rel of relations) {
      const sourceEid = entityNameToId[rel.source];
      const targetEid = entityNameToId[rel.target];
      if (sourceEid && targetEid) {
        const { data: existingRel } = await supabase
          .from("relations")
          .select("id")
          .eq("source_entity", sourceEid)
          .eq("target_entity", targetEid)
          .eq("relation_type", rel.type || "related_to")
          .limit(1);

        if (!existingRel || existingRel.length === 0) {
          await supabase.from("relations").insert({
            source_entity: sourceEid,
            target_entity: targetEid,
            relation_type: rel.type || "related_to",
            description: rel.description || "",
            source_id: source.id,
          });
        }
      }
    }

    // 9. Batch embed all observations (1 API call instead of N)
    let obsSkipped = 0;
    if (observations.length > 0) {
      const obsTexts = observations.map(
        (o: { content: string }) => o.content,
      );
      const obsEmbeddings = await embedBatch(obsTexts);

      for (let i = 0; i < observations.length; i++) {
        const obs = observations[i];

        // 9a. Exact content hash dedup (DB only)
        const obsHash = await hashContent(obs.content);
        const { data: existingObs } = await supabase
          .from("observations")
          .select("id")
          .eq("content_hash", obsHash)
          .limit(1);

        if (existingObs && existingObs.length > 0) {
          obsSkipped++;
          continue;
        }

        // 9b. Semantic similarity dedup (DB only)
        const { data: similarObs } = await supabase.rpc("search_knowledge", {
          query_embedding: obsEmbeddings[i],
          match_count: 1,
          filter_entity_type: null,
          filter_observation_type: null,
        });

        const nearDuplicate = (similarObs || []).find(
          (r: Record<string, unknown>) =>
            r.result_type === "observation" &&
            (r.similarity as number) >= 0.95,
        );

        if (nearDuplicate) {
          obsSkipped++;
          continue;
        }

        // 9c. Insert observation (DB only)
        const obsEntityIds = (obs.entities || [])
          .map((name: string) => entityNameToId[name])
          .filter(Boolean);

        await supabase.from("observations").insert({
          content: obs.content,
          content_hash: obsHash,
          embedding: obsEmbeddings[i],
          observation_type: obs.type || "fact",
          entity_ids: obsEntityIds,
          source_id: source.id,
        });
      }
    }

    // 10. Mark source as extracted
    await supabase
      .from("sources")
      .update({ status: "extracted" })
      .eq("id", source.id);

    return Response.json({
      status: "success",
      source_id: source.id,
      entities_count: entities.length,
      relations_count: relations.length,
      observations_count: observations.length,
      observations_skipped: obsSkipped,
    });
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    return Response.json({ status: "failed", error: message }, { status: 500 });
  }
});
