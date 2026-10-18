#!/usr/bin/env python3
"""LAN-facing web chat UI for the Peregrine NPU LLM.

Serves a single-page chat UI at ``GET /`` and a streaming text endpoint at
``POST /api/chat`` that proxies into the local Genie server on
127.0.0.1:11434 (which talks to the Hexagon NPU). The user assistant.py loop
also funnels through genie-server, so this front-end shares the model with
voice — concurrency is serialized by the GenieDialog lock inside genie_server.

Network shape:

   Browser ──HTTPS──> web_chat.py (0.0.0.0:443, TLS-wrapped, this process)
                          │
   Browser ──HTTP───> redirect listener (0.0.0.0:80) ── 301 ─> https://…
                          │
                          └─loopback─> genie_server.py (127.0.0.1:11434)
                                              │
                                              └─libGenie──> Hexagon NPU

TLS uses a self-signed certificate minted by ``scripts/generate-certs.sh``.
The CA is NOT served over the network — by design — so unauthenticated LAN
clients can't enumerate it. Distribution is out-of-band: operators run
``peregrine-self-test.sh --show-ca`` (over SSH, which requires credentials)
or scp ``/home/trailcurrent/certs/ca.pem``. The HTTP listener on :80 exists
only to 301-redirect any plain-HTTP request to HTTPS.

Conversation state lives entirely in the browser (localStorage). The server
is stateless — each /api/chat POST sends the full message list and trims it
here to fit the model's 1024-token context window before forwarding to
genie-server's /api/chat endpoint.
"""

import hmac
import io
import json
import os
import re
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import wave
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn


# --- Config ---
HOST = os.getenv("WEB_CHAT_HOST", "0.0.0.0")
GENIE_URL = os.getenv("GENIE_URL", "http://127.0.0.1:11434")

# Intent RPC exposed by assistant.py — lets the chat run the same device-status,
# device-control, and geocode intents the voice loop uses. If unreachable
# (assistant.py not running, wrong port), we transparently fall back to LLM only.
INTENT_RPC_URL = os.getenv("INTENT_RPC_URL", "http://127.0.0.1:11435")
INTENT_RPC_TIMEOUT = float(os.getenv("INTENT_RPC_TIMEOUT", "1.5"))

# TLS — when WEB_CHAT_TLS_CERT and WEB_CHAT_TLS_KEY are set the main listener
# wraps its socket with SSL and listens on WEB_CHAT_HTTPS_PORT (default 443).
# A second listener on WEB_CHAT_HTTP_PORT (default 80) serves /ca.pem in the
# clear and 301-redirects everything else to HTTPS.
TLS_CERT_PATH = os.getenv("WEB_CHAT_TLS_CERT", "")
TLS_KEY_PATH = os.getenv("WEB_CHAT_TLS_KEY", "")
CA_PATH = os.getenv("WEB_CHAT_CA_PATH", "")
TLS_ENABLED = bool(TLS_CERT_PATH and TLS_KEY_PATH
                   and os.path.isfile(TLS_CERT_PATH)
                   and os.path.isfile(TLS_KEY_PATH))
HTTPS_PORT = int(os.getenv("WEB_CHAT_HTTPS_PORT", "443"))
HTTP_PORT = int(os.getenv("WEB_CHAT_HTTP_PORT", "80"))
# Public hostname used when redirecting HTTP→HTTPS (so the redirect target
# uses the name the user already typed rather than an IP).
PUBLIC_HOSTNAME = os.getenv("WEB_CHAT_PUBLIC_HOSTNAME", "")
# Legacy/override single port — only honored when TLS is OFF.
PORT = int(os.getenv("WEB_CHAT_PORT", "80"))

# --- Voice-terminal endpoint (ESP32 P4 push-to-talk) ---
# When PEREGRINE_VOICE_TOKEN is set, we open an additional plain-HTTP
# listener on PEREGRINE_VOICE_PORT (default 8081) that accepts a WAV upload,
# transcribes it, runs the same intent-then-LLM path the chat UI uses, and
# returns a WAV of the spoken reply. Trust boundary is LAN + shared bearer
# token — the ESP32 firmware can't practically ship the self-signed TLS CA
# for the HTTPS listener, and both boxes live on the same in-vehicle network.
VOICE_PORT = int(os.getenv("PEREGRINE_VOICE_PORT", "8081"))
VOICE_TOKEN = os.getenv("PEREGRINE_VOICE_TOKEN", "")
VOICE_ENABLED = bool(VOICE_TOKEN)
VOICE_MAX_BYTES = int(os.getenv("PEREGRINE_VOICE_MAX_BYTES", str(1024 * 1024)))
VOICE_TARGET_SR = int(os.getenv("PEREGRINE_VOICE_TARGET_SR", "16000"))
PIPER_MODEL_PATH = os.getenv(
    "PIPER_MODEL",
    os.path.expanduser("~/piper-voices/en_US-libritts_r-medium.onnx"),
)

# Lazily initialized on first /api/voice request so the chat UI can start
# even if faster-whisper / Piper are missing on the box.
_stt_engine = None
_tts_engine = None
_engines_lock = threading.Lock()

DEFAULT_SYSTEM_PROMPT = os.getenv(
    "WEB_CHAT_SYSTEM",
    # The chat process consults an intent RPC before this prompt ever reaches
    # the LLM — sensor readings, device control, and location queries are
    # answered from live MQTT / GPS state, not from the model. Tell the model
    # so it doesn't reflexively disclaim ("I can't see the physical
    # environment") when a fallback prose answer is warranted.
    "You are Peregrine, a helpful on-device voice and chat assistant for the "
    "TrailCurrent platform. You have access to live vehicle sensor data and "
    "device controls (lights, relays, radio, thermostat, GPS location, air "
    "quality, battery, water tanks) which are handled by a separate intent "
    "layer before your response is used — never claim you can't see the "
    "physical environment. If a question about the vehicle reaches you here, "
    "answer conversationally from general knowledge; the intent layer will "
    "have already handled anything requiring live data. Reply concisely.",
)

# The voice path (Piper TTS) reads every character aloud, including markdown
# punctuation the chat UI would render invisibly. Append a plain-speech
# instruction only for LLM calls whose response goes to TTS — keep the chat
# UI's system prompt unchanged so it can still emit formatted answers.
VOICE_SYSTEM_PROMPT = os.getenv(
    "WEB_CHAT_VOICE_SYSTEM",
    DEFAULT_SYSTEM_PROMPT + " Respond in plain spoken English — no markdown, "
    "no headings, no bullet points, no asterisks, no code blocks. Use short "
    "sentences.",
)

# Llama3.2-1B-1024-v68 has a 1024-token context. Leave room for the new
# user turn + response. Token counts are estimated as ~4 chars per token
# (close enough for English; we err on the side of more trimming).
MAX_CONTEXT_TOKENS = int(os.getenv("WEB_CHAT_MAX_CONTEXT", "700"))
RESPONSE_RESERVE_TOKENS = 200


def _approx_tokens(text: str) -> int:
    """Rough character-based token estimate. Good enough for trimming."""
    return max(1, len(text) // 4)


def _latest_user_text(messages):
    """Return the newest user message's content, or '' if none."""
    for m in reversed(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"].strip()
    return ""


def _replace_last_user(messages, new_content):
    """Return a new message list with the newest user turn's content replaced.

    The caller's list and dicts are not mutated — the replacement is a
    copy. Only the rewritten dict is new; other dicts are re-referenced.
    """
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") == "user":
            out[i] = dict(out[i], content=new_content)
            break
    return out


def _rpc_post(path, payload):
    """POST JSON to the intent RPC and return the decoded response dict.

    Returns None on any transport error — callers treat that as "no match"
    so the chat still works when assistant.py isn't running.
    """
    try:
        req = urllib.request.Request(
            INTENT_RPC_URL.rstrip("/") + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=INTENT_RPC_TIMEOUT) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, ValueError, TimeoutError, OSError) as e:
        print(f"[web-chat] intent RPC unavailable ({path}): {e}", file=sys.stderr)
        return None


def _intent_lookup(text):
    """Return a canned response string if the intent RPC matches, else None."""
    result = _rpc_post("/api/intent", {"text": text})
    if result and result.get("matched"):
        return result.get("response")
    return None


def _enrich_text(text):
    """Ask the intent RPC to prepend location context if applicable.

    Returns the (possibly rewritten) text. On any transport error, returns
    the original text unchanged — the LLM still gets a valid prompt, just
    without the "you are in <city>" hint.
    """
    result = _rpc_post("/api/enrich", {"text": text})
    if result and isinstance(result.get("text"), str):
        return result["text"]
    return text


def _command_execute(text):
    """Run handle_command on the LLM's JSON output via the RPC.

    Returns the spoken confirmation or None if the JSON couldn't be executed.
    """
    result = _rpc_post("/api/command", {"text": text})
    if result and result.get("executed"):
        return result.get("confirmation")
    return None


def _trim_messages(messages, system_prompt):
    """Drop oldest turns until estimated tokens fit the context budget.

    Always keeps the latest user message. Pairs (user/assistant) are removed
    from the front when over budget.
    """
    budget = MAX_CONTEXT_TOKENS - _approx_tokens(system_prompt) - RESPONSE_RESERVE_TOKENS
    cleaned = [m for m in messages if m.get("role") in ("user", "assistant")
               and isinstance(m.get("content"), str) and m["content"].strip()]
    if not cleaned:
        return cleaned

    def total_tokens(lst):
        return sum(_approx_tokens(m["content"]) for m in lst)

    while len(cleaned) > 1 and total_tokens(cleaned) > budget:
        # Drop the oldest message; if it's a user, also drop the next assistant.
        cleaned.pop(0)
    # If even the latest message blows the budget, truncate its content.
    if cleaned and total_tokens(cleaned) > budget:
        last = cleaned[-1]
        max_chars = max(200, budget * 4)
        last["content"] = last["content"][:max_chars]
    return cleaned


CHAT_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<meta name="theme-color" content="#1a1f1c" />
<title>Peregrine</title>
<style>
  :root {
    --bg: #1a1f1c;
    --panel: #232a26;
    --panel-2: #2c3530;
    --text: #e8efe9;
    --muted: #8ea297;
    --accent: #52a441;
    --accent-dim: #3d7a30;
    --user: #2f4a3a;
    --border: #34403a;
    /* Side padding scales between mobile and desktop. */
    --pad-x: 12px;
    --max-w: 820px;
  }
  * { box-sizing: border-box; min-width: 0; }
  html, body {
    margin: 0; height: 100%;
    /* dvh accounts for mobile address-bar resize without scroll jumps */
    height: 100dvh;
    overflow: hidden;
  }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter,
                 "Liberation Sans", sans-serif;
    font-size: 16px;            /* 16px base prevents iOS Safari zoom-on-focus */
    line-height: 1.45;
    display: flex; flex-direction: column;
    /* Respect iPhone notch / Android nav-bar */
    padding: env(safe-area-inset-top) env(safe-area-inset-right)
             env(safe-area-inset-bottom) env(safe-area-inset-left);
  }
  header {
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    padding: 10px var(--pad-x);
    display: flex; align-items: center; gap: 10px;
    flex-wrap: nowrap;
  }
  header .dot {
    width: 10px; height: 10px; border-radius: 50%; flex: 0 0 10px;
    background: var(--accent); box-shadow: 0 0 8px var(--accent);
  }
  header h1 {
    margin: 0; font-size: 16px; font-weight: 600; letter-spacing: 0.02em;
    margin-right: auto;
  }
  header button {
    background: transparent; border: 1px solid var(--border); color: var(--muted);
    padding: 6px 10px; border-radius: 4px; cursor: pointer; font-size: 13px;
    flex: 0 0 auto;
  }
  header button:hover, header button:active {
    color: var(--text); border-color: var(--accent-dim);
  }

  #log {
    flex: 1; overflow-y: auto; overflow-x: hidden;
    padding: 12px var(--pad-x);
    display: flex; flex-direction: column; gap: 12px;
    width: 100%; max-width: var(--max-w); margin: 0 auto;
    -webkit-overflow-scrolling: touch;
  }
  .msg {
    display: flex; flex-direction: column; gap: 4px;
    max-width: 92%;
  }
  .msg.user { align-self: flex-end; align-items: flex-end; }
  .msg.assistant, .msg.error { align-self: flex-start; align-items: flex-start; }
  .msg .who {
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--muted); padding: 0 4px;
  }
  .msg .bubble {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 10px 14px;
    font-size: 15px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;     /* break very long URLs / tokens */
    word-break: break-word;
    line-height: 1.5;
    max-width: 100%;
  }
  .msg.user .bubble { white-space: pre-wrap; }
  /* The assistant bubble holds rendered markdown — block elements set their
     own spacing, so suppress the pre-wrap newlines that would double up. */
  .msg.assistant .bubble { white-space: normal; }

  /* Markdown block styles inside the assistant bubble */
  .bubble p { margin: 6px 0; }
  .bubble p:first-child { margin-top: 0; }
  .bubble p:last-child  { margin-bottom: 0; }
  .bubble h1, .bubble h2, .bubble h3, .bubble h4 {
    margin: 12px 0 6px; line-height: 1.3;
  }
  .bubble h1 { font-size: 18px; }
  .bubble h2 { font-size: 16px; }
  .bubble h3, .bubble h4 { font-size: 15px; }
  .bubble ul, .bubble ol { margin: 6px 0; padding-left: 22px; }
  .bubble li { margin: 2px 0; }
  .bubble a { color: var(--accent); }

  /* Inline code */
  .bubble code.inline {
    background: #0e1410;
    border: 1px solid var(--border);
    padding: 1px 5px;
    border-radius: 3px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.9em;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  /* Fenced code blocks with a copyable header */
  .bubble pre.codeblock {
    background: #0e1410;
    border: 1px solid var(--border);
    border-radius: 8px;
    margin: 8px 0;
    overflow: hidden;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 13px;
    max-width: 100%;
  }
  .bubble pre.codeblock .codehead {
    display: flex; justify-content: space-between; align-items: center;
    gap: 8px;
    background: #1a221c;
    border-bottom: 1px solid var(--border);
    padding: 4px 8px 4px 10px;
    font-size: 11px;
    color: var(--muted);
  }
  .bubble pre.codeblock .lang {
    letter-spacing: 0.04em; text-transform: lowercase;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .bubble pre.codeblock .copy {
    background: transparent; border: 1px solid var(--border);
    color: var(--muted); padding: 3px 10px; border-radius: 4px;
    font-size: 11px; cursor: pointer; font-family: inherit;
    flex: 0 0 auto; min-height: 26px;
  }
  .bubble pre.codeblock .copy:hover,
  .bubble pre.codeblock .copy:active {
    color: var(--text); border-color: var(--accent-dim);
  }
  .bubble pre.codeblock .copy.copied {
    color: var(--accent); border-color: var(--accent);
  }
  .bubble pre.codeblock code {
    display: block;
    padding: 10px 12px;
    white-space: pre;            /* preserve formatting inside code */
    overflow-x: auto;            /* code scrolls horizontally, bubble doesn't */
    line-height: 1.5;
    color: #d7e6d2;
    -webkit-overflow-scrolling: touch;
  }
  .msg.user .bubble {
    background: var(--user); border-color: var(--accent-dim);
    border-bottom-right-radius: 4px;
  }
  .msg.assistant .bubble { border-bottom-left-radius: 4px; }
  .msg.error .bubble { color: #ff8a8a; border-color: #5a2a2a; background: #2a1f1f; }
  .cursor::after {
    content: "▊"; color: var(--accent); animation: blink 1s steps(2) infinite;
    margin-left: 2px;
  }
  @keyframes blink { 50% { opacity: 0; } }

  form {
    background: var(--panel);
    border-top: 1px solid var(--border);
    padding: 10px var(--pad-x);
    display: flex; gap: 8px; align-items: flex-end;
    width: 100%; max-width: var(--max-w); margin: 0 auto;
  }
  textarea {
    flex: 1; min-width: 0;            /* avoid flex-overflow blowing layout */
    resize: none; min-height: 44px; max-height: 40vh;
    background: var(--panel-2); color: var(--text);
    border: 1px solid var(--border); border-radius: 10px;
    padding: 10px 12px; font: inherit;
    font-size: 16px;                  /* keep 16px on mobile to suppress zoom */
  }
  textarea:focus { outline: none; border-color: var(--accent); }
  button.send {
    background: var(--accent); color: #0a1108; border: none;
    padding: 0 18px; min-height: 44px; border-radius: 10px;
    font-weight: 600; cursor: pointer; font-size: 15px;
    flex: 0 0 auto;
  }
  button.send:disabled { background: var(--accent-dim); cursor: not-allowed; }

  /* Wider screens: more breathing room, slightly larger bubbles */
  @media (min-width: 700px) {
    :root { --pad-x: 20px; }
    header { padding: 12px var(--pad-x); }
    #log { padding: 20px var(--pad-x); gap: 14px; }
    .msg { max-width: 80%; }
    .msg .bubble { font-size: 15px; }
    form { padding: 12px var(--pad-x); gap: 10px; }
  }
</style>
</head>
<body>
<header>
  <div class="dot"></div>
  <h1>Peregrine</h1>
  <button id="clear">Clear</button>
</header>

<div id="log" aria-live="polite"></div>

<form id="form" autocomplete="off">
  <textarea id="input" placeholder="Ask Peregrine anything…"
            rows="1" autofocus></textarea>
  <button class="send" type="submit">Send</button>
</form>

<script>
const STORAGE_KEY = "peregrine-chat-history";
const log = document.getElementById("log");
const form = document.getElementById("form");
const input = document.getElementById("input");
const sendBtn = form.querySelector("button.send");
const clearBtn = document.getElementById("clear");

let history = loadHistory();
renderHistory();

function loadHistory() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || []; }
  catch (e) { return []; }
}
function saveHistory() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(history)); }
  catch (e) {}
}
function renderHistory() {
  log.innerHTML = "";
  for (const m of history) addBubble(m.role, m.content);
  log.scrollTop = log.scrollHeight;
}
function setBubbleContent(bubble, role, text) {
  // User input renders as plain text (white-space: pre-wrap preserves newlines).
  // Assistant output is markdown — rendered with our tiny inline renderer.
  if (role === "assistant") {
    bubble.innerHTML = renderMarkdown(text);
  } else {
    bubble.textContent = text;
  }
}
function addBubble(role, text) {
  const msg = document.createElement("div");
  msg.className = "msg " + role;
  const who = document.createElement("div");
  who.className = "who";
  who.textContent = role === "user" ? "You" : (role === "assistant" ? "Peregrine" : role);
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  setBubbleContent(bubble, role, text);
  msg.appendChild(who);
  msg.appendChild(bubble);
  log.appendChild(msg);
  log.scrollTop = log.scrollHeight;
  return bubble;
}

// ─── Tiny markdown renderer ────────────────────────────────────────────────
// Handles fenced code blocks (with copy button + optional language label),
// inline code, bold, italic, links, headings, and unordered/ordered lists.
// Sanitization: everything from the model is HTML-escaped before any tag
// insertion; link hrefs are restricted to safe schemes.
function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, "&quot;");
}
function renderInline(text) {
  let out = escapeHtml(text);
  // Inline code first so its contents are immune to other inline rules.
  out = out.replace(/`([^`\n]+)`/g, (_, code) => `<code class="inline">${code}</code>`);
  // Bold **text**
  out = out.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  // Italic *text* — avoid matching ** by requiring a non-* on each side.
  out = out.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  // Links [text](url) — only allow http(s), mailto, relative, or anchor.
  out = out.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (m, txt, url) => {
    if (!/^(https?:\/\/|mailto:|\/|#)/i.test(url)) return m;
    return '<a href="' + escapeAttr(url) +
           '" target="_blank" rel="noopener noreferrer">' + txt + "</a>";
  });
  return out;
}
function renderTextBlock(text) {
  const lines = text.split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    let line = lines[i];
    if (!line.trim()) { i++; continue; }
    // Heading
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      const lvl = Math.min(h[1].length, 4);
      out.push("<h" + lvl + ">" + renderInline(h[2]) + "</h" + lvl + ">");
      i++; continue;
    }
    // Unordered list
    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push("<li>" + renderInline(lines[i].replace(/^\s*[-*]\s+/, "")) + "</li>");
        i++;
      }
      out.push("<ul>" + items.join("") + "</ul>");
      continue;
    }
    // Ordered list
    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push("<li>" + renderInline(lines[i].replace(/^\s*\d+\.\s+/, "")) + "</li>");
        i++;
      }
      out.push("<ol>" + items.join("") + "</ol>");
      continue;
    }
    // Paragraph — gather adjacent non-special, non-blank lines
    const para = [];
    while (i < lines.length && lines[i].trim()
           && !/^#{1,6}\s/.test(lines[i])
           && !/^\s*[-*]\s+/.test(lines[i])
           && !/^\s*\d+\.\s+/.test(lines[i])) {
      para.push(lines[i]);
      i++;
    }
    // Join with <br> so the model's intra-paragraph line breaks survive.
    out.push("<p>" + para.map(renderInline).join("<br>") + "</p>");
  }
  return out.join("");
}
function renderMarkdown(src) {
  if (!src) return "";
  // Split on fenced code blocks. Match an opening ``` (optional language),
  // then everything up to a closing ``` OR end of string (so streaming
  // mid-block still renders as a code block in progress).
  const parts = [];
  const fenceRe = /```([a-zA-Z0-9_+\-.]*)\n?([\s\S]*?)(?:```|$)/g;
  let last = 0, m;
  while ((m = fenceRe.exec(src)) !== null) {
    if (m.index > last) parts.push({t: "text", v: src.slice(last, m.index)});
    parts.push({t: "code", lang: m[1] || "", code: m[2] || ""});
    last = fenceRe.lastIndex;
    // Avoid infinite loop on zero-length match.
    if (m[0].length === 0) fenceRe.lastIndex++;
  }
  if (last < src.length) parts.push({t: "text", v: src.slice(last)});

  return parts.map(p => {
    if (p.t === "code") {
      const langLabel = p.lang
        ? '<span class="lang">' + escapeHtml(p.lang) + "</span>"
        : '<span class="lang">code</span>';
      // Store the raw source in a data attribute so Copy returns the exact
      // text the model produced (with tabs / whitespace intact).
      return '<pre class="codeblock"><div class="codehead">' +
             langLabel +
             '<button class="copy" type="button" aria-label="Copy code">Copy</button>' +
             '</div><code data-source="' + escapeAttr(p.code) + '">' +
             escapeHtml(p.code) + "</code></pre>";
    }
    return renderTextBlock(p.v);
  }).join("");
}

// ─── Clipboard ─────────────────────────────────────────────────────────────
// HTTPS is a secure context, so navigator.clipboard is available. The
// textarea+execCommand fallback below is kept for unusual environments
// (insecure HTTP, very old browsers, clipboard-permission denied).
function copyToClipboard(text, btn) {
  const onDone = () => {
    const original = btn.textContent;
    btn.textContent = "Copied";
    btn.classList.add("copied");
    setTimeout(() => {
      btn.textContent = original;
      btn.classList.remove("copied");
    }, 1200);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(onDone).catch(() => fallbackCopy(text, onDone));
  } else {
    fallbackCopy(text, onDone);
  }
}
function fallbackCopy(text, onDone) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.top = "-1000px";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, ta.value.length);
  try { document.execCommand("copy"); onDone(); }
  catch (e) { /* ignore */ }
  document.body.removeChild(ta);
}
// Event delegation: assistant bubbles are re-rendered on every streaming
// delta, so individual buttons would have to be re-bound each time. One
// listener on #log covers all current and future copy buttons.
log.addEventListener("click", (e) => {
  const btn = e.target.closest("button.copy");
  if (!btn) return;
  const code = btn.closest("pre.codeblock").querySelector("code");
  const text = code.dataset.source != null ? code.dataset.source : code.textContent;
  copyToClipboard(text, btn);
});
function autoResize() {
  input.style.height = "auto";
  input.style.height = Math.min(200, input.scrollHeight) + "px";
}
input.addEventListener("input", autoResize);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});
clearBtn.addEventListener("click", () => {
  history = [];
  saveHistory();
  renderHistory();
  input.focus();
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;

  history.push({ role: "user", content: text });
  saveHistory();
  addBubble("user", text);
  input.value = "";
  autoResize();

  sendBtn.disabled = true;

  const bubble = addBubble("assistant", "");
  bubble.classList.add("cursor");
  let acc = "";

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: history }),
    });
    if (!resp.ok || !resp.body) {
      throw new Error("HTTP " + resp.status);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    let finished = false;
    outer: while (!finished) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // SSE frames are separated by a blank line.
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of frame.split("\n")) {
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trimStart();
          if (payload === "[DONE]") { finished = true; break outer; }
          try {
            const evt = JSON.parse(payload);
            if (evt.done) { finished = true; break outer; }
            if (evt.delta) {
              acc += evt.delta;
              bubble.innerHTML = renderMarkdown(acc);
              log.scrollTop = log.scrollHeight;
            }
            if (evt.error) {
              bubble.parentElement.classList.add("error");
              bubble.textContent = evt.error;
            }
          } catch (err) { /* ignore non-JSON keepalives */ }
        }
      }
    }
    // Release the underlying connection so the browser doesn't keep the
    // request "in flight" after the model has finished streaming.
    try { await reader.cancel(); } catch (e) { /* already closed */ }
  } catch (err) {
    bubble.parentElement.classList.add("error");
    bubble.textContent = "Error: " + err.message;
  } finally {
    bubble.classList.remove("cursor");
    if (acc) {
      history.push({ role: "assistant", content: acc });
      saveHistory();
    }
    sendBtn.disabled = false;
    input.focus();
  }
});
</script>
</body>
</html>
"""


def _get_engines():
    """Return (stt_engine, tts_engine), loading them once per process."""
    global _stt_engine, _tts_engine
    with _engines_lock:
        if _stt_engine is None:
            from stt import STTEngine
            _stt_engine = STTEngine()
            _stt_engine.load()
        if _tts_engine is None:
            from tts import TTSEngine
            _tts_engine = TTSEngine(PIPER_MODEL_PATH)
            _tts_engine.load()
    return _stt_engine, _tts_engine


def _resample_pcm_s16_mono(pcm: bytes, src_sr: int, dst_sr: int) -> bytes:
    """Downsample/upsample S16LE mono PCM. Uses stdlib audioop.ratecv.

    audioop is deprecated but still present through Python 3.13 (Peregrine
    runs 3.12 on Ubuntu 24.04). If it disappears in a future Python we
    fall back to a coarse linear resample so the endpoint stays functional.
    """
    if src_sr == dst_sr or not pcm:
        return pcm
    try:
        import audioop  # noqa: PLC0415
        converted, _ = audioop.ratecv(pcm, 2, 1, src_sr, dst_sr, None)
        return converted
    except ImportError:
        # Fallback: nearest-neighbor pick — quality drop, but audible.
        import array
        samples = array.array("h")
        samples.frombytes(pcm)
        step = src_sr / dst_sr
        out = array.array("h")
        i = 0.0
        n = len(samples)
        while int(i) < n:
            out.append(samples[int(i)])
            i += step
        return out.tobytes()


def _wav_from_pcm(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _pcm_from_wav(wav_bytes):
    """Return (pcm_bytes, sample_rate) from an S16LE mono WAV.

    Raises ValueError on a malformed or non-conforming WAV so the endpoint
    can 400 instead of shipping garbage into Piper's resampler.
    """
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        if wf.getnchannels() != 1:
            raise ValueError(f"expected mono, got {wf.getnchannels()} channels")
        if wf.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit, got {wf.getsampwidth() * 8}-bit")
        pcm = wf.readframes(wf.getnframes())
        return pcm, wf.getframerate()


## Known faster-whisper base.en hallucinations on silence / hum / short
## clips. Case-insensitive exact-match after stripping punctuation.
## Sources: OpenAI Whisper issue tracker + observed on this device.
_WHISPER_HALLUCINATIONS = frozenset(s.lower() for s in (
    "you", "you.",
    "thanks for watching",
    "thanks for watching!",
    "thank you for watching",
    "thank you.",
    "thanks.",
    "music", "music playing",
    "[music]", "[music playing]",
    ".", "!", "?", "-", "--", "...",
    "bye", "bye.", "bye bye", "bye-bye",
    "okay", "ok",
    "uh", "um", "hmm", "mm", "mm.", "hmm.",
    "the", "a", "and",
))
def _is_whisper_hallucination(text: str) -> bool:
    if not text:
        return True
    # If the transcript is only punctuation + whitespace (e.g. ". . . . ."
    # or "!!!" — Whisper's classic output on near-silence), drop it.
    if not any(ch.isalnum() for ch in text):
        return True
    t = text.strip().lower().rstrip(".,!?;:")
    if not t:
        return True
    if t in _WHISPER_HALLUCINATIONS:
        return True
    # Very short single-token utterances that aren't actual commands
    # tend to be noise. 3 chars or fewer with no vowels is basically
    # always garbage from a MEMS mic (short pop, keyboard click, etc.).
    if len(t) <= 3 and not any(ch in "aeiouy" for ch in t):
        return True
    return False


def _wav_header_bytes(sample_rate: int, data_size: int) -> bytes:
    """Standard 44-byte PCM/mono/16-bit WAV header.

    For streaming responses where the total data size isn't known up
    front, pass data_size=0xFFFFFFFF — most decoders (including our
    ESP32 client, which only skips the first 44 bytes and streams the
    rest into I2S) don't inspect the size field.
    """
    import struct
    return (
        b"RIFF"
        + struct.pack("<I", (36 + data_size) & 0xFFFFFFFF)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", data_size & 0xFFFFFFFF)
    )


# Markdown / formatting characters that Piper reads aloud verbatim
# ("asterisk", "pound", "underscore", ...) if left in the LLM output.
# The system prompt asks the model to skip them, but small models
# (llama3.2-1B on Genie NPU) don't reliably comply — so we also strip
# defensively here before every synthesis call.
_MD_CODE_FENCE     = re.compile(r"```[\s\S]*?```")
_MD_INLINE_CODE    = re.compile(r"`([^`]*)`")
_MD_LINK           = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_LINE_STRUCTURE = re.compile(r"^\s*(?:>+|[#]{1,6}|[-*+])\s+", re.MULTILINE)
_MD_STRIP_TRANS    = str.maketrans("", "", "*_`~#|<>{}[]\\")


def _strip_for_tts(text: str) -> str:
    """Remove markdown/formatting characters Piper would read aloud.

    Belt-and-suspenders for the voice pipeline — the system prompt asks
    the LLM to output plain speech, but this catches cases where it
    slips into markdown anyway (headings, bullets, **bold**, `code`,
    [link text](url), etc.).
    """
    if not text:
        return text
    # Fenced code blocks — replace with a spoken placeholder rather than
    # dumping raw code into TTS, which produces a torrent of "backslash n"
    # / "curly brace" audio.
    text = _MD_CODE_FENCE.sub(" (code block) ", text)
    # [text](url) -> text ; then `inline code` -> inline code
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_INLINE_CODE.sub(r"\1", text)
    # Strip line-leading structure: > blockquote, # headings, - / * bullets
    text = _MD_LINE_STRUCTURE.sub("", text)
    # Strip remaining pure-formatting characters. NOT stripped (they carry
    # spoken meaning): & @ % $ + = / and normal sentence punctuation.
    text = text.translate(_MD_STRIP_TRANS)
    # Collapse whitespace introduced by removals.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _stream_piper_pcm(tts, text: str, target_sr: int):
    """Synthesize `text` with Piper and yield 22.05 kHz mono PCM bytes.

    Uses PiperVoice.synthesize() directly (tts.py's underlying engine)
    so we can emit chunks as they're produced instead of waiting for
    the whole sentence to finish. Resamples per-chunk when target_sr
    differs from Piper's native rate.
    """
    text = _strip_for_tts(text)
    if not text:
        return
    if not tts.load():
        return
    src_rate = tts._sample_rate
    resample_state = None
    with tts._lock:
        for chunk in tts._voice.synthesize(text):
            pcm = chunk.audio_int16_bytes
            if not pcm:
                continue
            chunk_sr = chunk.sample_rate or src_rate
            if chunk_sr != target_sr:
                import audioop
                pcm, resample_state = audioop.ratecv(
                    pcm, 2, 1, chunk_sr, target_sr, resample_state
                )
            yield pcm


def _iter_genie_stream(text: str):
    """POST to genie-server /api/chat with stream=True.

    Yields each `response` string delta as it arrives. Raises on
    network failure; caller handles.
    """
    payload = json.dumps({
        "messages": [{"role": "user", "content": text}],
        "system": VOICE_SYSTEM_PROMPT,
        "stream": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        GENIE_URL.rstrip("/") + "/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw in resp:
            line = raw.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("done"):
                break
            delta = evt.get("response") or ""
            if delta:
                yield delta


# Sentence boundary chars — anywhere one of these appears we flush the
# accumulator to Piper. Comma/semicolon are included because Piper
# handles clause-level chunks smoothly and it dramatically cuts the
# perceived latency: first PCM starts flowing after ~15 chars of LLM
# output instead of waiting for the first period.
_SENTENCE_BREAK = set(".!?,;")


def _voice_pipeline(wav_upload: bytes):
    """Run STT → intent/LLM → TTS. Returns (reply_wav, transcript, response_text).

    Any missing engine or model unavailability propagates as a RuntimeError
    so the VoiceHandler can return 503.
    """
    stt, tts = _get_engines()
    if not stt.available:
        raise RuntimeError("STT engine unavailable")
    if not tts.available:
        raise RuntimeError("TTS engine unavailable")

    transcript = stt.transcribe_wav(wav_upload).strip()

    # Whisper base.en frequently hallucinates a handful of common phrases
    # when fed silence, background hum, or too-short clips. If we take
    # the hallucination at face value it triggers the full LLM path
    # (Genie NPU + Piper TTS ≈ 25-30 sec) generating an unwanted menu.
    # Treat these as silence so the fast "I didn't catch that" path fires
    # and the ESP32 gets a response in <1 sec instead of timing out.
    if _is_whisper_hallucination(transcript):
        print(f"[voice] dropping likely hallucination: {transcript!r}", file=sys.stderr)
        transcript = ""

    if not transcript:
        reply_text = "I didn't catch that."
        sr, reply_wav = tts.render_to_wav_bytes(_strip_for_tts(reply_text))
    else:
        canned = _intent_lookup(transcript)
        if canned:
            reply_text = canned
        else:
            enriched = _enrich_text(transcript)
            reply_text = _voice_llm_reply(enriched) or "Sorry, I couldn't get a reply."
        sr, reply_wav = tts.render_to_wav_bytes(_strip_for_tts(reply_text))

    if not reply_wav:
        raise RuntimeError("empty TTS output")

    # Resample WAV to target SR (16 kHz) so the ESP32 codec runs one rate.
    pcm, wav_sr = _pcm_from_wav(reply_wav)
    if wav_sr != VOICE_TARGET_SR:
        pcm = _resample_pcm_s16_mono(pcm, wav_sr, VOICE_TARGET_SR)
    reply_wav = _wav_from_pcm(pcm, VOICE_TARGET_SR)
    return reply_wav, transcript, reply_text


def _voice_llm_reply(text: str) -> str:
    """Non-streaming call to genie-server, collect full response.

    Uses /api/chat with a minimal single-turn message so we can reuse the
    same system-prompt shape the streaming path uses. If the model emits a
    device-control JSON blob, hand it to the intent RPC's /api/command
    endpoint (same as the chat SSE path) and use the confirmation instead.
    """
    messages = [{"role": "user", "content": text}]
    payload = json.dumps({
        "messages": messages,
        "system": VOICE_SYSTEM_PROMPT,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        GENIE_URL.rstrip("/") + "/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            evt = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as e:
        print(f"[voice] LLM call failed: {e}", file=sys.stderr)
        return ""
    reply = (evt.get("message") or {}).get("content") or evt.get("response") or ""
    reply = reply.strip()
    if reply.startswith("{"):
        confirmation = _command_execute(reply)
        return confirmation or "Sorry, I couldn't do that."
    return reply


class VoiceMixin:
    """POST /api/voice, shared by both listeners that serve it.

    - The HTTPS listener (ChatHandler) serves it with the same trust as
      /api/chat: the client is on the LAN and trusts Peregrine's CA. That is
      all a voice terminal needs — the URL and CA Headwaters already holds
      for Overlook's chat — so Headwaters hands both to devices like
      Tiercel and nothing else has to be provisioned.
    - The plain-HTTP listener (VoiceHandler, PEREGRINE_VOICE_PORT) has no
      TLS, so it keeps the bearer token. It stays for terminals that predate
      the HTTPS path.
    """

    def _handle_voice(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            self._json({"error": "missing Content-Length"}, status=411)
            return
        if length > VOICE_MAX_BYTES:
            self._json({"error": "payload too large"}, status=413)
            return
        wav = self.rfile.read(length)

        # Fast path decisions (STT + intent lookup) happen synchronously
        # before we commit to sending headers. That way if any of them
        # fails or short-circuits, we can still return a fixed WAV with
        # a proper Content-Length.
        try:
            stt, tts = _get_engines()
            if not stt.available or not tts.available:
                self._json({"error": "engines unavailable"}, status=503)
                return

            # Bias Whisper toward likely words. Doesn't force these — just
            # makes the model more likely to pick them when they sound
            # close to what was actually said.
            initial_prompt = (
                "Peregrine assistant. "
                "Turn on the lights. What is the "
                "temperature. Weather forecast. Battery level. Water "
                "level. Cabin. Trailer."
            )
            transcript = stt.transcribe_wav(
                wav, initial_prompt=initial_prompt).strip()
            if _is_whisper_hallucination(transcript):
                print(f"[voice] dropping likely hallucination: {transcript!r}",
                      file=sys.stderr)
                transcript = ""

            if not transcript:
                self._send_fixed_wav(tts, "", "I didn't catch that.")
                return

            canned = _intent_lookup(transcript)
            if canned:
                self._send_fixed_wav(tts, transcript, canned)
                return

            # No short-circuit — stream the LLM response
            enriched = _enrich_text(transcript)
            self._send_streamed_llm(tts, transcript, enriched)

        except ValueError as e:
            self._json({"error": f"bad audio: {e}"}, status=400)
        except RuntimeError as e:
            self._json({"error": str(e)}, status=503)
        except Exception as e:  # noqa: BLE001
            print(f"[voice] pipeline error: {e}", file=sys.stderr)
            # If headers already sent, best-effort close; can't send an
            # error body at this point.
            try:
                self._json({"error": "internal error"}, status=500)
            except Exception:
                pass

    def _send_fixed_wav(self, tts, transcript: str, reply_text: str):
        """Synth `reply_text` in full, resample, send one Content-Length'd WAV."""
        print(f"[voice] fast: transcript={transcript!r} -> reply={reply_text!r}",
              file=sys.stderr)
        sr, reply_wav = tts.render_to_wav_bytes(_strip_for_tts(reply_text))
        if not reply_wav:
            raise RuntimeError("empty TTS output")
        pcm, wav_sr = _pcm_from_wav(reply_wav)
        if wav_sr != VOICE_TARGET_SR:
            pcm = _resample_pcm_s16_mono(pcm, wav_sr, VOICE_TARGET_SR)
        reply_wav = _wav_from_pcm(pcm, VOICE_TARGET_SR)

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(reply_wav)))
        self.send_header("X-Peregrine-Transcript",
                         urllib.parse.quote(transcript, safe=""))
        self.send_header("X-Peregrine-Response",
                         urllib.parse.quote(reply_text, safe=""))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(reply_wav)

    def _send_streamed_llm(self, tts, transcript: str, enriched: str):
        """Stream Genie tokens → Piper synth → PCM to the response body.

        No Content-Length header; client reads until socket close
        (esp_http_client + curl both handle this fine). Response text
        header is filled with a placeholder before streaming; the actual
        text is unknown until Genie finishes.
        """
        print(f"[voice] stream: transcript={transcript!r}"
              + (f" enriched={enriched!r}" if enriched != transcript else ""),
              file=sys.stderr)
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("X-Peregrine-Transcript",
                         urllib.parse.quote(transcript, safe=""))
        # Placeholder — the real text isn't known until Genie finishes.
        # UIs that want the exact text can hit a follow-up endpoint later.
        self.send_header("X-Peregrine-Response", "streaming")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        # WAV header first — data_size 0xFFFFFFFF is a "sizeless" sentinel
        # the ESP32 client tolerates (it only skips 44 bytes and streams
        # the rest into I2S).
        self.wfile.write(_wav_header_bytes(VOICE_TARGET_SR, 0xFFFFFFFF))
        self.wfile.flush()

        buf = []
        full_response = []
        looks_like_json = None   # None until first non-ws char seen
        bytes_streamed = 0

        try:
            for delta in _iter_genie_stream(enriched):
                full_response.append(delta)

                # JSON detection — if the model emits {"action":"..."}
                # we can't feed it to Piper. Buffer the whole thing and
                # hand it to /api/command at the end.
                if looks_like_json is None:
                    stripped = "".join(full_response).lstrip()
                    if stripped:
                        looks_like_json = stripped.startswith("{")
                if looks_like_json:
                    continue   # accumulate silently, handle at end

                buf.append(delta)
                text = "".join(buf)
                # Flush at the last sentence boundary in the buffer so we
                # emit whole phrases (Piper handles clause-level chunks
                # cleanly; sub-word chunks sound choppy).
                last = -1
                for i, c in enumerate(text):
                    if c in _SENTENCE_BREAK:
                        last = i
                if last >= 0:
                    to_synth = text[:last + 1]
                    buf = [text[last + 1:]] if last + 1 < len(text) else []
                    for pcm in _stream_piper_pcm(tts, to_synth, VOICE_TARGET_SR):
                        try:
                            self.wfile.write(pcm)
                            bytes_streamed += len(pcm)
                        except (BrokenPipeError, ConnectionResetError):
                            print("[voice] client closed mid-stream", file=sys.stderr)
                            return

            # Flush remaining accumulator OR handle JSON command
            if looks_like_json:
                full = "".join(full_response).strip()
                confirmation = _command_execute(full) or "Sorry, I couldn't do that."
                for pcm in _stream_piper_pcm(tts, confirmation, VOICE_TARGET_SR):
                    try:
                        self.wfile.write(pcm)
                        bytes_streamed += len(pcm)
                    except (BrokenPipeError, ConnectionResetError):
                        return
            elif buf:
                tail = "".join(buf).strip()
                if tail:
                    for pcm in _stream_piper_pcm(tts, tail, VOICE_TARGET_SR):
                        try:
                            self.wfile.write(pcm)
                            bytes_streamed += len(pcm)
                        except (BrokenPipeError, ConnectionResetError):
                            return
        except urllib.error.URLError as e:
            print(f"[voice] Genie stream failed mid-response: {e}", file=sys.stderr)
        finally:
            try:
                self.wfile.flush()
            except Exception:
                pass
            print(f"[voice] streamed {bytes_streamed} bytes of PCM "
                  f"({bytes_streamed/(VOICE_TARGET_SR*2):.1f}s of audio)",
                  file=sys.stderr)

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


# --- HTTP handler ---

class ChatHandler(VoiceMixin, BaseHTTPRequestHandler):
    server_version = "PeregrineChat/1.0"

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_html()
        elif self.path == "/healthz":
            self._serve_json({"status": "ok", "tls": TLS_ENABLED})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/chat":
            self._handle_chat()
        elif self.path == "/api/voice" and TLS_ENABLED:
            # Same trust as /api/chat: no token on the TLS listener. Not
            # offered on the plain-HTTP fallback, where it would be open.
            self._handle_voice()
        else:
            self.send_error(404)

    # ---- handlers ----

    def _serve_html(self):
        body = CHAT_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_chat(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError:
            self._serve_json({"error": "invalid JSON"}, status=400)
            return

        messages = body.get("messages") or []
        if not isinstance(messages, list) or not messages:
            self._serve_json({"error": "missing messages"}, status=400)
            return

        system_prompt = body.get("system") or DEFAULT_SYSTEM_PROMPT
        trimmed = _trim_messages(messages, system_prompt)
        if not trimmed:
            self._serve_json({"error": "no usable messages"}, status=400)
            return

        # Start SSE stream. Force connection close so mobile browsers (notably
        # Firefox mobile) drop their fetch ReadableStream buffer promptly when
        # the model finishes — they otherwise hang on the open socket and the
        # UI shows a stuck blinking cursor after the response is complete.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        # Disable proxy buffering, in case anyone fronts this later.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        try:
            # Try the intent RPC first — the same fast-path the voice loop uses.
            # If it matches, we're done (no LLM call). Otherwise fall through
            # to the LLM and, if the LLM emits a JSON control command, feed it
            # to the RPC /api/command endpoint so lights actually turn on.
            latest_user = _latest_user_text(trimmed)
            if latest_user:
                canned = _intent_lookup(latest_user)
                if canned:
                    self._send_sse({"delta": canned})
                    self._send_sse({"done": True})
                    self._send_sse_raw("data: [DONE]\n\n")
                    return
                # Not a canned answer — see if the message references "here" /
                # "this area" / etc. and needs a location fact injected before
                # the LLM sees it. Rewrites only the newest user turn.
                enriched = _enrich_text(latest_user)
                if enriched != latest_user:
                    trimmed = _replace_last_user(trimmed, enriched)
            self._proxy_genie_stream(trimmed, system_prompt)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _proxy_genie_stream(self, messages, system_prompt):
        """Stream tokens from genie-server's /api/chat and forward as SSE.

        Watches the first non-whitespace character of the stream — if it's
        ``{`` the model is emitting a device-control JSON command, which the
        UI shouldn't show verbatim. We buffer the whole response, hand it to
        the intent RPC's /api/command endpoint (same code path the voice loop
        uses), and stream back the resulting spoken confirmation instead.
        """
        payload = json.dumps({
            "messages": messages,
            "system": system_prompt,
            "stream": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            GENIE_URL.rstrip("/") + "/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        buffered = []          # holds full response when we suspect JSON
        looks_like_json = None # None until we see first non-whitespace char
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                for raw in resp:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("done"):
                        if looks_like_json:
                            full = "".join(buffered)
                            confirmation = _command_execute(full)
                            reply = confirmation or "Sorry, I couldn't do that."
                            self._send_sse({"delta": reply})
                        self._send_sse({"done": True})
                        self._send_sse_raw("data: [DONE]\n\n")
                        return
                    delta = evt.get("response") or ""
                    if not delta:
                        continue
                    if looks_like_json is None:
                        stripped = delta.lstrip()
                        if stripped:
                            looks_like_json = stripped.startswith("{")
                    if looks_like_json:
                        buffered.append(delta)
                    else:
                        self._send_sse({"delta": delta})
        except urllib.error.URLError as e:
            self._send_sse({"error": f"NPU backend unavailable: {e.reason}"})
            self._send_sse_raw("data: [DONE]\n\n")

    def _send_sse(self, obj):
        self._send_sse_raw("data: " + json.dumps(obj) + "\n\n")

    def _send_sse_raw(self, frame):
        self.wfile.write(frame.encode("utf-8"))
        self.wfile.flush()

    def log_message(self, format, *args):
        sys.stdout.write(
            "[web-chat] %s - %s\n" % (self.address_string(), format % args)
        )
        sys.stdout.flush()


class VoiceHandler(VoiceMixin, BaseHTTPRequestHandler):
    """Bearer-token gated voice endpoint on the plain-HTTP voice port."""

    server_version = "PeregrineVoice/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/healthz":
            self._json({"ok": True, "voice_enabled": VOICE_ENABLED})
            return
        self._json({"error": "not found"}, status=404)

    def do_POST(self):
        if self.path != "/api/voice":
            self._json({"error": "not found"}, status=404)
            return
        if not self._auth_ok():
            self._json({"error": "unauthorized"}, status=401)
            return
        self._handle_voice()

    def _auth_ok(self) -> bool:
        if not VOICE_TOKEN:
            return False
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):], VOICE_TOKEN)

    def log_message(self, format, *args):
        sys.stdout.write(
            "[voice] %s - %s\n" % (self.address_string(), format % args)
        )
        sys.stdout.flush()


class HttpsRedirectHandler(BaseHTTPRequestHandler):
    """Tiny HTTP listener that 301-redirects everything to HTTPS.

    Nothing is served in the clear — the CA cert is intentionally NOT
    exposed here. Operators distribute the CA out-of-band by running
    ``peregrine-self-test.sh --show-ca`` (which requires shell access to
    the board) or by ``scp``-ing /home/trailcurrent/certs/ca.pem.
    """

    server_version = "PeregrineChatRedirect/1.0"

    def do_GET(self):
        self._redirect()

    def do_HEAD(self):
        self._redirect()

    def do_POST(self):
        # Don't 301 POSTs — browsers won't replay POST bodies. Return a hint.
        self.send_response(308)
        self.send_header("Location", self._redirect_target())
        self.end_headers()

    def _redirect(self):
        self.send_response(301)
        self.send_header("Location", self._redirect_target())
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirect_target(self):
        # Prefer the hostname the client actually used (so peregrine.local
        # stays peregrine.local in the redirect, avoiding an IP-then-name
        # cert mismatch). Fall back to the configured public hostname.
        host_hdr = self.headers.get("Host", "")
        host = host_hdr.split(":", 1)[0] if host_hdr else PUBLIC_HOSTNAME
        if not host:
            host = self.server.server_address[0]
        port_suffix = "" if HTTPS_PORT == 443 else f":{HTTPS_PORT}"
        return f"https://{host}{port_suffix}{self.path}"

    def log_message(self, format, *args):
        sys.stdout.write(
            "[web-chat-redir] %s - %s\n" % (self.address_string(), format % args)
        )
        sys.stdout.flush()


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded so a slow NPU stream doesn't block the static page or health."""
    allow_reuse_address = True
    daemon_threads = True


def _build_tls_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=TLS_CERT_PATH, keyfile=TLS_KEY_PATH)
    # Sensible cipher posture: let the OpenSSL DEFAULT do the picking but
    # explicitly disable insecure RC4/3DES/null suites.
    ctx.set_ciphers("DEFAULT:!aNULL:!eNULL:!RC4:!3DES")
    return ctx


def _serve_forever_with_tls(server, ctx):
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


def _start_voice_listener():
    """Bind the ESP32 P4 voice endpoint on its own thread.

    Only runs when PEREGRINE_VOICE_TOKEN is set — silence-by-default so the
    endpoint isn't reachable on boxes that haven't provisioned a token yet.
    """
    if not VOICE_ENABLED:
        print("[voice] endpoint disabled (PEREGRINE_VOICE_TOKEN unset)")
        return None
    try:
        server = ThreadingHTTPServer((HOST, VOICE_PORT), VoiceHandler)
    except OSError as e:
        print(f"[voice] cannot bind {HOST}:{VOICE_PORT}: {e}", file=sys.stderr)
        return None
    print(f"[voice] listening on http://{HOST}:{VOICE_PORT} "
          f"(target_sr={VOICE_TARGET_SR}, max_bytes={VOICE_MAX_BYTES})")
    thread = threading.Thread(
        target=server.serve_forever, daemon=True, name="voice-listener",
    )
    thread.start()
    return server


def main():
    voice_server = _start_voice_listener()
    if TLS_ENABLED:
        ctx = _build_tls_context()
        https_server = ThreadingHTTPServer((HOST, HTTPS_PORT), ChatHandler)
        print(f"[web-chat] HTTPS listening on https://{HOST}:{HTTPS_PORT} "
              f"(cert={TLS_CERT_PATH})")
        # Start the HTTPS server on its own thread so we can also bind 80.
        https_thread = threading.Thread(
            target=_serve_forever_with_tls,
            args=(https_server, ctx),
            daemon=True,
        )
        https_thread.start()

        http_server = ThreadingHTTPServer((HOST, HTTP_PORT), HttpsRedirectHandler)
        print(f"[web-chat] HTTP redirector on :{HTTP_PORT} → HTTPS "
              f"(also serves /ca.pem unencrypted)")
        sys.stdout.flush()
        try:
            http_server.serve_forever()
        except KeyboardInterrupt:
            pass
        http_server.server_close()
        https_server.shutdown()
        https_server.server_close()
    else:
        # Plain-HTTP fallback (e.g. dev workstation without certs yet)
        server = ThreadingHTTPServer((HOST, PORT), ChatHandler)
        print(f"[web-chat] listening on http://{HOST}:{PORT} "
              f"(TLS disabled — set WEB_CHAT_TLS_CERT/KEY to enable)")
        sys.stdout.flush()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        server.server_close()
    if voice_server is not None:
        voice_server.shutdown()
        voice_server.server_close()
    print("[web-chat] stopped.")


if __name__ == "__main__":
    main()
