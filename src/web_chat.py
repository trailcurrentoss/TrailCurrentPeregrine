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

import json
import os
import ssl
import sys
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn


# --- Config ---
HOST = os.getenv("WEB_CHAT_HOST", "0.0.0.0")
GENIE_URL = os.getenv("GENIE_URL", "http://127.0.0.1:11434")

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

DEFAULT_SYSTEM_PROMPT = os.getenv(
    "WEB_CHAT_SYSTEM",
    "You are Peregrine, a helpful local voice and chat assistant running "
    "on-device for the TrailCurrent platform. Reply concisely.",
)

# Llama3.2-1B-1024-v68 has a 1024-token context. Leave room for the new
# user turn + response. Token counts are estimated as ~4 chars per token
# (close enough for English; we err on the side of more trimming).
MAX_CONTEXT_TOKENS = int(os.getenv("WEB_CHAT_MAX_CONTEXT", "700"))
RESPONSE_RESERVE_TOKENS = 200


def _approx_tokens(text: str) -> int:
    """Rough character-based token estimate. Good enough for trimming."""
    return max(1, len(text) // 4)


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


# --- HTTP handler ---

class ChatHandler(BaseHTTPRequestHandler):
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
            self._proxy_genie_stream(trimmed, system_prompt)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _proxy_genie_stream(self, messages, system_prompt):
        """Stream tokens from genie-server's /api/chat and forward as SSE."""
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
                        self._send_sse({"done": True})
                        self._send_sse_raw("data: [DONE]\n\n")
                        return
                    delta = evt.get("response") or ""
                    if delta:
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


def main():
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
    print("[web-chat] stopped.")


if __name__ == "__main__":
    main()
