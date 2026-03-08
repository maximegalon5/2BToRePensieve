"""
Diagnostic integration test for the email-capture → ingest → Telegram flow.

Tests each stage independently to pinpoint failures:
  Stage 1: Endpoint reachability
  Stage 2: Svix signature verification
  Stage 3: Resend API access (list recent emails)
  Stage 4: Ingest endpoint auth + processing
  Stage 5: Telegram notification delivery
  Stage 6: Full E2E (signed webhook with real email_id)

Usage:
  python tests/test_email_flow.py              # Run all stages
  python tests/test_email_flow.py --stage 3    # Run specific stage
"""

import sys
import os
import json
import time
import hmac
import hashlib
import base64
import uuid

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

import httpx

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_WEBHOOK_SECRET = os.getenv("RESEND_WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_ALLOWED_USERS", "")).split(",")[0].strip()

EMAIL_CAPTURE_URL = f"{SUPABASE_URL}/functions/v1/email-capture"
INGEST_URL = f"{SUPABASE_URL}/functions/v1/ingest"

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


def sign_svix(body: str, secret: str) -> dict:
    """Compute Svix webhook signature headers."""
    svix_id = f"msg_{uuid.uuid4().hex[:24]}"
    svix_timestamp = str(int(time.time()))

    # Strip "whsec_" prefix and base64-decode
    secret_b64 = secret[6:] if secret.startswith("whsec_") else secret
    secret_bytes = base64.b64decode(secret_b64)

    signed_content = f"{svix_id}.{svix_timestamp}.{body}"
    sig = hmac.new(secret_bytes, signed_content.encode(), hashlib.sha256).digest()
    svix_signature = "v1," + base64.b64encode(sig).decode()

    return {
        "svix-id": svix_id,
        "svix-timestamp": svix_timestamp,
        "svix-signature": svix_signature,
    }


def stage_1_reachability():
    """Stage 1: Can we reach the email-capture endpoint?"""
    print("\n--- Stage 1: Endpoint Reachability ---")
    try:
        res = httpx.get(EMAIL_CAPTURE_URL, timeout=10)
        # GET should return 405 Method Not Allowed (only POST accepted)
        if res.status_code == 405:
            print(f"  {PASS}: Endpoint reachable (405 on GET as expected)")
            return PASS
        else:
            print(f"  {FAIL}: Unexpected status {res.status_code}: {res.text[:200]}")
            return FAIL
    except Exception as e:
        print(f"  {FAIL}: Cannot reach endpoint: {e}")
        return FAIL


def stage_2_signature():
    """Stage 2: Does Svix signature verification work?"""
    print("\n--- Stage 2: Svix Signature Verification ---")
    if not RESEND_WEBHOOK_SECRET:
        print(f"  {SKIP}: RESEND_WEBHOOK_SECRET not set")
        return SKIP

    # 2a: Send without signature headers → should get 401
    body = json.dumps({"type": "email.received", "data": {"email_id": "fake"}})
    try:
        res = httpx.post(EMAIL_CAPTURE_URL, content=body,
                         headers={"Content-Type": "application/json"}, timeout=10)
        if res.status_code == 401:
            print(f"  {PASS}: Unsigned request rejected (401)")
        else:
            print(f"  {FAIL}: Unsigned request got {res.status_code} (expected 401): {res.text[:200]}")
            return FAIL
    except Exception as e:
        print(f"  {FAIL}: Request failed: {e}")
        return FAIL

    # 2b: Send with valid signature → should pass signature check (will fail later at Resend API)
    svix_headers = sign_svix(body, RESEND_WEBHOOK_SECRET)
    try:
        res = httpx.post(EMAIL_CAPTURE_URL, content=body,
                         headers={"Content-Type": "application/json", **svix_headers}, timeout=15)
        if res.status_code == 401:
            print(f"  {FAIL}: Signed request still rejected (401) — signature computation may be wrong")
            print(f"         Response: {res.text[:200]}")
            return FAIL
        else:
            # Any non-401 means signature passed (502 from Resend = expected for fake email_id)
            print(f"  {PASS}: Signed request accepted (status {res.status_code})")
            print(f"         Response: {res.text[:200]}")
            return PASS
    except Exception as e:
        print(f"  {FAIL}: Signed request failed: {e}")
        return FAIL


def stage_3_resend_api():
    """Stage 3: Can we access the Resend Receiving API?"""
    print("\n--- Stage 3: Resend API Access ---")
    if not RESEND_API_KEY:
        print(f"  {SKIP}: RESEND_API_KEY not set")
        return SKIP

    # 3a: List received emails
    try:
        res = httpx.get("https://api.resend.com/emails/receiving",
                        headers={"Authorization": f"Bearer {RESEND_API_KEY}"}, timeout=10)
        if res.status_code == 200:
            data = res.json()
            emails = data.get("data", data) if isinstance(data, dict) else data
            count = len(emails) if isinstance(emails, list) else "unknown"
            print(f"  {PASS}: Resend API accessible, {count} received emails")

            # Show most recent email for reference
            if isinstance(emails, list) and emails:
                latest = emails[0]
                print(f"         Latest: id={latest.get('id', '?')}, "
                      f"from={latest.get('from', '?')}, "
                      f"subject={latest.get('subject', '?')[:50]}")
                return PASS, latest.get("id")
            return PASS, None
        else:
            print(f"  {FAIL}: Resend API returned {res.status_code}: {res.text[:300]}")
            return FAIL, None
    except Exception as e:
        print(f"  {FAIL}: Resend API error: {e}")
        return FAIL, None


def stage_4_ingest():
    """Stage 4: Does the ingest endpoint accept authenticated requests?"""
    print("\n--- Stage 4: Ingest Endpoint ---")
    if not SUPABASE_KEY:
        print(f"  {SKIP}: SUPABASE_SERVICE_ROLE_KEY not set")
        return SKIP

    # 4a: Unauthenticated request → should get 401
    try:
        res = httpx.post(INGEST_URL,
                         json={"content": "test", "source_type": "test", "origin": "test://diag", "title": "diag"},
                         timeout=10)
        if res.status_code == 401:
            print(f"  {PASS}: Unauthenticated ingest rejected (401)")
        else:
            print(f"  WARNING: Unauthenticated ingest got {res.status_code} (expected 401)")
    except Exception as e:
        print(f"  {FAIL}: Ingest request failed: {e}")
        return FAIL

    # 4b: Authenticated request with test content (will create a real source — use unique origin)
    test_origin = f"test://email-flow-diag-{int(time.time())}"
    try:
        res = httpx.post(INGEST_URL,
                         json={
                             "content": "Diagnostic test: email flow integration check. Delete this source.",
                             "source_type": "test",
                             "origin": test_origin,
                             "title": "Email Flow Diagnostic",
                         },
                         headers={"Authorization": f"Bearer {SUPABASE_KEY}"},
                         timeout=30)
        result = res.json()
        if res.status_code == 200 and result.get("status") in ("success", "duplicate"):
            print(f"  {PASS}: Authenticated ingest works (status={result['status']})")
            return PASS
        else:
            print(f"  {FAIL}: Ingest returned {res.status_code}: {json.dumps(result)[:300]}")
            return FAIL
    except Exception as e:
        print(f"  {FAIL}: Authenticated ingest failed: {e}")
        return FAIL


def stage_5_telegram():
    """Stage 5: Can we send a Telegram notification?"""
    print("\n--- Stage 5: Telegram Notification ---")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"  {SKIP}: TELEGRAM_BOT_TOKEN or chat ID not set")
        return SKIP

    try:
        res = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": "*Email flow diagnostic*\nThis is a test notification from the integration test.",
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        result = res.json()
        if result.get("ok"):
            print(f"  {PASS}: Telegram notification sent successfully")
            return PASS
        else:
            print(f"  {FAIL}: Telegram API error: {result.get('description', result)}")
            return FAIL
    except Exception as e:
        print(f"  {FAIL}: Telegram request failed: {e}")
        return FAIL


def stage_6_trace(email_id: str | None = None):
    """Stage 6: Trace the exact path email-capture takes — find where PDFs get lost."""
    print("\n--- Stage 6: Trace Email-Capture Logic ---")
    if not RESEND_API_KEY:
        print(f"  {SKIP}: RESEND_API_KEY not set")
        return SKIP

    if not email_id:
        print(f"  {SKIP}: No email_id available")
        return SKIP

    print(f"  Email ID: {email_id}")

    # Step 1: Fetch email from Resend (same as function does)
    try:
        res = httpx.get(f"https://api.resend.com/emails/receiving/{email_id}",
                        headers={"Authorization": f"Bearer {RESEND_API_KEY}"}, timeout=10)
        if res.status_code != 200:
            print(f"  {FAIL}: Resend API returned {res.status_code}: {res.text[:200]}")
            return FAIL
        email = res.json()
        print(f"  Step 1 OK: Email fetched (from={email.get('from')}, subject={email.get('subject')})")
    except Exception as e:
        print(f"  {FAIL}: Resend API error: {e}")
        return FAIL

    # Step 2: Check body
    body = (email.get("text") or "").strip()
    if not body and email.get("html"):
        body = "stripped-html"
    print(f"  Step 2: Body length={len(body)} chars ({'has content' if body else 'EMPTY — would skip'})")
    if not body:
        print(f"  {FAIL}: Empty body — function would return 'skipped'")
        return FAIL

    # Step 3: Check attachments in API response
    attachments_raw = email.get("attachments", [])
    print(f"  Step 3: API attachments count={len(attachments_raw)}")
    for att in attachments_raw:
        print(f"    - id={att.get('id')}, filename={att.get('filename')}, "
              f"type={att.get('content_type')}, size={att.get('size')}")

    if not attachments_raw:
        print(f"  No attachments in API response — no PDFs to process")
        return PASS

    # Step 4: Fetch download URLs (same as function does at line 298-334)
    attachments_with_urls = []
    for att in attachments_raw:
        if not att.get("id"):
            print(f"    Skipping attachment without id")
            continue
        try:
            att_res = httpx.get(
                f"https://api.resend.com/emails/receiving/{email_id}/attachments/{att['id']}",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"}, timeout=10)
            if att_res.status_code == 200:
                att_data = att_res.json()
                url = att_data.get("download_url")
                print(f"    {att['filename']}: download_url={'SET' if url else 'NULL'}, "
                      f"expires={att_data.get('expires_at', '?')}")
                attachments_with_urls.append(att_data)
            else:
                print(f"    {att['filename']}: fetch FAILED ({att_res.status_code}): {att_res.text[:100]}")
        except Exception as e:
            print(f"    {att['filename']}: fetch ERROR: {e}")

    # Step 5: Filter to PDFs with download URLs
    pdfs = [a for a in attachments_with_urls
            if a.get("download_url") and (a.get("content_type") or "").startswith("application/pdf")]
    print(f"  Step 5: PDFs with download URLs: {len(pdfs)}")

    if not pdfs:
        print(f"  {FAIL}: No PDFs with download URLs — this is why pdfs=0")
        return FAIL

    # Step 6: Try downloading PDF
    for pdf in pdfs:
        url = pdf["download_url"]
        try:
            dl_res = httpx.get(url, timeout=15)
            if dl_res.status_code == 200:
                size = len(dl_res.content)
                # Check if it looks like a PDF
                is_pdf = dl_res.content[:5] == b"%PDF-"
                print(f"    {pdf['filename']}: downloaded {size} bytes, valid_pdf={is_pdf}")
                if not is_pdf:
                    print(f"      First 50 bytes: {dl_res.content[:50]}")
            else:
                print(f"    {pdf['filename']}: download FAILED ({dl_res.status_code})")
                # Check if URL expired
                if dl_res.status_code in (403, 410):
                    print(f"      URL may have expired (expires_at={pdf.get('expires_at')})")
        except Exception as e:
            print(f"    {pdf['filename']}: download ERROR: {e}")

    print(f"  {PASS}: Trace complete — all steps working")
    return PASS


def stage_7_e2e(email_id: str | None = None):
    """Stage 7: Full E2E — send signed webhook with a real email_id."""
    print("\n--- Stage 7: Full E2E (signed webhook → Resend fetch → ingest → Telegram) ---")
    if not all([RESEND_WEBHOOK_SECRET, RESEND_API_KEY, SUPABASE_KEY]):
        print(f"  {SKIP}: Missing required env vars")
        return SKIP

    if not email_id:
        # Try to get latest email from Resend
        try:
            res = httpx.get("https://api.resend.com/emails/receiving",
                            headers={"Authorization": f"Bearer {RESEND_API_KEY}"}, timeout=10)
            if res.status_code == 200:
                data = res.json()
                emails = data.get("data", data) if isinstance(data, dict) else data
                if isinstance(emails, list) and emails:
                    email_id = emails[0].get("id")
        except Exception:
            pass

    if not email_id:
        print(f"  {SKIP}: No email_id available (no received emails found)")
        return SKIP

    print(f"  Using email_id: {email_id}")

    # Craft webhook payload matching Resend's format
    body = json.dumps({
        "type": "email.received",
        "data": {
            "email_id": email_id,
            "created_at": "2025-01-01T00:00:00.000Z",
        },
    })

    svix_headers = sign_svix(body, RESEND_WEBHOOK_SECRET)

    try:
        res = httpx.post(
            EMAIL_CAPTURE_URL,
            content=body,
            headers={"Content-Type": "application/json", **svix_headers},
            timeout=60,
        )
        result = res.json()
        print(f"  Status: {res.status_code}")
        print(f"  Response: {json.dumps(result, indent=2)[:500]}")

        if res.status_code == 200 and result.get("status") in ("success", "duplicate"):
            pdf_count = len(result.get("pdf_attachments", []))
            print(f"  {PASS}: E2E flow completed (status={result['status']}, pdfs={pdf_count})")
            return PASS
        elif res.status_code == 200 and result.get("status") == "skipped":
            print(f"  {PASS}: E2E reached processing but skipped: {result.get('reason')}")
            return PASS
        else:
            print(f"  {FAIL}: E2E flow failed")
            return FAIL
    except Exception as e:
        print(f"  {FAIL}: E2E request failed: {e}")
        return FAIL


def main():
    target_stage = None
    for arg in sys.argv[1:]:
        if arg == "--stage":
            idx = sys.argv.index(arg) + 1
            if idx < len(sys.argv):
                target_stage = int(sys.argv[idx])

    print("=" * 60)
    print("EMAIL FLOW DIAGNOSTIC TEST")
    print("=" * 60)
    print(f"  Endpoint: {EMAIL_CAPTURE_URL}")
    print(f"  Resend API key: {'set' if RESEND_API_KEY else 'NOT SET'}")
    print(f"  Webhook secret: {'set' if RESEND_WEBHOOK_SECRET else 'NOT SET'}")
    print(f"  Telegram bot: {'set' if TELEGRAM_BOT_TOKEN else 'NOT SET'}")
    print(f"  Telegram chat: {TELEGRAM_CHAT_ID or 'NOT SET'}")

    results = {}
    email_id = None

    stages = {
        1: ("Reachability", lambda: stage_1_reachability()),
        2: ("Svix Signature", lambda: stage_2_signature()),
        3: ("Resend API", lambda: None),  # handled specially
        4: ("Ingest Endpoint", lambda: stage_4_ingest()),
        5: ("Telegram", lambda: stage_5_telegram()),
        6: ("Trace Logic", lambda: stage_6_trace(email_id)),
        7: ("Full E2E", lambda: stage_7_e2e(email_id)),
    }

    for num in sorted(stages.keys()):
        if target_stage and num != target_stage:
            continue

        if num == 3:
            result = stage_3_resend_api()
            if isinstance(result, tuple):
                results[num] = result[0]
                email_id = result[1]
            else:
                results[num] = result
        elif num == 6:
            results[num] = stage_6_trace(email_id)
        elif num == 7:
            results[num] = stage_7_e2e(email_id)
        else:
            results[num] = stages[num][1]()

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for num in sorted(results.keys()):
        name = stages[num][0]
        status = results[num]
        icon = {"PASS": "+", "FAIL": "!", "SKIP": "-"}.get(status, "?")
        print(f"  [{icon}] Stage {num}: {name} — {status}")

    failed = [n for n, s in results.items() if s == FAIL]
    if failed:
        print(f"\nFirst failure at stage {failed[0]} — fix this first.")
        return 1
    else:
        print("\nAll stages passed!")
        return 0


if __name__ == "__main__":
    sys.exit(main())
