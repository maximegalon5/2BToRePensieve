"""Shared text chunking for knowledge graph ingestion.

Splits long text into chunks at sentence boundaries, staying under the
12k char extraction limit so each chunk gets full LLM processing.
Used by YouTube, PDF, Notion, and email connectors.
"""
from __future__ import annotations


def chunk_text(text: str, max_chars: int = 10000) -> list[str]:
    """Split long text into chunks at sentence boundaries.

    Each chunk is up to max_chars, split at sentence-ending punctuation
    (. ! ?) to avoid cutting mid-thought. Default 10k stays safely under
    the 12k extraction limit.

    Returns a list of 1+ chunks (single-element list if text is short enough).
    """
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0

    while start < len(text):
        end = min(start + max_chars, len(text))

        if end < len(text):
            # Try to split at a sentence boundary (look back from end)
            window = text[start:end]
            # Find last sentence-ending punctuation followed by a space/newline
            best_cut = -1
            for punct in [". ", "! ", "? ", ".\n", "!\n", "?\n"]:
                pos = window.rfind(punct)
                if pos > best_cut and pos >= max_chars // 3:
                    best_cut = pos + len(punct)

            if best_cut > 0:
                end = start + best_cut
            else:
                # Fall back to last space
                space_pos = window.rfind(" ")
                if space_pos > max_chars // 3:
                    end = start + space_pos + 1

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        start = end

    return chunks
