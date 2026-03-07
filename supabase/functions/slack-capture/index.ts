// Open Brain Slack Capture — Supabase Edge Function
// Receives Slack events, forwards message text to the ingest function.

import { Logger } from "../_shared/logger.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const SLACK_BOT_TOKEN = Deno.env.get("SLACK_BOT_TOKEN")!;
const SLACK_SIGNING_SECRET = Deno.env.get("SLACK_SIGNING_SECRET") || "";

async function verifySlackSignature(
  rawBody: string,
  timestamp: string,
  signature: string,
): Promise<boolean> {
  if (!SLACK_SIGNING_SECRET) return true; // Skip if not configured
  const baseString = `v0:${timestamp}:${rawBody}`;
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(SLACK_SIGNING_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, encoder.encode(baseString));
  const hex = Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return signature === `v0=${hex}`;
}

Deno.serve(async (req) => {
  const log = new Logger("slack");

  // Read raw body for signature verification
  const rawBody = await req.text();
  const timestamp = req.headers.get("X-Slack-Request-Timestamp") || "";
  const signature = req.headers.get("X-Slack-Signature") || "";

  // Reject requests older than 5 minutes (replay protection)
  if (timestamp) {
    const age = Math.abs(Date.now() / 1000 - Number(timestamp));
    if (age > 300) {
      log.warn("auth", "Request too old", { age });
      return Response.json({ error: "Request too old" }, { status: 403 });
    }
  }

  if (SLACK_SIGNING_SECRET && !(await verifySlackSignature(rawBody, timestamp, signature))) {
    log.warn("auth", "Invalid Slack signature");
    return Response.json({ error: "Invalid signature" }, { status: 401 });
  }

  const body = JSON.parse(rawBody);

  // Slack URL verification challenge
  if (body.type === "url_verification") {
    return Response.json({ challenge: body.challenge });
  }

  // Handle events
  if (body.type === "event_callback") {
    const event = body.event;

    // Only process message events (not bot messages, not edits)
    if (
      event.type === "message" &&
      !event.bot_id &&
      !event.subtype
    ) {
      const text = event.text || "";
      const channel = event.channel || "";
      const user = event.user || "";
      const ts = event.ts || "";

      log.info("message", "Slack message received", { channel, user, textLength: text.length });

      if (text.trim()) {
        // Forward to ingest Edge Function
        log.startStep("ingest");
        const ingestRes = await fetch(`${SUPABASE_URL}/functions/v1/ingest`, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${SUPABASE_KEY}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            content: text,
            source_type: "slack",
            origin: `slack://${channel}/${ts}`,
            title: "",
            metadata: { channel, user, ts },
          }),
        });

        const result = await ingestRes.json();
        log.endStep("ingest", "Ingest complete", { status: result.status });

        // Reply in Slack with confirmation
        const entityCount = result.entities_count || 0;
        const obsCount = result.observations_count || 0;
        let reply = "";

        if (result.status === "success") {
          reply = `Captured. ${entityCount} entities, ${obsCount} observations extracted.`;
        } else if (result.status === "duplicate") {
          reply = "Already captured.";
        } else {
          reply = `Capture failed: ${result.error || "unknown error"}`;
          log.error("ingest", result.error || "unknown error");
        }

        await fetch("https://slack.com/api/chat.postMessage", {
          method: "POST",
          headers: {
            Authorization: `Bearer ${SLACK_BOT_TOKEN}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            channel,
            thread_ts: ts,
            text: reply,
          }),
        });
      }
    }
  }

  log.summary();
  return new Response("ok", { status: 200 });
});
