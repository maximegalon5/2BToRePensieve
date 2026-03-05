// Open Brain Email Capture — Supabase Edge Function
// Receives Resend inbound email webhooks, verifies Svix signature,
// fetches body via Resend API, extracts PDF attachments, then forwards to ingest.

import { extractText } from "https://esm.sh/unpdf@0.12";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const RESEND_API_KEY = Deno.env.get("RESEND_API_KEY") || "";
const RESEND_WEBHOOK_SECRET = Deno.env.get("RESEND_WEBHOOK_SECRET") || "";

// Max PDF file size to process (10 MB)
const MAX_PDF_BYTES = 10 * 1024 * 1024;

if (!RESEND_API_KEY) {
  console.warn("RESEND_API_KEY is not set — cannot fetch email body from Resend");
}
if (!RESEND_WEBHOOK_SECRET) {
  console.warn("RESEND_WEBHOOK_SECRET is not set — webhook signature verification disabled");
}

// --- Text chunking (mirrors open_brain/chunking.py) ---

function chunkText(text: string, maxChars = 10000): string[] {
  if (text.length <= maxChars) return [text];

  const chunks: string[] = [];
  let start = 0;

  while (start < text.length) {
    let end = Math.min(start + maxChars, text.length);

    if (end < text.length) {
      const window = text.slice(start, end);
      let bestCut = -1;
      for (const punct of [". ", "! ", "? ", ".\n", "!\n", "?\n"]) {
        const pos = window.lastIndexOf(punct);
        if (pos > bestCut && pos >= maxChars / 3) {
          bestCut = pos + punct.length;
        }
      }
      if (bestCut > 0) {
        end = start + bestCut;
      } else {
        const spacePos = window.lastIndexOf(" ");
        if (spacePos > maxChars / 3) {
          end = start + spacePos + 1;
        }
      }
    }

    const chunk = text.slice(start, end).trim();
    if (chunk) chunks.push(chunk);
    start = end;
  }

  return chunks;
}

// --- Svix webhook signature verification ---
// Resend uses Svix for webhook signing: HMAC-SHA256 with base64-encoded secret.
// Secret format: "whsec_<base64-encoded-key>"
// Signed content: "{svix-id}.{svix-timestamp}.{raw-body}"
// Signature header: "v1,<base64-signature>" (may have multiple space-separated)

async function verifySvixSignature(
  rawBody: string,
  headers: Headers,
): Promise<boolean> {
  const svixId = headers.get("svix-id");
  const svixTimestamp = headers.get("svix-timestamp");
  const svixSignature = headers.get("svix-signature");

  if (!svixId || !svixTimestamp || !svixSignature) {
    console.error("Missing Svix headers for webhook verification");
    return false;
  }

  // Reject timestamps older than 5 minutes to prevent replay attacks
  const now = Math.floor(Date.now() / 1000);
  const ts = parseInt(svixTimestamp, 10);
  if (isNaN(ts) || Math.abs(now - ts) > 300) {
    console.error("Svix timestamp too old or invalid:", svixTimestamp);
    return false;
  }

  // Strip "whsec_" prefix and base64-decode the secret
  const secretBase64 = RESEND_WEBHOOK_SECRET.startsWith("whsec_")
    ? RESEND_WEBHOOK_SECRET.slice(6)
    : RESEND_WEBHOOK_SECRET;
  const secretBytes = Uint8Array.from(atob(secretBase64), (c) => c.charCodeAt(0));

  // Construct signed content
  const signedContent = `${svixId}.${svixTimestamp}.${rawBody}`;

  // Compute HMAC-SHA256
  const key = await crypto.subtle.importKey(
    "raw",
    secretBytes,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signatureBytes = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(signedContent),
  );
  const expectedSig = "v1," + btoa(String.fromCharCode(...new Uint8Array(signatureBytes)));

  // Compare against all signatures in the header (space-separated)
  const signatures = svixSignature.split(" ");
  return signatures.some((sig) => sig === expectedSig);
}

// --- PDF extraction helper ---

interface AttachmentInfo {
  id: string;
  filename: string;
  content_type: string;
  size?: number;
  download_url: string | null;
  expires_at?: string;
}

async function extractPdfText(downloadUrl: string): Promise<{ text: string; totalPages: number } | null> {
  try {
    const pdfRes = await fetch(downloadUrl);
    if (!pdfRes.ok) {
      console.error("PDF download failed:", pdfRes.status);
      return null;
    }

    const pdfBuffer = await pdfRes.arrayBuffer();
    if (pdfBuffer.byteLength > MAX_PDF_BYTES) {
      console.warn("PDF too large, skipping:", pdfBuffer.byteLength, "bytes");
      return null;
    }

    const result = await extractText(new Uint8Array(pdfBuffer));
    const text = result.text?.trim() || "";
    return text ? { text, totalPages: result.totalPages || 0 } : null;
  } catch (err) {
    console.error("PDF text extraction error:", err);
    return null;
  }
}

async function ingestContent(
  content: string,
  sourceType: string,
  origin: string,
  title: string,
  metadata: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const res = await fetch(`${SUPABASE_URL}/functions/v1/ingest`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${SUPABASE_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      content,
      source_type: sourceType,
      origin,
      title,
      metadata,
    }),
  });
  return await res.json();
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

  if (req.method !== "POST") {
    return Response.json({ error: "Method not allowed" }, { status: 405 });
  }

  // Read raw body once (needed for both signature verification and JSON parsing)
  const rawBody = await req.text();

  // Verify Svix webhook signature
  if (RESEND_WEBHOOK_SECRET) {
    const valid = await verifySvixSignature(rawBody, req.headers);
    if (!valid) {
      return Response.json({ error: "Invalid webhook signature" }, { status: 401 });
    }
  }

  try {
    // Resend sends JSON webhook with email.received event
    const webhook = JSON.parse(rawBody);

    // Validate event type
    if (webhook.type !== "email.received") {
      return Response.json({ status: "skipped", reason: `unhandled event: ${webhook.type}` });
    }

    const data = webhook.data;
    if (!data?.email_id) {
      return Response.json({ status: "skipped", reason: "no email_id in webhook" });
    }

    const emailId = data.email_id as string;

    // Fetch full email content from Resend Receiving API
    if (!RESEND_API_KEY) {
      return Response.json(
        { status: "failed", error: "RESEND_API_KEY not configured" },
        { status: 500 },
      );
    }

    const emailRes = await fetch(
      `https://api.resend.com/emails/receiving/${emailId}`,
      {
        method: "GET",
        headers: { Authorization: `Bearer ${RESEND_API_KEY}` },
      },
    );

    if (!emailRes.ok) {
      const errBody = await emailRes.text();
      console.error("Resend API failed:", emailRes.status, errBody);
      return Response.json(
        { status: "failed", error: `Resend API ${emailRes.status}: ${errBody}` },
        { status: 502 },
      );
    }

    const email = await emailRes.json();

    // Extract all fields from the full API response
    const from = (email.from as string) || "";
    const to = Array.isArray(email.to) ? email.to.join(", ") : (email.to as string) || "";
    const cc = Array.isArray(email.cc) ? email.cc.join(", ") : (email.cc as string) || "";
    const bcc = Array.isArray(email.bcc) ? email.bcc.join(", ") : (email.bcc as string) || "";
    const replyTo = Array.isArray(email.reply_to) ? email.reply_to.join(", ") : (email.reply_to as string) || "";
    const subject = (email.subject as string) || "";
    const messageId = (email.message_id as string) || "";

    // Prefer plain text body, fall back to stripping HTML
    let body = (email.text || "").trim();
    if (!body && email.html) {
      body = email.html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
    }

    if (!body) {
      return Response.json({ status: "skipped", reason: "empty email body" });
    }

    // Fetch attachment download URLs (if any)
    const webhookAttachments = data.attachments || email.attachments || [];
    const attachments: AttachmentInfo[] = [];
    for (const att of webhookAttachments) {
      if (!att.id) continue;
      try {
        const attRes = await fetch(
          `https://api.resend.com/emails/receiving/${emailId}/attachments/${att.id}`,
          {
            method: "GET",
            headers: { Authorization: `Bearer ${RESEND_API_KEY}` },
          },
        );
        if (attRes.ok) {
          const attData = await attRes.json();
          attachments.push({
            id: attData.id,
            filename: attData.filename,
            content_type: attData.content_type,
            size: attData.size,
            download_url: attData.download_url,
            expires_at: attData.expires_at,
          });
        } else {
          // Still record what we know from the webhook metadata
          attachments.push({
            id: att.id,
            filename: att.filename,
            content_type: att.content_type,
            download_url: null,
          });
          console.error("Attachment fetch failed:", att.id, attRes.status);
        }
      } catch (err) {
        console.error("Attachment fetch error:", att.id, err);
        attachments.push({
          id: att.id,
          filename: att.filename,
          content_type: att.content_type,
          download_url: null,
        });
      }
    }

    // Append attachment summary to body so entities/observations can reference them
    if (attachments.length > 0) {
      const attSummary = attachments
        .map((a) => `[Attachment: ${a.filename} (${a.content_type})]`)
        .join("\n");
      body += `\n\n--- Attachments ---\n${attSummary}`;
    }

    // 1. Ingest the email body
    const emailResult = await ingestContent(
      body,
      "email",
      `email://${from}`,
      subject || "(no subject)",
      {
        from,
        to,
        cc: cc || undefined,
        bcc: bcc || undefined,
        reply_to: replyTo || undefined,
        subject,
        message_id: messageId || undefined,
        email_id: emailId,
        received_at: data.created_at || email.created_at || null,
        attachments: attachments.length > 0 ? attachments : undefined,
      },
    );

    // 2. Extract and ingest PDF attachments as separate sources
    const pdfResults: Record<string, unknown>[] = [];
    for (const att of attachments) {
      if (!att.download_url) continue;
      if (!att.content_type?.startsWith("application/pdf")) continue;
      if (att.size && att.size > MAX_PDF_BYTES) {
        console.warn("Skipping large PDF attachment:", att.filename, att.size, "bytes");
        continue;
      }

      console.log("Extracting PDF:", att.filename);
      const pdfData = await extractPdfText(att.download_url);
      if (!pdfData) continue;

      console.log(`  PDF text: ${pdfData.text.length} chars, ${pdfData.totalPages} pages`);

      // Chunk the PDF text for full extraction
      const chunks = chunkText(pdfData.text);
      console.log(`  Chunked into ${chunks.length} piece(s)`);

      for (let i = 0; i < chunks.length; i++) {
        const chunkLabel = chunks.length > 1 ? ` (chunk ${i + 1}/${chunks.length})` : "";
        const chunkOrigin = i === 0
          ? `email://${from}/attachment/${att.filename}`
          : `email://${from}/attachment/${att.filename}#chunk-${i + 1}`;

        const result = await ingestContent(
          chunks[i],
          "email_attachment",
          chunkOrigin,
          `${att.filename}${chunkLabel}`,
          {
            parent_email_id: emailId,
            parent_subject: subject,
            from,
            filename: att.filename,
            content_type: att.content_type,
            total_pages: pdfData.totalPages,
            chunk_index: i,
            total_chunks: chunks.length,
            chunk_chars: chunks[i].length,
            total_chars: pdfData.text.length,
          },
        );

        pdfResults.push({
          filename: att.filename,
          chunk: i + 1,
          total_chunks: chunks.length,
          status: result.status,
        });
        console.log(`  chunk ${i + 1}/${chunks.length}: ${result.status}`);
      }
    }

    return Response.json({
      ...emailResult,
      pdf_attachments: pdfResults.length > 0 ? pdfResults : undefined,
    });
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    return Response.json({ status: "failed", error: message }, { status: 500 });
  }
});
