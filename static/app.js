const $ = (s) => document.querySelector(s);
let OVERVIEW = null, CASES = [], current = null, ranStep = 0, runningStep = false, runningAll = false;
let activeStage = 0;
let AGENT_OUTPUTS = {};
const STAGE_TITLES = [
  "Raw data loaded",
  "Agent 1 found suspicious patterns",
  "Agent 2 ranked the cases",
  "Agent 3 recommended actions",
  "Agent 4 wrote the summary",
];
const STAGE_TABS = ["Raw Data", "01 Findings", "02 Ranking", "03 Actions", "04 Summary"];
const STEP_KEYS = ["find", "rank", "act", "summary"];

async function api(path, opts) {
  const r = await fetch(path, { cache: "no-store", ...(opts || {}) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}
const esc = (s) => (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = (n) => "$" + Math.round(n).toLocaleString();

async function boot() {
  OVERVIEW = await api("/api/overview");
  CASES = await api("/api/cases");
  render();
}

function render() {
  $("#brief-title").textContent = OVERVIEW.brief.title;
  $("#brief-sub").textContent = OVERVIEW.brief.subtitle;
  $("#stage-title").textContent = ranStep > 0
    ? (AGENT_OUTPUTS[STEP_KEYS[ranStep - 1]]?.stage_title || STAGE_TITLES[ranStep])
    : STAGE_TITLES[0];

  // stats
  const s = OVERVIEW.stats;
  $("#stats").innerHTML = `
    ${stat(ranStep >= 1 ? s.cases : "—", "cases")}
    ${stat(s.transactions, "transactions")}
    ${stat(ranStep >= 3 ? s.urgent : "—", "urgent")}`;

  $("#btn-run-all").disabled = runningStep || runningAll || ranStep >= STEP_KEYS.length;
  $("#btn-run-all").textContent = runningAll ? `Running ${Math.min(ranStep + 1, STEP_KEYS.length)}/4…` : ranStep > 0 ? "Run remaining agents" : "Run all agents";

  // run steps — one per agent, each shows its role and (once run) what it wrote
  $("#run-steps").innerHTML = OVERVIEW.interaction_history.map((h, i) => `
    <div class="run-step ${i < ranStep ? "done" : ""} ${i === ranStep ? "next" : ""}" data-i="${i}">
      <span class="s">${h.step}</span>
      <span class="rs-body"><span class="t">${esc(h.short)}</span><span class="rs-desc">${esc(h.desc)}</span></span>
      <span class="out">${i < ranStep ? esc((AGENT_OUTPUTS[STEP_KEYS[i]] || {}).history_wrote || h.wrote) : i === ranStep ? (runningStep ? "Running Claude…" : "Run next") : "Waiting"}</span>
    </div>`).join("");
  document.querySelectorAll(".run-step").forEach((el) =>
    el.onclick = () => runStep(parseInt(el.dataset.i)));

  // transactions
  $("#txns tbody").innerHTML = OVERVIEW.transactions_sample.map((t) => `
    <tr><td>${t.txn_id}</td><td>${t.src}</td><td>${t.dst}</td>
        <td>${t.amount.toFixed(2)}</td><td>${t.timestamp}</td><td>${t.device_id}</td></tr>`).join("");
  renderStageTabs();

  // queue appears only after Agent 1 has found cases. Scores/ranking appear after Agent 2.
  $("#queue-panel").hidden = ranStep < 1;
  $("#queue-title").textContent = ranStep < 2 ? "Findings" : "Worst First";
  $("#queue-note").textContent = ranStep < 2 ? `${CASES.length} findings` : `${CASES.length} cases ranked worst-first`;
  $("#queue").innerHTML = CASES.map((c, idx) => `
    <div class="case ${ranStep >= 3 && c.status === "ELEVATED" ? "elevated" : "review"}" data-id="${c.case_id}">
      <div class="ctitle">${ranStep >= 2 ? `#${c.rank} ` : ""}${esc(c.short)}</div>
      <div class="cmeta">${ranStep >= 2 ? `Score ${c.score} · ` : ""}${money(c.exposure)} · ${c.tx_count} tx</div>
      <div class="badge ${ranStep >= 3 ? c.status : ranStep >= 2 ? "RANKED" : "FOUND"}">${ranStep >= 3 ? c.status : ranStep >= 2 ? "RANKED" : "FOUND"}</div>
      <div class="cblurb">${esc(c.blurb)}</div>
      ${ranStep >= 3 ? `<div class="caction">${esc(c.recommendation.action)}</div>` : ""}
    </div>`).join("");
  document.querySelectorAll(".case").forEach((el) => el.onclick = () => openCase(el.dataset.id));

  // ledger/history reveal only the memory objects written so far.
  $("#ledger").innerHTML = OVERVIEW.ledger.slice(0, ranStep).map((l, i) => {
    const out = AGENT_OUTPUTS[STEP_KEYS[i]];
    return `
      <div class="led"><div class="a">${esc(l.agent)}</div>
        <div class="k">${esc(l.key)}</div><div class="d">${esc(out?.ledger_detail || l.detail)}</div>
        ${out?.source ? `<div class="src">source: ${esc(out.source)}</div>` : ""}
      </div>`;
  }).join("");

  // interaction history
  $("#history").innerHTML = OVERVIEW.interaction_history.slice(0, ranStep).map((h, i) => {
    const out = AGENT_OUTPUTS[STEP_KEYS[i]];
    return `
      <div class="hist"><div class="hn">${h.step}</div>
        <div class="hbody">
          <div class="htitle">${esc(h.agent)}</div>
          <div class="rw"><div class="tag">READ</div><div class="val">${esc(h.read)}</div></div>
          <div class="rw"><div class="tag">WROTE</div><div class="val">${esc(out?.history_wrote || h.wrote)}</div></div>
          <div class="rw"><div class="tag">COGNEE KEY</div><div class="val mono">${esc(h.key)}</div></div>
        </div></div>`;
  }).join("");

  $("#summary-panel").hidden = ranStep < 4;
  if (ranStep >= 4) renderSummary();
  if (ranStep < 1) $("#detail").hidden = true;
}

function renderStageTabs() {
  $("#stage-tabs").innerHTML = STAGE_TABS.map((label, i) => `
    <button class="stage-tab ${activeStage === i ? "active" : ""}" ${i > ranStep ? "disabled" : ""} data-stage="${i}">
      ${esc(label)}
    </button>
  `).join("");
  document.querySelectorAll(".stage-tab").forEach((btn) => {
    btn.onclick = () => {
      activeStage = Number(btn.dataset.stage);
      render();
      if (activeStage > 0 && topRingId()) openCase(current || topRingId(), false);
    };
  });
  $("#stage-body").innerHTML = stageBody(activeStage);
}

function caseRows(mode) {
  const llm = AGENT_OUTPUTS[mode === "find" ? "find" : mode === "rank" ? "rank" : "act"];
  if (llm?.cards?.length) {
    return llm.cards.map((c) => `
      <div class="stage-card">
        <div class="stage-card-title">${esc(c.title)}</div>
        <div class="stage-card-meta">${esc(c.badge)} · ${esc(c.meta)} · ${esc(llm.source)}</div>
        <p>${esc(c.body)}</p>
        ${mode === "act" && c.action ? `<div class="stage-action">${esc(c.action)}</div>` : ""}
      </div>
    `).join("");
  }
  return CASES.map((c) => `
    <div class="stage-card">
      <div class="stage-card-title">${mode === "rank" ? `#${c.rank} ` : ""}${esc(c.short)}</div>
      <div class="stage-card-meta">
        ${mode === "find" ? "Found" : mode === "rank" ? `Score ${c.score}` : c.status}
        · ${money(c.exposure)} · ${c.tx_count} tx · ${c.n_accounts} accounts
      </div>
      <p>${esc(c.blurb)}</p>
      ${mode === "act" ? `<div class="stage-action">${esc(c.recommendation.action)}</div>` : ""}
    </div>
  `).join("");
}

function stageBody(stage) {
  if (stage === 0) {
    return `
      <h3>Raw Transaction Sample</h3>
      <table class="txns">
        <thead><tr><th>TXN</th><th>FROM</th><th>TO</th><th>AMOUNT</th><th>TIMESTAMP</th><th>DEVICE</th></tr></thead>
        <tbody>${OVERVIEW.transactions_sample.map((t) => `
          <tr><td>${t.txn_id}</td><td>${t.src}</td><td>${t.dst}</td>
          <td>${t.amount.toFixed(2)}</td><td>${t.timestamp}</td><td>${t.device_id}</td></tr>`).join("")}</tbody>
      </table>`;
  }
  if (stage === 1) {
    const llm = AGENT_OUTPUTS.find;
    return `<h3>${esc(llm?.tab_title || "Agent 1 Output: Findings")}</h3><p class="muted">${esc(llm?.headline || "Suspicious clusters found from graph, timing, amount, and device signals.")}</p>${caseRows("find")}`;
  }
  if (stage === 2) {
    const llm = AGENT_OUTPUTS.rank;
    return `<h3>${esc(llm?.tab_title || "Agent 2 Output: Worst-First Ranking")}</h3><p class="muted">${esc(llm?.headline || "The queue is now sorted by exposure, breadth, device evidence, and velocity.")}</p>${caseRows("rank")}`;
  }
  if (stage === 3) {
    const llm = AGENT_OUTPUTS.act;
    return `<h3>${esc(llm?.tab_title || "Agent 3 Output: Actions")}</h3><p class="muted">${esc(llm?.headline || "Each ranked case now has an operational recommendation.")}</p>${caseRows("act")}`;
  }
  const c = CASES.find((item) => item.case_id === topRingId()) || CASES[0];
  const llm = AGENT_OUTPUTS.summary;
  if (llm?.summary) {
    return `
      <h3>${esc(llm.tab_title || "Agent 4 Output: Summary")}</h3>
      <div class="stage-card">
        <div class="stage-card-title">${esc(llm.summary.top_case || c?.title || "No case selected")}</div>
        <p><strong>Finding:</strong> ${esc(llm.summary.finding)}</p>
        <p><strong>Action:</strong> ${esc(llm.summary.action)}</p>
        <p><strong>Summary:</strong> ${esc(llm.summary.signable_summary)}</p>
        <div class="stage-card-meta">Generated via ${esc(llm.source)}</div>
        <button class="ghost sm" onclick="downloadRing(topRingId())">Download summary</button>
      </div>`;
  }
  return `
    <h3>Agent 4 Output: Summary</h3>
    <div class="stage-card">
      <div class="stage-card-title">${esc(c?.title || "No case selected")}</div>
      <p><strong>Finding:</strong> ${esc(c?.blurb || "")}</p>
      <p><strong>Action:</strong> ${esc(c?.recommendation?.action || "")}</p>
      <p><strong>Summary:</strong> The system found the hidden ring, ranked it first, assigned a review action, and prepared a downloadable analyst summary.</p>
      <button class="ghost sm" onclick="downloadRing(topRingId())">Download summary</button>
    </div>`;
}

const stat = (n, l) => `<div class="stat"><div class="n">${typeof n === "number" ? n.toLocaleString() : n}</div><div class="l">${l}</div></div>`;

// Step-by-step "run each agent one handoff at a time"
function topRingId() {
  const ring = CASES.find((c) => c.kind === "ring");
  return ring ? ring.case_id : null;
}
async function runStep(i) {
  if (i !== ranStep || runningStep) return;
  runningStep = true;
  render();
  const key = STEP_KEYS[i];
  try {
    AGENT_OUTPUTS[key] = await api(`/api/agent/${key}`, { method: "POST" });
  } catch (e) {
    AGENT_OUTPUTS[key] = {
      step: key,
      source: "frontend error",
      tab_title: `Agent ${i + 1} Output`,
      headline: `Could not run Claude agent: ${e.message}`,
      cards: [{ title: "Agent error", meta: "No output", body: e.message, badge: "ERROR" }],
      history_wrote: `Agent failed: ${e.message}`,
      ledger_detail: `Agent failed: ${e.message}`,
    };
  } finally {
    runningStep = false;
  }
  ranStep = Math.max(ranStep, i + 1);
  activeStage = ranStep;
  render();
  const id = current || topRingId();
  if (ranStep >= 1 && id) {
    await openCase(id, false);
    render();
    await openCase(id, false);
  }
}

async function runAllSteps() {
  if (runningStep || runningAll || ranStep >= STEP_KEYS.length) return;
  runningAll = true;
  render();
  try {
    while (ranStep < STEP_KEYS.length) {
      await runStep(ranStep);
    }
  } finally {
    runningAll = false;
    render();
  }
}

async function openCase(id, shouldScroll = true) {
  current = id;
  document.querySelectorAll(".case").forEach((c) => c.classList.toggle("sel", c.dataset.id === id));
  const d = await api(`/api/cases/${id}`);
  if (current !== id) return;
  const c = d.case;
  $("#detail").hidden = false;
  $("#d-title").textContent = c.title;
  $("#d-sub").textContent = `${c.n_accounts} accounts · ${c.tx_count} transactions · ${c.velocity} window`;
  $("#btn-download").style.display = c.kind === "ring" ? "" : "none";
  $("#d-stats").innerHTML =
    stat(ranStep >= 2 ? c.score : "—", "Risk score") + stat(money(c.exposure), "Total moved") +
    stat(c.n_accounts, "Accounts") + `<div class="stat"><div class="n">${c.velocity}</div><div class="l">Velocity</div></div>`;
  drawGraph(d.graph, c.hub);
  $("#d-reasons").innerHTML = c.visible_reasons.map((r) => `<li>${esc(r)}</li>`).join("");
  $("#d-action").textContent = ranStep >= 3
    ? c.recommendation.action + (c.recommendation.urgent ? "  (URGENT)" : "")
    : "Pending Agent 3 action recommendation.";
  $("#answers").innerHTML = "";
  $(".ask").style.display = ranStep >= 4 && c.kind === "ring" ? "" : "none";
  if (shouldScroll) $("#detail").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderSummary() {
  const c = CASES.find((item) => item.case_id === topRingId()) || CASES[0];
  if (!c) return;
  $("#summary-body").innerHTML = `
    <p><strong>Top case:</strong> ${esc(c.title)}</p>
    <p><strong>Why it matters:</strong> ${esc(AGENT_OUTPUTS.summary?.summary?.finding || c.blurb)}</p>
    <p><strong>Recommended action:</strong> ${esc(AGENT_OUTPUTS.summary?.summary?.action || c.recommendation.action)}</p>
    <p><strong>Summary:</strong> ${esc(AGENT_OUTPUTS.summary?.summary?.signable_summary || "Agent 4 can download the signable case summary/report for analyst review.")}</p>
  `;
}

async function ask() {
  const q = $("#ask-input").value.trim();
  if (!q || !current) return;
  $("#btn-ask").textContent = "…";
  try {
    const r = await api(`/api/rings/${current}/ask`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q }),
    });
    $("#answers").insertAdjacentHTML("afterbegin",
      `<div class="ans"><div class="q">Q: ${esc(q)}</div><div>${esc(r.answer)}</div>
       <div class="src">grounded via ${esc(r.source)}</div></div>`);
    $("#ask-input").value = "";
  } catch (e) {
    $("#answers").insertAdjacentHTML("afterbegin", `<div class="ans">Error: ${esc(e.message)}</div>`);
  } finally { $("#btn-ask").textContent = "Ask"; }
}

async function downloadRing(id) {
  await api(`/api/rings/${id}/casepack`, { method: "POST" });
  window.open(`/api/rings/${id}/casepack.html`, "_blank");
  const a = document.createElement("a");
  a.href = `/api/rings/${id}/casepack.md`; a.download = `SAR_${id}.md`;
  document.body.appendChild(a); a.click(); a.remove();
}
async function download() { if (current) downloadRing(current); }

async function reset() {
  await api("/api/reset", { method: "POST" });
  ranStep = 0;
  activeStage = 0;
  current = null;
  AGENT_OUTPUTS = {};
  await boot();
}

async function copyLedger() {
  const text = OVERVIEW.ledger.map((l) => `${l.agent}\n${l.key}\n${l.detail}`).join("\n\n");
  try { await navigator.clipboard.writeText(text); $("#btn-copy").textContent = "Copied ✓"; setTimeout(() => $("#btn-copy").textContent = "Copy ledger", 1500); } catch {}
}

// SVG money-flow graph
function drawGraph(g, hub) {
  const svg = $("#graph"), W = 520, H = 360, cx = W / 2, cy = H / 2, R = 130;
  const nodes = g.nodes;
  if (!nodes.length) { svg.innerHTML = `<text x="${cx}" y="${cy}" text-anchor="middle" fill="#9ca3af" font-size="13">No account-to-account graph for this case</text>`; return; }
  const pos = {};
  nodes.forEach((n, i) => {
    const ang = (2 * Math.PI * i) / nodes.length - Math.PI / 2;
    pos[n.id] = { x: cx + R * Math.cos(ang), y: cy + R * Math.sin(ang) };
  });
  const agg = {};
  g.edges.forEach((e) => {
    const k = e.src + "|" + e.dst;
    agg[k] = agg[k] || { src: e.src, dst: e.dst, n: 0, amt: 0 };
    agg[k].n++; agg[k].amt += e.amount;
  });
  let edges = "", shared = "", nds = "";
  Object.values(agg).forEach((e) => {
    const a = pos[e.src], b = pos[e.dst]; if (!a || !b) return;
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2 - 16;
    edges += `<path d="M${a.x},${a.y} Q${mx},${my} ${b.x},${b.y}" class="ed" marker-end="url(#ar)"/>`;
    edges += `<text x="${mx}" y="${my}" class="el">${e.n}× $${Math.round(e.amt / e.n)}</text>`;
  });
  (g.shared_devices || []).forEach((sd) => {
    for (let i = 0; i < sd.accounts.length; i++) for (let j = i + 1; j < sd.accounts.length; j++) {
      const a = pos[sd.accounts[i]], b = pos[sd.accounts[j]];
      if (a && b) shared += `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" class="sh"/>`;
    }
  });
  nodes.forEach((n) => {
    const p = pos[n.id], r = n.hub ? 24 : 18;
    nds += `<circle cx="${p.x}" cy="${p.y}" r="${r}" class="nd ${n.hub ? "hub" : ""}"/>
      <text x="${p.x}" y="${p.y + 4}" class="nl">${n.id.replace("AC-", "")}</text>
      ${n.hub ? `<text x="${p.x}" y="${p.y - r - 5}" class="hl">HUB</text>` : ""}`;
  });
  svg.innerHTML = `<defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#2f6fd0"/></marker></defs>
    <style>.ed{fill:none;stroke:#2f6fd0;stroke-width:1.5;opacity:.6}.el{fill:#9ca3af;font-size:9px;text-anchor:middle}
    .sh{stroke:#c98a14;stroke-width:1.5;stroke-dasharray:5 4;opacity:.8}
    .nd{fill:#fff;stroke:#2f6fd0;stroke-width:2}.nd.hub{fill:#c23b32;stroke:#c23b32}
    .nl{fill:#1a1f29;font-size:11px;font-weight:700;text-anchor:middle}.nd.hub+.nl,.hub~.nl{fill:#fff}
    .hl{fill:#c23b32;font-size:9px;font-weight:800;text-anchor:middle}</style>
    ${shared}${edges}${nds}`;
}

$("#btn-ask").onclick = ask;
$("#ask-input").addEventListener("keydown", (e) => { if (e.key === "Enter") ask(); });
$("#btn-download").onclick = download;
$("#btn-summary-download").onclick = () => { if (topRingId()) downloadRing(topRingId()); };
$("#btn-run-all").onclick = runAllSteps;
$("#btn-reset").onclick = reset;
$("#btn-restore").onclick = reset;
$("#btn-load").onclick = () => alert("This prototype runs on the bundled Track 02 benchmark (track02_fraud_watch.csv). Swap the file via RINGFINDER_DATA to load your own.");
$("#btn-copy").onclick = copyLedger;
boot();
