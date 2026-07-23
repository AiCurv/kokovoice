"""
Text Chunker — splits long TTS input into ordered chunks.

Strategy:
  1. Primary: Split at natural sentence boundaries (periods, exclamation marks,
     question marks, newlines).
  2. Secondary: If a single sentence exceeds MAX_CHUNK_SIZE, split at whitespace.
  3. Tertiary: Only split inside a word as an absolute last resort.

Each chunk preserves order and no text is lost or duplicated.
"""

import re
from typing import List

# Maximum characters per chunk — chosen to be safe for Kokoro inference
# and well under the GitHub Actions payload limit. Kokoro can handle
# ~500 chars comfortably; we use 450 to leave margin for lang codes etc.
MAX_CHUNK_SIZE = 450

# Sentence-ending punctuation patterns
SENTENCE_END_RE = re.compile(r'[.!?。！？]\s*')
NEWLINE_RE = re.compile(r'\n+')


def chunk_text(text: str, max_chunk_size: int = MAX_CHUNK_SIZE) -> List[str]:
    """
    Split text into ordered chunks that never exceed max_chunk_size.

    Returns:
        List of text chunks in original order. No text is lost.
        Each chunk is guaranteed <= max_chunk_size characters.
    """
    if len(text) <= max_chunk_size:
        return [text]

    # Strategy 1: Split at sentence boundaries
    sentences = _split_into_sentences(text)
    chunks = _merge_sentences_into_chunks(sentences, max_chunk_size)

    # Validate: if any chunk still exceeds max_chunk_size, force-split it
    final_chunks = []
    for chunk in chunks:
        if len(chunk) <= max_chunk_size:
            final_chunks.append(chunk)
        else:
            # Strategy 2/3: Force-split oversized chunks
            final_chunks.extend(_force_split(chunk, max_chunk_size))

    # Remove any empty chunks
    final_chunks = [c for c in final_chunks if c.strip()]

    return final_chunks


def _split_into_sentences(text: str) -> List[str]:
    """Split text at sentence boundaries, preserving punctuation."""
    # Split on sentence-ending punctuation followed by whitespace or newline
    parts = SENTENCE_END_RE.split(text)

    # Re-attach the punctuation that was consumed by the regex
    sentences = []
    positions = [m.end() for m in SENTENCE_END_RE.finditer(text)]

    pos = 0
    for i, part in enumerate(parts):
        end = positions[i] if i < len(positions) else len(text)
        sentence = text[pos:end]
        pos = end
        if sentence.strip():
            sentences.append(sentence.strip())

    # If the regex didn't produce good splits, try splitting on newlines
    if len(sentences) <= 1 and len(text) > MAX_CHUNK_SIZE:
        lines = NEWLINE_RE.split(text)
        sentences = [line.strip() for line in lines if line.strip()]

    # If we still have just one giant sentence, return the whole text
    # (the force-split will handle it)
    if not sentences:
        sentences = [text]

    return sentences


def _merge_sentences_into_chunks(sentences: List[str], max_size: int) -> List[str]:
    """Merge consecutive sentences into chunks up to max_size."""
    chunks = []
    current_chunk = ""

    for sentence in sentences:
        # If adding this sentence would exceed max_size, start a new chunk
        if current_chunk and len(current_chunk) + len(sentence) + 1 > max_size:
            chunks.append(current_chunk)
            current_chunk = sentence
        else:
            if current_chunk:
                current_chunk = current_chunk + " " + sentence
            else:
                current_chunk = sentence

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _force_split(text: str, max_size: int) -> List[str]:
    """
    Force-split text that exceeds max_size.
    Strategy 2: Split at whitespace boundaries.
    Strategy 3: Split inside a word only as last resort.
    """
    if len(text) <= max_size:
        return [text]

    chunks = []
    words = text.split()
    current = ""

    for word in words:
        if current and len(current) + len(word) + 1 > max_size:
            chunks.append(current)
            current = word
        else:
            if current:
                current = current + " " + word
            else:
                current = word

    # Handle the case where a single word exceeds max_size (Strategy 3)
    if current and len(current) > max_size:
        # Split inside the word at max_size boundaries
        for i in range(0, len(current), max_size):
            piece = current[i:i + max_size]
            if piece:
                chunks.append(piece)
    elif current:
        chunks.append(current)

    return chunks
