// Open Brain Slack Capture — Supabase Edge Function
// Receives Slack events, forwards message text to the ingest function.

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const SLACK_BOT_TOKEN = Deno.env.get("SLACK_BOT_TOKEN")!;

Deno.serve(async (req) => {
  const body = await req.json();

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

      if (text.trim()) {
        // Forward to ingest Edge Function
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

  return new Response("ok", { status: 200 });
});
