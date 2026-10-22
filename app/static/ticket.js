(function () {
  "use strict";
  const root = document.getElementById("ticket-app");
  if (!root) return;
  const id = root.dataset.id;
  const readonly = root.dataset.readonly === "true";
  const $ = (sel) => document.querySelector(sel);
  const { esc, renderMarkdown, parseMessage, relTime } = window.TG;

  let state = null;
  let sending = false;

  // ------------------------------------------------------------------ views
  function show(view) {
    ["generating", "failed", "ticket"].forEach((v) => { $(`#view-${v}`).hidden = v !== view; });
  }

  let genTimer = null, genStarted = Date.now();
  async function poll() {
    try {
      state = await TG.api(`/api/scenarios/${id}`);
    } catch (err) {
      $("#failed-reason").textContent = err.message;
      show("failed");
      return;
    }
    if (state.status === "generating") {
      show("generating");
      const elapsed = (Date.now() - genStarted) / 1000;
      const step = elapsed < 6 ? 0 : elapsed < 25 ? 1 : elapsed < 50 ? 2 : 3;
      document.querySelectorAll(".progress-steps li").forEach((li) => {
        const n = Number(li.dataset.step);
        li.classList.toggle("done", n < step);
        li.classList.toggle("active", n === step);
      });
      genTimer = setTimeout(poll, 2500);
      return;
    }
    if (state.status === "failed") {
      $("#failed-reason").textContent = state.error || "Something went wrong.";
      show("failed");
      return;
    }
    renderTicket();
    show("ticket");
  }

  // ------------------------------------------------------------- rendering
  function attachmentHTML(a) {
    const blob = encodeURIComponent(a.content);
    const lines = a.content.split("\n").length;
    return `<details class="attachment" ${lines <= 18 ? "open" : ""}>
      <summary><span class="file-icon" aria-hidden="true">▤</span><span class="mono">${esc(a.filename)}</span>
        <span class="muted small">${lines} line${lines === 1 ? "" : "s"}</span>
        <a class="small" download="${esc(a.filename)}" href="data:text/plain;charset=utf-8,${blob}">Download</a></summary>
      <pre class="code"><code>${esc(a.content)}</code></pre></details>`;
  }

  function bodyHTML(text) {
    return parseMessage(text).map((p) => p.type === "attachment" ? attachmentHTML(p) : renderMarkdown(p.text)).join("");
  }

  function messageHTML({ role, kind, content, created_at }, opts = {}) {
    const t = state.ticket;
    const isTse = role === "tse";
    const who = isTse ? "You" : t.customer.name;
    const initial = isTse ? "Y" : t.customer.name.slice(0, 1);
    const badge = kind === "resolution" ? '<span class="pill pill-res">Proposed fix</span>' : "";
    return `<li class="msg ${isTse ? "msg-tse" : "msg-customer"} ${opts.streaming ? "streaming" : ""}">
      <span class="avatar ${isTse ? "avatar-tse" : ""}" aria-hidden="true">${esc(initial)}</span>
      <div class="msg-card">
        <div class="msg-head"><span class="strong">${esc(who)}</span>${isTse ? "" : `<span class="muted small">${esc(t.customer.company)}</span>`}${badge}
          <span class="muted small msg-time">${created_at ? esc(relTime(created_at)) : ""}</span></div>
        <div class="msg-body">${opts.raw ? opts.raw : bodyHTML(content)}</div>
      </div></li>`;
  }

  function renderTicket() {
    const t = state.ticket;
    $("#t-ref").textContent = state.ref;
    const diff = $("#t-difficulty");
    diff.textContent = state.difficulty[0].toUpperCase() + state.difficulty.slice(1);
    diff.className = `pill pill-${state.difficulty}`;
    $("#t-spec").textContent = state.spec_label;
    const st = $("#t-status");
    st.textContent = state.status === "closed" ? "Closed" : "Open";
    st.className = `status status-${state.status}`;
    $("#t-subject").textContent = t.subject;
    const pv = state.provenance || {};
    const prov = $("#t-provenance");
    if (prov) {
      const bits = [pv.real_ticket ? "Based on a real solved ticket" : "Based on a hand-written starter pattern"];
      if (pv.solved_month) bits.push(pv.solved_month);
      if (pv.csat === "good") bits.push("customer rated it good");
      bits.push(pv.verified ? "grounding check passed" : "not grounding-checked");
      prov.textContent = bits.join(" · ");
      prov.className = `provenance ${pv.real_ticket && pv.verified ? "ok" : "warn"}`;
    }
    document.title = `${state.ref} · ${t.subject}`;

    $("#c-initial").textContent = t.customer.name.slice(0, 1);
    $("#c-name").textContent = t.customer.name;
    $("#c-title").textContent = t.customer.title;
    $("#c-company").textContent = t.customer.company;
    $("#c-tz").textContent = t.customer.timezone;
    $("#t-priority").innerHTML = `<span class="prio prio-${esc(t.priority)}">${esc(t.priority)}</span>`;
    $("#t-area").textContent = t.product_area;
    $("#t-env").innerHTML = t.environment.map((e) => `<li>${esc(e)}</li>`).join("");

    // Opening ticket = first customer message, with its attachments.
    const opening = t.body + t.attachments.map((a) => `\n\`\`\`attachment filename="${a.filename}"\n${a.content}\n\`\`\`\n`).join("");
    const thread = [messageHTML({ role: "customer", content: opening, created_at: state.created_at })]
      .concat(state.messages.map((m) => messageHTML(m)));
    $("#thread").innerHTML = thread.join("");

    renderSandbox();
    renderProgress();
    const closed = state.status === "closed";
    $("#composer").hidden = closed || readonly;
    $("#btn-close").hidden = closed || readonly;
    if (closed) renderGrade();
  }

  function renderSandbox() {
    const sb = state.sandbox;
    const steps = sb.repro_steps && sb.repro_steps.length
      ? `<details class="repro"><summary>Reproduction notes</summary><ol>${sb.repro_steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol></details>` : "";
    if (sb.needed) {
      $("#sandbox-body").innerHTML = `<p class="small">${esc(sb.template_title)}</p>
        <a class="btn btn-primary btn-block" href="/api/scenarios/${id}/sandbox.zip">Download sandbox (.zip)</a>
        <p class="muted small">Unzip it, set <code>DD_API_KEY</code> to a <strong>sandbox org</strong> key in <code>.env</code>, then <code>docker compose up -d --build</code>. The included README, <code>verify.sh</code> and <code>reset.sh</code> walk you through it.</p>${steps}`;
    } else {
      $("#sandbox-body").innerHTML = `<p class="muted small">No local sandbox for this one. Reproduce it in your Datadog sandbox org.</p>
        <a class="btn btn-ghost btn-block" href="/api/scenarios/${id}/repro.md">Download repro guide</a>${steps}`;
    }
  }

  function renderProgress() {
    const total = state.facts_total || 0, got = state.facts_revealed || 0;
    $("#facts-meter").style.width = total ? `${Math.round((got / total) * 100)}%` : "0%";
    $("#facts-text").textContent = total
      ? `You've uncovered ${got} of ${total} key facts the customer knows.`
      : "";
  }

  function scoreClass(n) { return n >= 75 ? "good" : n >= 50 ? "ok" : "low"; }

  function realApproachHTML(src, g) {
    const a = src.tse_approach || {}, p = src.provenance || {};
    const list = (xs) => (xs && xs.length ? `<ul>${xs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "");
    if (src.kind !== "distilled") {
      return `<details class="answer" open><summary>How the real TSE solved it</summary><p class="muted small">This ticket came from a hand-written starter pattern, not a real ticket, so there's no real TSE approach to compare against.</p>${g.approach_comparison ? `<p>${esc(g.approach_comparison)}</p>` : ""}</details>`;
    }
    const badge = p.csat === "good" ? `<span class="pill pill-low">Customer rated it good</span>` : "";
    return `<details class="answer" open><summary>How the real TSE solved it ${badge}</summary>
      ${g.approach_comparison ? `<h3>You vs. the real TSE</h3><p>${esc(g.approach_comparison)}</p>` : ""}
      ${src.root_cause ? `<h3>Real root cause</h3><p>${esc(src.root_cause)}</p>` : ""}
      ${src.resolution_path && src.resolution_path.length ? `<h3>Real resolution</h3><ol>${src.resolution_path.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>` : ""}
      ${a.summary ? `<h3>Their approach</h3><p>${esc(a.summary)}</p>` : ""}
      ${a.steps && a.steps.length ? `<h3>Steps they took</h3><ol>${a.steps.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>` : ""}
      ${a.key_questions && a.key_questions.length ? `<h3>Questions they asked</h3>${list(a.key_questions)}` : ""}
      ${a.communication && a.communication.length ? `<h3>Why their replies landed well</h3>${list(a.communication)}` : ""}
      ${a.customer_feedback ? `<h3>What the customer said about it</h3><blockquote>${esc(a.customer_feedback)}</blockquote>` : ""}
      <p class="muted small">From a real ticket solved in ${esc(p.solved_month || "recent months")}${p.complexity ? `, rated ${esc(p.complexity)} complexity` : ""}. Anonymized: no names, companies or IDs.</p>
    </details>`;
  }

  function renderGrade() {
    const g = state.grade, truth = state.truth;
    if (!g) return;
    const crit = [["diagnosis", "Diagnosis"], ["information_gathering", "Information gathering"], ["communication", "Communication"], ["resolution", "Resolution"]];
    const revealed = new Set(state.revealed_fact_ids || []);
    $("#grade-panel").innerHTML = `
      <div class="grade-top">
        <div class="score-ring score-${scoreClass(g.overall)}"><span>${g.overall}</span><small>/100</small></div>
        <div><h2>${g.resolved ? "Resolved" : "Not resolved"}</h2><p>${esc(g.overall_summary)}</p></div>
      </div>
      <div class="crit-grid">${crit.map(([k, label]) => `
        <div class="crit"><div class="crit-head"><span>${label}</span><span class="dots" aria-label="${g[k].score} of 5">${"●".repeat(g[k].score)}${"○".repeat(5 - g[k].score)}</span></div>
        <p class="small">${esc(g[k].justification)}</p></div>`).join("")}</div>
      <div class="two-col">
        <div><h3>Strengths</h3><ul>${g.strengths.map((s) => `<li>${esc(s)}</li>`).join("")}</ul></div>
        <div><h3>To improve</h3><ul>${g.improvements.map((s) => `<li>${esc(s)}</li>`).join("")}</ul></div>
      </div>
      <p class="small"><strong>Escalation:</strong> ${esc(g.escalation_judgement)}</p>
      <details class="answer" open><summary>Answer key</summary>
        <h3>Root cause</h3><p>${esc(truth.root_cause)}</p>
        <h3>Resolution</h3><ol>${truth.resolution_steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
        ${truth.acceptable_alternatives.length ? `<h3>Also acceptable</h3><ul>${truth.acceptable_alternatives.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>` : ""}
        <h3>Hidden facts</h3><ul class="facts">${truth.facts.map((f) => `<li class="${revealed.has(f.id) ? "found" : "missed"}"><span class="fact-mark">${revealed.has(f.id) ? "✓" : "✗"}</span>${esc(f.statement)} <span class="muted small">(revealed when ${esc(f.reveal_when)})</span></li>`).join("")}</ul>
        ${truth.red_herrings.length ? `<h3>Red herrings</h3><ul>${truth.red_herrings.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>` : ""}
        <h3>Escalation guidance</h3><p>${esc(truth.escalation.replace("_", " "))}: ${esc(truth.escalation_reason)}</p>
        ${truth.doc_links.length ? `<h3>Docs</h3><ul>${truth.doc_links.map((u) => `<li><a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(u)}</a></li>`).join("")}</ul>` : ""}
        <h3>Learning objectives</h3><ul>${truth.learning_objectives.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>
        <p class="muted small">Why this is ${esc(state.difficulty)}: ${esc(state.difficulty_rationale || "")}</p>
      </details>
      ${realApproachHTML(truth.source || {}, g)}
      ${(state.ungrounded || []).length ? `<details class="answer"><summary>Simulation caveats</summary><p class="small">The simulated customer said these things, which the source ticket doesn't support. The grade doesn't penalize you for them.</p><ul>${state.ungrounded.flatMap((u) => u.details).map((d) => `<li>${esc(d)}</li>`).join("")}</ul></details>` : ""}
      <details class="answer"><summary>What a strong first reply looks like</summary><div class="msg-body">${renderMarkdown(g.model_first_reply)}</div></details>`;
    $("#grade-panel").hidden = false;
  }

  // -------------------------------------------------------------- sending
  async function send(kind) {
    if (sending) return;
    const box = $("#reply");
    const content = box.value.trim();
    const errEl = $("#composer-error");
    errEl.textContent = "";
    if (!content) { errEl.textContent = "Write a reply first."; return; }
    sending = true;
    setComposerBusy(true);

    let res;
    try {
      res = await fetch(`/api/scenarios/${id}/messages`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": TG.csrf() },
        body: JSON.stringify({ content, kind }),
      });
    } catch (err) {
      errEl.textContent = "Network error. Your reply wasn't sent.";
      sending = false; setComposerBusy(false); return;
    }
    if (res.redirected) {
      window.location.href = `/login?next=${encodeURIComponent(location.pathname)}`;
      return;
    }
    if (!res.ok) {
      let msg = `Couldn't send (${res.status}).`;
      try { msg = (await res.json()).error || msg; } catch (_) { }
      errEl.textContent = msg;
      sending = false; setComposerBusy(false); return;
    }
    box.value = "";

    const thread = $("#thread");
    let streamEl = null, streamText = "";
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    const handle = (event, data) => {
      if (event === "tse") {
        state.messages.push(data);
        thread.insertAdjacentHTML("beforeend", messageHTML(data));
        thread.insertAdjacentHTML("beforeend", messageHTML({ role: "customer", content: "" }, { streaming: true, raw: '<span class="typing"><i></i><i></i><i></i></span>' }));
        streamEl = thread.lastElementChild.querySelector(".msg-body");
        scrollToEnd();
      } else if (event === "delta") {
        streamText += data.text;
        if (streamEl) streamEl.innerHTML = bodyHTML(streamText);
        scrollToEnd(true);
      } else if (event === "done") {
        state.messages.push(data);
        state.facts_revealed = undefined; // refreshed below
        if (streamEl) {
          const li = streamEl.closest(".msg");
          li.outerHTML = messageHTML(data);
        }
        refreshProgress();
      } else if (event === "error") {
        errEl.textContent = data.message;
        if (streamEl) streamEl.closest(".msg").remove();
      }
    };
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
          const ev = (raw.match(/^event: (.*)$/m) || [])[1] || "message";
          const dataLine = (raw.match(/^data: (.*)$/m) || [])[1];
          if (dataLine) handle(ev, JSON.parse(dataLine));
        }
      }
    } catch (err) {
      errEl.textContent = "The connection dropped. Reload to see the latest reply.";
    }
    sending = false;
    setComposerBusy(false);
    box.focus();
  }

  async function refreshProgress() {
    try {
      const fresh = await TG.api(`/api/scenarios/${id}`);
      state.facts_revealed = fresh.facts_revealed;
      state.facts_total = fresh.facts_total;
      renderProgress();
    } catch (_) { }
  }

  function scrollToEnd(onlyIfNear) {
    const nearBottom = window.innerHeight + window.scrollY > document.body.scrollHeight - 300;
    if (!onlyIfNear || nearBottom) $("#composer").scrollIntoView({ behavior: "smooth", block: "end" });
  }

  function setComposerBusy(busy) {
    $("#btn-send").disabled = busy;
    $("#btn-resolution").disabled = busy;
    $("#btn-send").textContent = busy ? "Customer is typing…" : "Send reply";
  }

  // --------------------------------------------------------------- close
  async function closeTicket() {
    const btn = $("#btn-close");
    btn.disabled = true;
    btn.textContent = "Grading… (about 30s)";
    try {
      state = await TG.api(`/api/scenarios/${id}/close`, { method: "POST", body: "{}" });
      renderTicket();
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (err) {
      $("#composer-error").textContent = err.message;
      btn.disabled = false;
      btn.textContent = "Close ticket & get graded";
    }
  }

  // --------------------------------------------------------------- wire up
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); send("reply"); });
  $("#btn-resolution").addEventListener("click", () => send("resolution"));
  $("#reply").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send("reply"); } });
  const dlg = $("#confirm-close");
  $("#btn-close").addEventListener("click", () => (dlg.showModal ? dlg.showModal() : closeTicket()));
  dlg.addEventListener("close", () => { if (dlg.returnValue === "confirm") closeTicket(); });

  poll();
})();
