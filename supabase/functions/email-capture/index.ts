// Open Brain Email Capture — Supabase Edge Function
// Receives Resend inbound email webhooks, verifies Svix signature,
// fetches body via Resend API, extracts PDF attachments, then forwards to ingest.
// Sends Telegram notification after PDF processing.

import { Logger } from "../_shared/logger.ts";

const OPENAI_API_KEY = Deno.env.get("OPENAI_API_KEY") || "";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const RESEND_API_KEY = Deno.env.get("RESEND_API_KEY") || "";
const RESEND_WEBHOOK_SECRET = Deno.env.get("RESEND_WEBHOOK_SECRET") || "";
const TELEGRAM_BOT_TOKEN = Deno.env.get("TELEGRAM_BOT_TOKEN") || "";
const TELEGRAM_NOTIFY_CHAT_ID = (Deno.env.get("TELEGRAM_ALLOWED_USERS") || "").split(",")[0]?.trim() || "";

// Max PDF file size to process (10 MB)
const MAX_PDF_BYTES = 10 * 1024 * 1024;

if (!RESEND_API_KEY) {
  console.warn("RESEND_API_KEY is not set — cannot fetch email body from Resend");
}
if (!RESEND_WEBHOOK_SECRET) {
  console.warn("RESEND_WEBHOOK_SECRET is not set — webhook signature verification disabled");
}

// --- Telegram notification ---

async function notifyTelegram(text: string) {
  if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_NOTIFY_CHAT_ID) return;
  try {
    await fetch(
      `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          chat_id: TELEGRAM_NOTIFY_CHAT_ID,
          text,
          parse_mode: "Markdown",
        }),
      },
    );
  } catch (err) {
    console.error("Telegram notification failed:", err);
  }
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

// --- PDF text extraction (tiered: unpdf → OpenAI fallback) ---

async function extractPdfWithUnpdf(data: Uint8Array): Promise<{ text: string; totalPages: number } | null> {
  try {
    const { getDocumentProxy, extractText } = await import("npm:unpdf@1.4.0");
    console.log("PDF [unpdf]: loaded unpdf@1.4.0, creating document proxy...");
    const pdf = await getDocumentProxy(data);
    console.log("PDF [unpdf]: proxy created, extracting text...");
    const result = await extractText(pdf, { mergePages: true });
    const text = (result.text || "").trim();
    console.log("PDF [unpdf]: extracted", text.length, "chars,", result.totalPages, "pages");
    if (text.length < 50) {
      console.warn("PDF [unpdf]: text too short (", text.length, "chars), likely extraction failure");
      return null;
    }
    return { text, totalPages: result.totalPages || 0 };
  } catch (err) {
    const msg = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
    console.error("PDF [unpdf]: failed —", msg);
    return null;
  }
}

async function extractPdfWithOpenAI(data: Uint8Array, filename: string): Promise<{ text: string; totalPages: number } | null> {
  if (!OPENAI_API_KEY) {
    console.warn("PDF [openai]: OPENAI_API_KEY not set, skipping fallback");
    return null;
  }
  try {
    console.log("PDF [openai]: sending", data.byteLength, "bytes to GPT-4o-mini...");
    const base64 = btoa(String.fromCharCode(...data));
    const res = await fetch("https://api.openai.com/v1/chat/completions", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${OPENAI_API_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: "gpt-4o-mini",
        messages: [{
          role: "user",
          content: [
            {
              type: "file",
              file: { data: base64, filename },
            },
            {
              type: "text",
              text: "Extract all text content from this PDF document. Return only the extracted text, preserving the original structure and formatting. Do not add any commentary.",
            },
          ],
        }],
        max_tokens: 16000,
      }),
    });

    if (!res.ok) {
      const errBody = await res.text().catch(() => "");
      console.error("PDF [openai]: API error", res.status, errBody.slice(0, 300));
      return null;
    }

    const result = await res.json();
    const text = result.choices?.[0]?.message?.content?.trim() || "";
    const usage = result.usage;
    console.log("PDF [openai]: extracted", text.length, "chars (tokens:", usage?.total_tokens || "?", ")");

    if (!text || text.length < 50) {
      console.warn("PDF [openai]: insufficient text extracted (", text.length, "chars)");
      return null;
    }

    return { text, totalPages: 0 }; // OpenAI doesn't report page count
  } catch (err) {
    const msg = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
    console.error("PDF [openai]: failed —", msg);
    return null;
  }
}

async function extractPdfText(downloadUrl: string, filename: string): Promise<{ text: string; totalPages: number; method: string } | null> {
  // Step 1: Download the PDF
  let pdfBuffer: ArrayBuffer;
  try {
    const pdfRes = await fetch(downloadUrl);
    if (!pdfRes.ok) {
      const errBody = await pdfRes.text().catch(() => "");
      console.error("PDF download failed:", pdfRes.status, errBody.slice(0, 200));
      return null;
    }
    pdfBuffer = await pdfRes.arrayBuffer();
    console.log("PDF: downloaded", pdfBuffer.byteLength, "bytes");
  } catch (err) {
    console.error("PDF download error:", err instanceof Error ? err.message : err);
    return null;
  }

  if (pdfBuffer.byteLength > MAX_PDF_BYTES) {
    console.warn("PDF too large, skipping:", pdfBuffer.byteLength, "bytes");
    return null;
  }

  const data = new Uint8Array(pdfBuffer);

  // Step 2: Try unpdf (fast, free, local)
  const unpdfResult = await extractPdfWithUnpdf(data);
  if (unpdfResult) {
    return { ...unpdfResult, method: "unpdf" };
  }

  // Step 3: Fallback to OpenAI (reliable, costs tokens)
  console.log("PDF: unpdf failed, falling back to OpenAI...");
  const openaiResult = await extractPdfWithOpenAI(data, filename);
  if (openaiResult) {
    return { ...openaiResult, method: "openai" };
  }

  console.error("PDF: all extraction methods failed for", filename);
  return null;
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

  const text = await res.text();
  let result: Record<string, unknown>;
  try {
    result = JSON.parse(text);
  } catch {
    console.error("Ingest returned non-JSON:", res.status, text.slice(0, 500));
    return { status: "failed", error: `Ingest non-JSON response (${res.status}): ${text.slice(0, 200)}` };
  }

  if (res.status !== 200 || result.status === "failed") {
    console.error("Ingest failed:", res.status, JSON.stringify(result).slice(0, 500));
  }
  return result;
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

  const log = new Logger("email");

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
    log.info("request", "Email webhook received", { emailId });

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

    log.info("email_parsed", "Email fetched and parsed", {
      from,
      subject,
      bodyLength: body.length,
      attachmentCount: attachments.length,
      pdfCount: attachments.filter(a => a.content_type?.startsWith("application/pdf")).length,
    });

    // 1. Ingest the email body
    log.startStep("ingest_body");
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

    log.endStep("ingest_body", "Email body ingested", { status: emailResult.status });

    // 2. Extract and ingest PDF attachments as separate sources
    const pdfResults: Record<string, unknown>[] = [];
    log.info("pdf_scan", "Scanning attachments for PDFs", {
      totalAttachments: attachments.length,
      withUrls: attachments.filter(a => a.download_url).length,
      pdfs: attachments.filter(a => a.content_type?.startsWith("application/pdf")).length,
    });
    for (const att of attachments) {
      if (!att.download_url) continue;
      if (!att.content_type?.startsWith("application/pdf")) continue;
      if (att.size && att.size > MAX_PDF_BYTES) {
        console.warn("Skipping large PDF attachment:", att.filename, att.size, "bytes");
        continue;
      }

      log.startStep(`pdf_${att.filename}`);
      const pdfData = await extractPdfText(att.download_url, att.filename);
      if (!pdfData) {
        log.failStep(`pdf_${att.filename}`, "All PDF extraction methods failed");
        continue;
      }

      log.info(`pdf_${att.filename}`, "PDF text extracted", {
        method: pdfData.method,
        chars: pdfData.text.length,
        pages: pdfData.totalPages,
      });

      // Chunk the PDF text for full extraction
      const chunks = chunkText(pdfData.text);

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
          entities: result.entities_count || 0,
          observations: result.observations_count || 0,
          error: result.status === "failed" ? result.error : undefined,
        });
      }

      log.endStep(`pdf_${att.filename}`, "PDF ingested", {
        chunks: chunks.length,
        pages: pdfData.totalPages,
        chars: pdfData.text.length,
      });
    }

    // 3. Send Telegram notification for processed PDFs
    if (pdfResults.length > 0) {
      const totalEntities = pdfResults.reduce((sum, r) => sum + ((r.entities as number) || 0), 0);
      const totalObs = pdfResults.reduce((sum, r) => sum + ((r.observations as number) || 0), 0);
      const pdfSummaries = [...new Set(pdfResults.map(r => r.filename))].map(filename => {
        const chunks = pdfResults.filter(r => r.filename === filename);
        const totalChunks = (chunks[0]?.total_chunks as number) || 1;
        return `  *${filename}* (${totalChunks} chunk${totalChunks > 1 ? "s" : ""})`;
      });

      const msg = [
        `*PDF processed from email*`,
        `From: ${from}`,
        `Subject: ${subject || "(no subject)"}`,
        ``,
        ...pdfSummaries,
        ``,
        `Extracted: ${totalEntities} entities, ${totalObs} observations`,
      ].join("\n");

      await notifyTelegram(msg);
      log.info("notification", "Telegram notification sent");
    }

    // Determine overall status: success if either email or PDFs succeeded
    const pdfSuccessCount = pdfResults.filter(r => r.status === "success").length;
    const overallStatus = (emailResult.status === "success" || pdfSuccessCount > 0)
      ? "success" : (emailResult.status as string);

    log.summary({
      emailStatus: emailResult.status,
      emailError: emailResult.status === "failed" ? emailResult.error : undefined,
      pdfCount: pdfResults.length,
      pdfSuccessCount,
    });

    return Response.json({
      status: overallStatus,
      email: emailResult,
      pdf_attachments: pdfResults.length > 0 ? pdfResults : undefined,
      _debug: {
        attachments_found: attachments.length,
        attachments_with_urls: attachments.filter(a => a.download_url).length,
        pdf_attachments_found: attachments.filter(a => a.content_type?.startsWith("application/pdf") && a.download_url).length,
        pdf_results_count: pdfResults.length,
        telegram_configured: !!(TELEGRAM_BOT_TOKEN && TELEGRAM_NOTIFY_CHAT_ID),
      },
    });
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    log.error("unhandled", err);
    log.summary({ status: "failed", error: message });
    return Response.json({ status: "failed", error: message }, { status: 500 });
  }
});
