/* Shared helpers: CSRF-aware fetch, a small safe Markdown renderer, relative times. */
(function () {
  "use strict";
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || "";

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      credentials: "same-origin",
      ...opts,
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf(), ...(opts.headers || {}) },
    });
    if (res.redirected && new URL(res.url).pathname === "/login") {
      window.location.href = `/login?next=${encodeURIComponent(location.pathname)}`;
      throw new Error("Your session expired. Redirecting to sign in…");
    }
    let body = null;
    try { body = await res.json(); } catch (_) { /* non-JSON */ }
    if (res.ok && body === null) throw new Error("Unexpected response from the server. Reload the page.");
    if (!res.ok) {
      const err = new Error((body && body.error) || `Request failed (${res.status})`);
      err.status = res.status;
      throw err;
    }
    return body;
  }

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function inline(text) {
    // text is already escaped
    return text
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\s][^*]*)\*(?=[\s).,!?]|$)/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  }

  /* Split a message into text and attachment parts. Attachments use a fenced block:
     ```attachment filename="x.log" ... ``` */
  function parseMessage(src) {
    const parts = [];
    const re = /```attachment\s+filename="([^"]+)"[^\n]*\n([\s\S]*?)(?:```|$)/g;
    let last = 0, m;
    while ((m = re.exec(src))) {
      if (m.index > last) parts.push({ type: "text", text: src.slice(last, m.index) });
      parts.push({ type: "attachment", filename: m[1], content: m[2].replace(/\n$/, "") });
      last = re.lastIndex;
    }
    if (last < src.length) parts.push({ type: "text", text: src.slice(last) });
    return parts;
  }

  function renderMarkdown(src) {
    const lines = String(src || "").replace(/\r\n/g, "\n").split("\n");
    let html = "", i = 0;
    while (i < lines.length) {
      const line = lines[i];
      const fence = line.match(/^```(.*)$/); // any info string ("```yaml gateway config")
      if (fence) {
        const buf = [];
        i++;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) buf.push(lines[i++]);
        i++;
        html += `<pre class="code"><code>${esc(buf.join("\n"))}</code></pre>`;
        continue;
      }
      if (/^\s*([-*]|\d+[.)])\s+/.test(line)) {
        const ordered = /^\s*\d+[.)]/.test(line);
        const items = [];
        while (i < lines.length && /^\s*([-*]|\d+[.)])\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*([-*]|\d+[.)])\s+/, ""));
          i++;
        }
        const tag = ordered ? "ol" : "ul";
        html += `<${tag}>${items.map((t) => `<li>${inline(esc(t))}</li>`).join("")}</${tag}>`;
        continue;
      }
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) { html += `<p class="md-h"><strong>${inline(esc(h[2]))}</strong></p>`; i++; continue; }
      if (/^>\s?/.test(line)) {
        const buf = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
        html += `<blockquote>${inline(esc(buf.join(" ")))}</blockquote>`;
        continue;
      }
      if (!line.trim()) { i++; continue; }
      const buf = [lines[i++]]; // always consume one line, so no input can stall the loop
      while (i < lines.length && lines[i].trim() && !/^```/.test(lines[i]) && !/^\s*([-*]|\d+[.)])\s+/.test(lines[i]) && !/^#{1,4}\s/.test(lines[i])) {
        buf.push(lines[i++]);
      }
      html += `<p>${buf.map((l) => inline(esc(l))).join("<br>")}</p>`;
    }
    return html;
  }

  function relTime(iso) {
    const d = new Date(iso), s = (Date.now() - d.getTime()) / 1000;
    if (isNaN(s)) return "";
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  }

  function refreshTimes(root = document) {
    root.querySelectorAll("time[data-relative]").forEach((t) => { t.textContent = relTime(t.getAttribute("datetime")); t.title = new Date(t.getAttribute("datetime")).toLocaleString(); });
  }
  document.addEventListener("DOMContentLoaded", () => refreshTimes());

  window.TG = { api, esc, renderMarkdown, parseMessage, relTime, refreshTimes, csrf };
})();
