"""Fetch YouTube video transcripts and ingest into knowledge graph.

Supports single videos and full playlists with daily sync.
Long transcripts are automatically chunked (~10k chars) so each chunk gets
full LLM extraction. Every chunk links back to the source video via origin URL.

Usage:
    # Single video
    python -m open_brain.connectors.youtube https://www.youtube.com/watch?v=VIDEO_ID

    # Full playlist (skips already-ingested videos)
    python -m open_brain.connectors.youtube --playlist https://www.youtube.com/playlist?list=PLAYLIST_ID

    # Playlist with daily sync (only new videos)
    python -m open_brain.connectors.youtube --playlist https://www.youtube.com/playlist?list=PLAYLIST_ID --sync

    # Dry run
    python -m open_brain.connectors.youtube --playlist URL --dry-run
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time

# Force UTF-8 stdout on Windows (video titles may contain emoji/unicode)
if sys.platform == "win32" and not isinstance(sys.stdout, io.TextIOWrapper):
    pass  # already wrapped
elif sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from youtube_transcript_api import YouTubeTranscriptApi
from openai import OpenAI

from open_brain.chunking import chunk_text
from open_brain.config import load_open_brain_config
from open_brain.db import get_client
from open_brain.embeddings import get_cloud_embedder
from open_brain.ingest import ingest_content


def extract_video_id(url: str) -> str:
    """Extract video ID from various YouTube URL formats."""
    patterns = [
        r'(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise ValueError(f"Could not extract video ID from: {url}")


def extract_playlist_id(url: str) -> str:
    """Extract playlist ID from YouTube URL."""
    match = re.search(r'[?&]list=([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)
    raise ValueError(f"Could not extract playlist ID from: {url}")


def fetch_transcript(video_id: str, cookies: str | None = None) -> str:
    """Fetch and concatenate transcript text.

    Args:
        video_id: YouTube video ID.
        cookies: Path to a Netscape-format cookies.txt file for authentication.
                 Helps avoid IP bans on shared infrastructure (e.g. GitHub Actions).
    """
    api = YouTubeTranscriptApi(cookies=cookies) if cookies else YouTubeTranscriptApi()
    transcript = api.fetch(video_id)
    return " ".join(entry.text for entry in transcript)


def chunk_transcript(text: str, max_chars: int = 10000) -> list[str]:
    """Split a long transcript into chunks at sentence boundaries.

    Delegates to open_brain.chunking.chunk_text for the shared implementation.
    """
    return chunk_text(text, max_chars=max_chars)


def get_playlist_videos_ytdlp(playlist_url: str) -> list[dict]:
    """Get all videos in a playlist using yt-dlp Python API (most reliable)."""
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp not found. Install with: pip install yt-dlp")

    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(playlist_url, download=False)
        entries = info.get("entries", [])

    videos = []
    for entry in entries:
        vid = entry.get("id", "")
        if vid:
            videos.append({
                "id": vid,
                "title": entry.get("title", ""),
                "url": f"https://www.youtube.com/watch?v={vid}",
            })
    return videos


def get_already_ingested(db_client, playlist_id: str) -> set[str]:
    """Get set of video IDs already ingested from this playlist."""
    result = db_client.table("sources") \
        .select("origin") \
        .eq("source_type", "youtube") \
        .like("origin", "https://www.youtube.com/watch%") \
        .execute()

    ingested = set()
    for row in result.data or []:
        match = re.search(r'v=([a-zA-Z0-9_-]{11})', row.get("origin", ""))
        if match:
            ingested.add(match.group(1))
    return ingested


def update_sync_state(db_client, sync_id: str, metadata: dict | None = None):
    """Update sync state cursor for daily sync tracking."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    existing = db_client.table("sync_state").select("id").eq("id", sync_id).execute()
    if existing.data:
        db_client.table("sync_state").update({
            "last_synced_at": now,
            "metadata": metadata or {},
        }).eq("id", sync_id).execute()
    else:
        db_client.table("sync_state").insert({
            "id": sync_id,
            "last_synced_at": now,
            "metadata": metadata or {},
        }).execute()


def ingest_single_video(
    db_client, embed_client, embed_model, chat_client, chat_model,
    video_id: str, title: str = "", dry_run: bool = False,
    max_chunk_chars: int = 10000, cookies: str | None = None,
) -> dict:
    """Ingest a single YouTube video transcript, chunking long transcripts.

    Long transcripts are split into ~10k char chunks so each gets full
    LLM extraction. Every chunk links back to the source video via origin URL
    and metadata, so knowledge can always be traced to the original video.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        transcript = fetch_transcript(video_id, cookies=cookies)
    except Exception as e:
        return {"status": "failed", "error": f"Transcript fetch failed: {e}"}

    if not transcript or len(transcript) < 50:
        return {"status": "skipped", "reason": "transcript too short"}

    video_title = title or f"YouTube: {video_id}"
    chunks = chunk_transcript(transcript, max_chunk_chars)

    if dry_run:
        return {
            "status": "dry_run",
            "title": video_title,
            "chars": len(transcript),
            "chunks": len(chunks),
        }

    total_entities = 0
    total_relations = 0
    total_observations = 0
    chunk_results = []

    for i, chunk in enumerate(chunks):
        chunk_label = f" (chunk {i+1}/{len(chunks)})" if len(chunks) > 1 else ""
        # First/only chunk uses clean URL; subsequent chunks add fragment
        chunk_origin = url if i == 0 else f"{url}#chunk-{i+1}"
        chunk_title = f"{video_title}{chunk_label}"

        result = ingest_content(
            supabase_client=db_client,
            embed_client=embed_client,
            embed_model=embed_model,
            chat_client=chat_client,
            chat_model=chat_model,
            content=chunk,
            source_type="youtube",
            origin=chunk_origin,
            title=chunk_title,
            metadata={
                "video_id": video_id,
                "youtube_url": url,
                "chunk_index": i,
                "total_chunks": len(chunks),
                "chunk_chars": len(chunk),
                "total_chars": len(transcript),
            },
        )

        status = result.get("status", "unknown")
        chunk_results.append(result)

        if status == "success":
            total_entities += result.get("entities_count", 0)
            total_relations += result.get("relations_count", 0)
            total_observations += result.get("observations_count", 0)
            if len(chunks) > 1:
                print(f"      chunk {i+1}/{len(chunks)}: {result.get('entities_count', 0)} entities, {result.get('relations_count', 0)} rels")
        elif status == "duplicate":
            if len(chunks) > 1:
                print(f"      chunk {i+1}/{len(chunks)}: DUP")
        elif status == "failed":
            if len(chunks) > 1:
                print(f"      chunk {i+1}/{len(chunks)}: FAIL — {result.get('error', '')[:80]}")

    # Return aggregated result
    any_success = any(r.get("status") == "success" for r in chunk_results)
    all_dup = all(r.get("status") == "duplicate" for r in chunk_results)

    if all_dup:
        return {"status": "duplicate"}

    return {
        "status": "success" if any_success else "failed",
        "entities_count": total_entities,
        "relations_count": total_relations,
        "observations_count": total_observations,
        "chunks_processed": len(chunks),
        "transcript_chars": len(transcript),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest YouTube video/playlist into Open Brain.")
    ap.add_argument("url", nargs="?", help="YouTube video URL or video ID (for single video mode)")
    ap.add_argument("--playlist", help="YouTube playlist URL to ingest all videos")
    ap.add_argument("--title", default="", help="Override video title (single video mode only)")
    ap.add_argument("--limit", type=int, default=0, help="Max videos to process (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be ingested")
    ap.add_argument("--delay", type=float, default=2.0, help="Seconds between API calls")
    ap.add_argument("--sync", action="store_true", help="Enable sync tracking (skip already ingested)")
    ap.add_argument("--cookies", default=None, help="Path to Netscape-format cookies.txt for YouTube auth (avoids IP bans)")
    ap.add_argument("--newest-first", action="store_true", help="Process newest playlist videos first (default: oldest first)")
    args = ap.parse_args()

    if not args.url and not args.playlist:
        ap.error("Provide a video URL or --playlist URL")

    cfg = load_open_brain_config()

    if not args.dry_run:
        db_client = get_client(cfg)
        embed_client, embed_model = get_cloud_embedder(cfg)
        chat_client = OpenAI(base_url=cfg.openrouter.base_url, api_key=cfg.openrouter.api_key)

    # --- Single video mode ---
    if args.url and not args.playlist:
        video_id = extract_video_id(args.url)
        print(f"Ingesting single video: {video_id}")

        result = ingest_single_video(
            db_client if not args.dry_run else None,
            embed_client if not args.dry_run else None,
            embed_model if not args.dry_run else None,
            chat_client if not args.dry_run else None,
            cfg.openrouter.chat_model if not args.dry_run else None,
            video_id=video_id,
            title=args.title,
            dry_run=args.dry_run,
            cookies=args.cookies,
        )

        status = result.get("status", "unknown")
        if status == "dry_run":
            chunks = result.get("chunks", 1)
            print(f"[DRY RUN] Would ingest: {result['title']} ({result['chars']} chars, {chunks} chunk{'s' if chunks > 1 else ''})")
        elif status == "success":
            chunks = result.get("chunks_processed", 1)
            print(f"Result: SUCCESS ({chunks} chunk{'s' if chunks > 1 else ''})")
            print(f"  Entities: {result['entities_count']}")
            print(f"  Relations: {result['relations_count']}")
            print(f"  Observations: {result['observations_count']}")
        elif status == "duplicate":
            print("Result: DUPLICATE (already ingested)")
        else:
            print(f"Result: {status} — {result.get('error', '')}")
        return 0

    # --- Playlist mode ---
    playlist_url = args.playlist
    playlist_id = extract_playlist_id(playlist_url)
    sync_id = f"youtube_playlist_{playlist_id}"

    print(f"Fetching playlist videos: {playlist_url}")
    videos = get_playlist_videos_ytdlp(playlist_url)
    print(f"Found {len(videos)} videos in playlist")

    # Check which are already ingested
    already_ingested: set[str] = set()
    if not args.dry_run:
        already_ingested = get_already_ingested(db_client, playlist_id)
        print(f"Already ingested: {len(already_ingested)} videos")

    new_videos = [v for v in videos if v["id"] not in already_ingested]
    if args.newest_first:
        new_videos = list(reversed(new_videos))
    print(f"New to ingest: {len(new_videos)} videos{' (newest first)' if args.newest_first else ''}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE INGEST'}")
    print()

    n_processed = 0
    n_success = 0
    n_dup = 0
    n_failed = 0
    n_skipped = 0
    total_chunks = 0

    for video in new_videos:
        n_processed += 1
        if args.limit and n_processed > args.limit:
            break

        vid = video["id"]
        title = video.get("title") or f"YouTube: {vid}"

        if args.dry_run:
            print(f"  [{n_processed}] {title} — {video['url']}")
            continue

        print(f"  [{n_processed}/{len(new_videos)}] {title}...")

        result = ingest_single_video(
            db_client, embed_client, embed_model, chat_client, cfg.openrouter.chat_model,
            video_id=vid, title=title, cookies=args.cookies,
        )

        status = result.get("status", "unknown")
        n_chunks = result.get("chunks_processed", 1)
        total_chunks += n_chunks

        if status == "success":
            n_success += 1
            chunk_info = f" ({n_chunks} chunks)" if n_chunks > 1 else ""
            print(f"    OK{chunk_info} — {result.get('entities_count', 0)} entities, {result.get('relations_count', 0)} rels, {result.get('observations_count', 0)} obs")
        elif status == "duplicate":
            n_dup += 1
            print(f"    DUP")
        elif status == "skipped":
            n_skipped += 1
            print(f"    SKIP — {result.get('reason', 'unknown')}")
        else:
            n_failed += 1
            print(f"    FAIL — {result.get('error', 'unknown')}")

        if args.delay > 0:
            time.sleep(args.delay)

    # Update sync state
    if not args.dry_run and args.sync:
        update_sync_state(db_client, sync_id, {
            "playlist_id": playlist_id,
            "total_videos": len(videos),
            "ingested_this_run": n_success,
            "chunks_this_run": total_chunks,
        })

    print()
    print(f"=== YouTube Playlist Ingestion Complete ===")
    print(f"Playlist: {playlist_id}")
    print(f"Total in playlist: {len(videos)}")
    print(f"Previously ingested: {len(already_ingested)}")
    print(f"Processed: {n_processed}")
    print(f"Success: {n_success} ({total_chunks} chunks)")
    print(f"Duplicates: {n_dup}")
    print(f"Skipped: {n_skipped}")
    print(f"Failed: {n_failed}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
