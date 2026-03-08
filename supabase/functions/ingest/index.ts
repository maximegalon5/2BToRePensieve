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
import { Logger } from "../_shared/logger.ts";

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

async function embedBatch(texts: string[], log?: Logger): Promise<number[][]> {
  if (texts.length === 0) return [];
  const res = await fetch("https://openrouter.ai/api/v1/embeddings", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${OPENROUTER_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ model: EMBED_MODEL, input: texts }),
  });
  if (!res.ok) {
    const body = await res.text();
    const err = new Error(`Embedding API ${res.status}: ${body.slice(0, 200)}`);
    log?.error("embed_batch", err, { count: texts.length });
    throw err;
  }
  const data = await res.json();
  if (!data?.data?.length) {
    const err = new Error(`Embedding response malformed: ${JSON.stringify(data).slice(0, 200)}`);
    log?.error("embed_batch", err, { count: texts.length });
    throw err;
  }
  return data.data.map((d: { embedding: number[] }) => d.embedding);
}

async function extractKnowledge(
  content: string,
  sourceType: string,
  title: string,
) {
  const systemPrompt = `You are a knowledge extraction engine. Given source content, extract structured knowledge as JSON.

Extract:
1. Entities — classified into EXACTLY one of these 6 types:
   - person: any human — real people, fictional characters, clients, team members
   - organization: companies, brands, teams, institutions, communities
   - project: products, repos, applications, initiatives, services
   - concept: ideas, theories, patterns, methodologies, decisions, events, places, substances, medical/scientific terms — anything abstract or categorical
   - tool: software, libraries, frameworks, APIs, platforms, programming languages, hardware
   - content: books, articles, videos, papers, courses, media
2. Relations — directed connections between entities
3. Observations — specific claims, facts, decisions, preferences, action items, insights

Rules:
- Entity type MUST be one of: person, organization, project, concept, tool, content. No other types.
- Entity names should be canonical (e.g., "Python" not "python language", "React" not "React.js")
- Each observation should be a single, self-contained statement
- Be thorough but precise — extract what is actually stated, not inferred
- If the source is conversational, extract the key knowledge, not every utterance

Respond with valid JSON only. No markdown fences. Schema:
{"entities": [{"name": "string", "type": "person|organization|project|concept|tool|content", "description": "string"}], "relations": [{"source": "string", "target": "string", "type": "string", "description": "string"}], "observations": [{"content": "string", "type": "string", "entities": ["string"]}]}`;

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

  if (!res.ok) {
    const errBody = await res.text().catch(() => "");
    console.error("extractKnowledge: LLM API error", res.status, errBody.slice(0, 300));
    return { entities: [], relations: [], observations: [] };
  }

  const data = await res.json();
  const raw = data.choices?.[0]?.message?.content || "{}";

  try {
    return JSON.parse(raw);
  } catch {
    console.error("extractKnowledge: failed to parse LLM response:", raw.slice(0, 300));
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

    if (!res.ok) {
      const errBody = await res.text().catch(() => "");
      console.error("batchConfirmMerges: LLM API error", res.status, errBody.slice(0, 300));
      const result: Record<string, boolean> = {};
      pairs.forEach((p) => { result[p.newName] = false; });
      return result;
    }

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

  // Auth: require service role key
  const authHeader = req.headers.get("Authorization") || "";
  const token = authHeader.replace("Bearer ", "");
  if (token !== SUPABASE_KEY) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  const log = new Logger("ingest");

  try {
    const { content, source_type, origin, title, metadata } = await req.json();
    log.info("request", "Ingest request received", {
      source_type: source_type || "unknown",
      origin: origin || "",
      contentLength: content?.length || 0,
    });

    if (!content) {
      log.warn("validation", "Missing content");
      return Response.json({ error: "content is required" }, { status: 400 });
    }

    // 1. Dedup check (DB only)
    log.startStep("dedup_check");
    const contentHash = await hashContent(content);
    const { data: existing } = await supabase
      .from("sources")
      .select("id")
      .eq("content_hash", contentHash);

    if (existing && existing.length > 0) {
      log.endStep("dedup_check", "Duplicate found", { existingId: existing[0].id });
      log.summary({ status: "duplicate" });
      return Response.json({
        status: "duplicate",
        message: "Content already ingested",
      });
    }
    log.endStep("dedup_check", "No duplicate");

    // 2. Store source (DB only)
    log.startStep("source_insert");
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

    if (sourceErr) {
      log.failStep("source_insert", sourceErr);
      throw new Error(`Source insert failed: ${sourceErr.message}`);
    }
    log.endStep("source_insert", "Source stored", { sourceId: source.id });

    // 3. Extract knowledge (1 LLM call)
    log.startStep("extraction");
    const extraction = await extractKnowledge(
      content,
      source_type || "unknown",
      title || "",
    );

    const entities = extraction.entities || [];
    const relations = extraction.relations || [];
    const observations = extraction.observations || [];
    log.endStep("extraction", "Knowledge extracted", {
      entities: entities.length,
      relations: relations.length,
      observations: observations.length,
    });

    // 4. Resolve entities: exact name match → fuzzy embedding match → create new
    log.startStep("entity_resolution");
    const entityNameToId: Record<string, string> = {};

    // 4a. Batch exact name lookups first (cheapest, most reliable)
    const exactMatches: Array<EntityCandidate | null> = [];
    for (const entity of entities) {
      const { data: byName } = await supabase
        .from("entities")
        .select("id, name, entity_type, description, aliases")
        .ilike("name", entity.name)
        .limit(1);
      exactMatches.push(byName?.[0] ? { ...byName[0], similarity: 1.0 } as EntityCandidate : null);
    }

    // 4b. For entities without exact match, batch embed and fuzzy search
    const needsEmbedding: number[] = [];
    for (let i = 0; i < entities.length; i++) {
      if (!exactMatches[i]) needsEmbedding.push(i);
    }

    const embeddingTexts = needsEmbedding.map((i) => {
      const e = entities[i];
      return e.description ? `${e.name}: ${e.description}` : e.name;
    });
    const embeddings = await embedBatch(embeddingTexts, log);

    // Store all embeddings (needed for new entity creation)
    const entityEmbeddings: Array<number[] | null> = new Array(entities.length).fill(null);
    needsEmbedding.forEach((origIdx, embIdx) => {
      entityEmbeddings[origIdx] = embeddings[embIdx];
    });

    // 4c. Fuzzy search for unmatched entities
    const fuzzyCandidates: Array<EntityCandidate | null> = new Array(entities.length).fill(null);
    for (let j = 0; j < needsEmbedding.length; j++) {
      const origIdx = needsEmbedding[j];
      const { data: similar } = await supabase.rpc(
        "search_similar_entities",
        {
          query_embedding: embeddings[j],
          match_count: 1,
          similarity_threshold: 0.8,
        },
      );
      fuzzyCandidates[origIdx] = similar?.[0] || null;
    }

    // 4d. Batch LLM merge confirmation for fuzzy matches only
    const mergePairs: Array<{
      newName: string;
      newType: string;
      newDesc: string;
      existing: EntityCandidate;
      origIdx: number;
    }> = [];

    for (const origIdx of needsEmbedding) {
      if (fuzzyCandidates[origIdx]) {
        mergePairs.push({
          newName: entities[origIdx].name,
          newType: entities[origIdx].type || "concept",
          newDesc: entities[origIdx].description || "",
          existing: fuzzyCandidates[origIdx]!,
          origIdx,
        });
      }
    }

    const mergeDecisions = await batchConfirmMerges(mergePairs);

    // 4e. Apply decisions: exact match → fuzzy merge → create new
    for (let i = 0; i < entities.length; i++) {
      const entity = entities[i];

      if (exactMatches[i]) {
        // Exact name match — use existing entity directly
        entityNameToId[entity.name] = exactMatches[i]!.id;
      } else if (fuzzyCandidates[i] && mergeDecisions[entity.name]) {
        // Fuzzy match confirmed by LLM — merge
        const candidate = fuzzyCandidates[i]!;
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
        // No match — create new entity (need embedding)
        let embedding = entityEmbeddings[i];
        if (!embedding) {
          // Entity had an exact-match attempt but no embedding yet
          const text = entity.description ? `${entity.name}: ${entity.description}` : entity.name;
          [embedding] = await embedBatch([text]);
        }

        const { data: newEntity } = await supabase
          .from("entities")
          .insert({
            name: entity.name,
            entity_type: entity.type || "concept",
            description: entity.description || "",
            embedding,
          })
          .select()
          .single();

        if (newEntity) {
          entityNameToId[entity.name] = newEntity.id;
        }
      }
    }

    const exactCount = exactMatches.filter(Boolean).length;
    const mergedCount = Object.values(mergeDecisions).filter(Boolean).length;
    const createdCount = entities.length - exactCount - mergedCount;
    log.endStep("entity_resolution", "Entities resolved", {
      total: entities.length,
      exact: exactCount,
      merged: mergedCount,
      created: createdCount,
    });

    // 8. Store relations — dedup: skip if ANY relation exists between this pair
    log.startStep("relations");
    let relsSkipped = 0;
    for (const rel of relations) {
      const sourceEid = entityNameToId[rel.source];
      const targetEid = entityNameToId[rel.target];
      if (sourceEid && targetEid) {
        // Check if any relation already exists between this entity pair
        const { data: existingRel } = await supabase
          .from("relations")
          .select("id")
          .eq("source_entity", sourceEid)
          .eq("target_entity", targetEid)
          .limit(1);

        if (existingRel && existingRel.length > 0) {
          relsSkipped++;
          continue;
        }

        await supabase.from("relations").insert({
          source_entity: sourceEid,
          target_entity: targetEid,
          relation_type: rel.type || "related_to",
          description: rel.description || "",
          source_id: source.id,
        });
      }
    }
    log.endStep("relations", "Relations stored", {
      total: relations.length,
      skipped: relsSkipped,
      inserted: relations.length - relsSkipped,
    });

    // 9. Batch embed all observations (1 API call instead of N)
    log.startStep("observations");
    let obsSkipped = 0;
    if (observations.length > 0) {
      const obsTexts = observations.map(
        (o: { content: string }) => o.content,
      );
      const obsEmbeddings = await embedBatch(obsTexts, log);

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

    log.endStep("observations", "Observations stored", {
      total: observations.length,
      skipped: obsSkipped,
      inserted: observations.length - obsSkipped,
    });

    // 10. Mark source as extracted
    await supabase
      .from("sources")
      .update({ status: "extracted" })
      .eq("id", source.id);

    const result = {
      status: "success",
      source_id: source.id,
      entities_count: entities.length,
      relations_count: relations.length,
      relations_skipped: relsSkipped,
      observations_count: observations.length,
      observations_skipped: obsSkipped,
    };

    log.summary(result);
    return Response.json(result);
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    log.error("unhandled", err);
    log.summary({ status: "failed", error: message });
    return Response.json({ status: "failed", error: message }, { status: 500 });
  }
});
