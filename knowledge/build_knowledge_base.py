#!/usr/bin/env python3
"""Build the TrailCurrent offline knowledge base for Peregrine.

Crawls markdown documentation from all TrailCurrent repositories, chunks
it into retrieval-sized pieces, and writes knowledge/chunks.json.

Run on the dev machine after updating documentation:
    python knowledge/build_knowledge_base.py

The output (knowledge/chunks.json) is deployed to the board by deploy.sh.
"""

import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# Source documents — ordered by priority (more specific first)
# CLAUDE.md files are intentionally excluded (agent config, not product docs)
# ---------------------------------------------------------------------------

PRODUCT_BASE = "/media/dave/extstorage/TrailCurrent/Product"
DOCS_BASE    = os.path.join(PRODUCT_BASE, "TrailCurrentDocumentation")

# fmt: off
SOURCES = [
    # ── Central documentation hub ──────────────────────────────────────────
    # All .md files under each section directory
    {"dir": os.path.join(DOCS_BASE, "01_Architecture")},
    {"dir": os.path.join(DOCS_BASE, "02_Hardware_Modules")},
    # SMS_NOTIFICATIONS.md excluded — internal Headwaters implementation detail
    # with incorrect "Borealis (PDM) lights" entry that confuses module identity queries
    {"file": os.path.join(DOCS_BASE, "03_Vehicle_Compute", "README.md")},
    {"file": os.path.join(DOCS_BASE, "03_Vehicle_Compute", "SETUP_GUIDE.md")},
    {"dir": os.path.join(DOCS_BASE, "04_Cloud_Application")},
    {"dir": os.path.join(DOCS_BASE, "05_Mobile_Application")},
    {"dir": os.path.join(DOCS_BASE, "06_Shared_Libraries")},
    {"dir": os.path.join(DOCS_BASE, "07_Development")},
    {"dir": os.path.join(DOCS_BASE, "08_Deployment")},
    {"dir": os.path.join(DOCS_BASE, "09_Troubleshooting")},
    {"dir": os.path.join(DOCS_BASE, "10_Reference")},
    # Root docs in the documentation hub
    {"file": os.path.join(DOCS_BASE, "README.md")},
    {"file": os.path.join(DOCS_BASE, "CORE_PRINCIPLES.md")},
    {"file": os.path.join(DOCS_BASE, "WHAT_IS_SOFTWARE_DEFINED_VEHICLE.md")},
    {"file": os.path.join(DOCS_BASE, "QUICK_START.md")},

    # ── Hardware module READMEs ────────────────────────────────────────────
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentBearing",   "README.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentTapper",    "README.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentPicket",    "README.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentAftline",   "README.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentSolstice",  "README.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentTorrent",   "README.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentAmpline",   "README.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentBorealis",  "README.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentPlateau",   "README.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentMilepost",  "README.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentTherma",    "README.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentSwitchback","README.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentReservoir", "README.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentFireside",  "README.md")},

    # ── Reservoir extra docs ───────────────────────────────────────────────
    {"dir": os.path.join(PRODUCT_BASE, "TrailCurrentReservoir", "DOCS")},

    # ── App repos ─────────────────────────────────────────────────────────
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentHeadwaters", "README.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentHeadwaters", "PI_DEPLOYMENT.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentHeadwaters", "SECURITY.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentFarwatch",   "README.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentPeregrine",  "README.md")},
    {"file": os.path.join(PRODUCT_BASE, "TrailCurrentPeregrine",  "image_build", "README.md")},
]
# fmt: on

# ---------------------------------------------------------------------------
# Markdown cleaning
# ---------------------------------------------------------------------------

# Patterns that mark a line as navigation noise (table-of-contents links etc.)
_LINK_LINE_RE = re.compile(r"^\s*[-*]\s*\[.+?\]\(.+?\)\s*$")
_IMAGE_RE      = re.compile(r"!\[.*?\]\(.*?\)")
_HTML_TAG_RE   = re.compile(r"<[^>]+>")
_BARE_URL_RE   = re.compile(r"(?<!\()(https?://\S+)")
_FENCE_RE      = re.compile(r"```.*?```", re.DOTALL)
_INDENT_CODE_RE = re.compile(r"(?m)^(    |\t).+")  # indented code blocks
# Markdown table rows: lines that start and end with | (pipe)
_TABLE_ROW_RE   = re.compile(r"(?m)^\|.+\|\s*$")
_TABLE_SEP_RE   = re.compile(r"(?m)^\|[-| :]+\|\s*$")  # separator rows |---|---|

def _clean_markdown(text: str) -> str:
    """Strip code blocks, tables, images, HTML, bare URLs from markdown text."""
    # Replace fenced code blocks with a brief placeholder
    text = _FENCE_RE.sub("[code example]", text)
    # Remove indented code blocks
    text = _INDENT_CODE_RE.sub("", text)
    # Remove markdown tables (they list names without context, pollute TF-IDF)
    text = _TABLE_SEP_RE.sub("", text)
    text = _TABLE_ROW_RE.sub("", text)
    # Remove markdown images
    text = _IMAGE_RE.sub("", text)
    # Remove HTML tags
    text = _HTML_TAG_RE.sub("", text)
    # Remove bare URLs (but keep link text)
    text = re.sub(r"\[(.+?)\]\(https?://[^)]+\)", r"\1", text)  # [text](url) → text
    text = _BARE_URL_RE.sub("", text)
    # Remove horizontal rules
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_STUB_PHRASES = (
    "NEEDS TO BE COMPLETED",
    "TODO",
    "PLACEHOLDER",
    "Coming soon",
    "TBD",
)

def _is_stub(text: str) -> bool:
    """Return True if this chunk is an unfilled documentation stub."""
    return any(phrase in text for phrase in _STUB_PHRASES)


_CHECKBOX_RE = re.compile(r"^\s*-\s*\[[ x]\]")

def _is_nav_noise(lines: list[str]) -> bool:
    """Return True if >55% of non-empty lines are navigation link lines,
    or >60% are checkbox list items (deployment checklists, task lists)."""
    non_empty = [l for l in lines if l.strip()]
    if not non_empty:
        return True
    link_count = sum(1 for l in non_empty if _LINK_LINE_RE.match(l))
    if link_count / len(non_empty) > 0.55:
        return True
    checkbox_count = sum(1 for l in non_empty if _CHECKBOX_RE.match(l))
    return checkbox_count / len(non_empty) > 0.60


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

MAX_WORDS = 140   # soft ceiling per chunk (~175 tokens)
MIN_WORDS = 25    # discard chunks shorter than this


def _word_count(text: str) -> int:
    return len(text.split())


def _split_into_sentences(text: str) -> list[str]:
    """Rough sentence splitter — good enough for documentation prose."""
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\-*])", text)
    return [p.strip() for p in parts if p.strip()]


def _chunk_section(heading: str, body: str) -> list[str]:
    """Split a section body into chunks of at most MAX_WORDS words.

    Splits by paragraph first; if a paragraph still exceeds MAX_WORDS,
    splits further at sentence boundaries.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\n+", body) if p.strip()]
    chunks: list[str] = []
    current_lines: list[str] = []
    current_words = 0

    for para in paragraphs:
        para_words = _word_count(para)
        if current_words + para_words > MAX_WORDS and current_lines:
            chunks.append("\n\n".join(current_lines))
            current_lines = []
            current_words = 0

        if para_words > MAX_WORDS:
            # Split the oversized paragraph at sentence boundaries
            sentences = _split_into_sentences(para)
            for sent in sentences:
                sw = _word_count(sent)
                if current_words + sw > MAX_WORDS and current_lines:
                    chunks.append("\n\n".join(current_lines))
                    current_lines = []
                    current_words = 0
                current_lines.append(sent)
                current_words += sw
        else:
            current_lines.append(para)
            current_words += para_words

    if current_lines:
        chunks.append("\n\n".join(current_lines))

    return chunks


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)


def _extract_sections(text: str, file_label: str) -> list[dict]:
    """Split a markdown document into (heading, body) sections.

    Returns a list of {source, heading, text} dicts — not yet filtered.
    """
    text = _clean_markdown(text)
    sections: list[dict] = []

    # Find all heading positions
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        # No headings at all — treat the whole file as one section
        sections.append({
            "source": file_label,
            "heading": os.path.basename(file_label).replace(".md", ""),
            "body": text,
        })
        return sections

    # Text before the first heading
    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections.append({
            "source": file_label,
            "heading": os.path.basename(file_label).replace(".md", ""),
            "body": preamble,
        })

    for i, match in enumerate(matches):
        heading_text = match.group(2).strip()
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        sections.append({
            "source": file_label,
            "heading": heading_text,
            "body": body,
        })

    return sections


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

def _collect_files() -> list[str]:
    """Expand SOURCES into a deduplicated list of .md file paths."""
    seen: set[str] = set()
    files: list[str] = []

    for src in SOURCES:
        if "file" in src:
            path = src["file"]
            if os.path.isfile(path) and path not in seen:
                seen.add(path)
                files.append(path)
            elif not os.path.isfile(path):
                print(f"  [skip] not found: {path}", file=sys.stderr)
        elif "dir" in src:
            d = src["dir"]
            if not os.path.isdir(d):
                print(f"  [skip] directory not found: {d}", file=sys.stderr)
                continue
            for fname in sorted(os.listdir(d)):
                if fname.endswith(".md") and not fname.upper().startswith("CLAUDE"):
                    path = os.path.join(d, fname)
                    if path not in seen:
                        seen.add(path)
                        files.append(path)

    return files


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

def _make_label(path: str) -> str:
    """Convert an absolute path to a short human-readable label."""
    try:
        return os.path.relpath(path, PRODUCT_BASE)
    except ValueError:
        return path


def build(output_path: str) -> None:
    files = _collect_files()
    print(f"Processing {len(files)} files...")

    chunks: list[dict] = []
    chunk_id = 0

    for path in files:
        label = _make_label(path)
        try:
            raw = open(path, encoding="utf-8").read()
        except OSError as e:
            print(f"  [error] {label}: {e}", file=sys.stderr)
            continue

        sections = _extract_sections(raw, label)
        file_chunks = 0

        for sec in sections:
            body_lines = sec["body"].splitlines()
            if _is_nav_noise(body_lines):
                continue

            sub_chunks = _chunk_section(sec["heading"], sec["body"])
            for text in sub_chunks:
                # Prefix with heading so each chunk is self-contained
                full_text = f"{sec['heading']}: {text}" if sec["heading"] else text
                if _word_count(full_text) < MIN_WORDS:
                    continue
                if _is_stub(full_text):
                    continue
                chunks.append({
                    "id": f"doc-{chunk_id:04d}",
                    "source": sec["source"],
                    "heading": sec["heading"],
                    "text": full_text,
                })
                chunk_id += 1
                file_chunks += 1

        if file_chunks:
            print(f"  {label}: {file_chunks} chunks")

    # Write output
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(chunks, f, indent=2, ensure_ascii=False)

    size_kb = os.path.getsize(output_path) // 1024
    print(f"\nWrote {len(chunks)} chunks to {output_path} ({size_kb} KB)")

    # Sample output
    print("\nSample chunks:")
    import random
    for c in random.sample(chunks, min(4, len(chunks))):
        preview = c["text"][:120].replace("\n", " ")
        print(f"  [{c['id']}] {c['source']} / {c['heading']}")
        print(f"    {preview}...")
        print()


if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output = os.path.join(script_dir, "chunks.json")
    build(output)
