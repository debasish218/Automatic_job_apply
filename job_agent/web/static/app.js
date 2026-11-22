"use strict";
// ---------------------------------------------------------------- helpers
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : null);
const link = (u, label) => (safeUrl(u) ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(label || u)}</a>` : esc(label || u || "—"));
const scoreCls = (s) => (s == null ? "" : s >= 75 ? "hi" : s >= 55 ? "mid" : "lo");
const scoreBadge = (s) => (s == null ? '<span class="tag">—</span>' : `<span class="score ${scoreCls(s)}">${s}</span>`);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", "X-Requested-With": "job-assistant", ...(opts.headers || {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (bad ? " bad" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = "toast"), bad ? 6000 : 3000);
}
async function busy(btn, label, fn) {
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span> ${esc(label)}`;
  try { return await fn(); } catch (e) { toast(e.message, true); } finally { btn.disabled = false; btn.innerHTML = old; }
}

// ---------------------------------------------------------------- state
let view = "ready";
let status = null;
let lastEventId = 0;
const events = [];
let currentJob = null;

// ---------------------------------------------------------------- status / header
async function refreshStatus() {
  try { status = await api("/api/status"); } catch { return; }
  const s = status.stats, g = status.gemini, gm = status.gmail, sx = status.searxng, w = status.worker;
  $("#profile-line").textContent = `${status.profile.name} · ${status.profile.experience} · ${status.profile.resumes.length} resume versions`;
  const avail = g.models.filter((m) => m.available).length;
  const pill = (cls, text, title = "") => `<span class="pill ${cls}" title="${esc(title)}"><span class="dot"></span>${esc(text)}</span>`;
  $("#pills").innerHTML =
    pill(avail ? "ok" : "bad", `Gemini · ${avail}/${g.models.length} models available`, g.models.map((m) => `${m.model}: ${m.available ? "available" : m.reason} (used today ${m.used_today})`).join("\n")) +
    pill(gm.ready ? "ok" : "bad", gm.ready ? `Gmail · ${gm.account}` : "Gmail not connected", gm.detail) +
    pill(sx.ok ? "ok" : "bad", sx.ok ? "Search · SearXNG" : "Search offline", sx.detail) +
    pill(w.state === "running" ? "ok" : w.state === "paused_quota" || w.state === "error" ? "bad" : "warn", `Discovery · ${w.state.replace("_", " ")}`, w.message);
  const running = ["running", "stopping", "waiting"].includes(w.state);
  const btn = $("#btn-run");
  btn.textContent = w.state === "stopping" ? "Stopping…" : running ? "Stop" : "Start discovery";
  btn.className = "btn " + (running ? "danger" : "primary");
  btn.disabled = w.state === "stopping";
  $("#step").textContent = running ? w.step : w.message || "";
  const stat = (v, k) => `<div class="stat"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`;
  $("#stats").innerHTML =
    stat(s.total || 0, "Jobs tracked") + stat(s.companies || 0, "Companies") + stat(s.pending_review || 0, "Matches to review") +
    stat(s.emails_sent || 0, "Emails sent") + stat(`${s.sent_today || 0}/${status.daily_limit}`, "Sent today") +
    stat(s.queries_run || 0, "Searches run") + stat(g.calls, "Gemini calls (session)");
  $("#c-matches").textContent = s.pending_review || "";
  $("#c-sent").textContent = s.emailed || "";
}

$("#btn-run").addEventListener("click", async (e) => {
  const running = ["running", "waiting"].includes(status?.worker.state);
  await busy(e.currentTarget, running ? "Stopping" : "Starting", () => api(running ? "/api/discovery/stop" : "/api/discovery/start", { method: "POST" }));
  refreshStatus();
});

// ---------------------------------------------------------------- events
async function pollEvents() {
  try {
    const rows = await api(`/api/events?after=${lastEventId}`);
    if (rows.length) {
      lastEventId = rows[rows.length - 1].id;
      events.push(...rows);
      if (events.length > 600) events.splice(0, events.length - 600);
      if (view === "activity") renderActivity();
      if (rows.some((r) => r.level === "good") && ["ready", "matches"].includes(view)) loadList();
    }
  } catch { /* server restarting */ }
}

// ---------------------------------------------------------------- list views
document.querySelectorAll("#tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    view = b.dataset.view;
    loadList();
  })
);
$("#search").addEventListener("input", () => renderRows());

let rows = [];
async function loadList() {
  $("#search").parentElement.style.display = view === "activity" ? "none" : "";
  if (view === "activity") return renderActivity();
  try {
    rows = view === "companies" ? await api("/api/companies") : await api(`/api/jobs?view=${view}`);
  } catch (e) { rows = []; toast(e.message, true); }
  if (view === "ready") $("#c-ready").textContent = rows.length || "";
  renderRows();
}

function renderRows() {
  const q = $("#search").value.toLowerCase();
  const list = rows.filter((r) => !q || JSON.stringify(r).toLowerCase().includes(q));
  const el = $("#list");
  if (view === "companies") {
    el.innerHTML = list.length ? `<table><thead><tr><th>Company</th><th>Jobs</th><th>Best match</th><th>To review</th><th>Emailed</th><th>Domain</th></tr></thead><tbody>${list
      .map((c) => `<tr data-company="${esc(c.company)}"><td><b>${esc(c.company)}</b></td><td>${c.jobs}</td><td>${scoreBadge(c.best_score)}</td><td>${c.pending || 0}</td><td>${c.emailed || 0}</td><td class="sub">${esc(c.domain || "")}</td></tr>`)
      .join("")}</tbody></table>` : `<div class="empty">No companies yet — start discovery.</div>`;
    el.querySelectorAll("tr[data-company]").forEach((tr) => tr.addEventListener("click", () => {
      const name = tr.dataset.company;
      document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("active"));
      view = "all";
      $("#search").value = name;
      api("/api/jobs?view=all").then((r) => { rows = r; renderRows(); });
    }));
    return;
  }
  if (!list.length) {
    const msg = { ready: "No drafted emails waiting. Matches without a recruiter email are under “All matches”.", matches: "No matches to review yet — press Start discovery.", sent: "Nothing sent yet.", filtered: "Nothing filtered yet.", skipped: "Nothing skipped." }[view] || "Nothing here.";
    el.innerHTML = `<div class="empty">${esc(msg)}</div>`;
    return;
  }
  el.innerHTML = `<table><thead><tr><th>Score</th><th>Role</th><th>Location</th><th>Experience</th><th>Source</th><th>Contacts</th><th>Status</th></tr></thead><tbody>${list
    .map((r) => {
      const st = r.email_status === "draft" ? '<span class="tag info">Draft ready</span>' : r.email_status === "sent" ? '<span class="tag good">Sent</span>'
        : r.status === "pending_review" ? '<span class="tag warn">Needs contact</span>' : `<span class="tag">${esc(r.status.replace("_", " "))}</span>`;
      const reason = ["filtered_out", "duplicate", "error", "skipped"].includes(r.status) ? `<div class="sub">${esc(r.status_reason || "")}</div>` : "";
      return `<tr data-id="${r.id}"><td>${scoreBadge(r.score)}</td><td><b>${esc(r.title)}</b><div class="sub">${esc(r.company)}</div>${reason}</td>
        <td>${esc(r.location || "—")}</td><td class="sub">${esc((r.experience_req || "—").slice(0, 60))}</td><td><span class="tag">${esc(r.site || r.source)}</span></td>
        <td>${r.contacts ? `${r.contacts} <span class="sub">(${r.emails_found} email)</span>` : '<span class="sub">—</span>'}</td><td>${st}</td></tr>`;
    }).join("")}</tbody></table>`;
  el.querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => openJob(+tr.dataset.id)));
}

function renderActivity() {
  const models = status ? status.gemini.models : [];
  $("#list").innerHTML = `<div style="padding:12px 14px;border-bottom:1px solid var(--border)"><b>Gemini models</b>
    <div class="chips" style="margin-top:6px">${models.map((m) => `<span class="tag ${m.available ? "good" : "bad"}" title="${esc(m.reason || "")}${m.until ? " until " + esc(m.until) : ""}">${esc(m.model)} · ${m.available ? "ok" : esc(m.reason)} · ${m.used_today} today</span>`).join("")}</div></div>
    <div class="feed">${events.slice().reverse().map((e) => `<div class="${esc(e.level)}"><span class="ts">${esc(e.ts.replace("T", " "))}</span>${esc(e.message)}</div>`).join("") || '<div class="empty">No activity yet.</div>'}</div>`;
}

// ---------------------------------------------------------------- job drawer
function openDrawer() { $("#drawer").classList.add("open"); $("#scrim").classList.add("open"); }
function closeDrawer() { $("#drawer").classList.remove("open"); $("#scrim").classList.remove("open"); currentJob = null; }
$("#scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { if ($("#modal").classList.contains("open")) closeModal(); else closeDrawer(); } });

async function openJob(id) {
  openDrawer();
  $("#drawer-body").innerHTML = '<div class="empty"><span class="spinner"></span> Loading…</div>';
  try { renderJob(await api(`/api/jobs/${id}`)); } catch (e) { $("#drawer-body").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

const VERIF = {
  verified_on_page: ["good", "Verified on page"], search_snippet: ["warn", "Seen in search result"],
  linkedin_search_result: ["info", "LinkedIn search result"], user_provided: ["info", "Added by you"],
};

function renderJob(d) {
  currentJob = d;
  const j = d.job, a = d.analysis, e = d.email;
  const contacts = d.contacts.map((c) => {
    const [cls, label] = VERIF[c.verification] || ["", c.verification];
    const dm = c.email ? (c.domain_match === 1 ? '<span class="tag good">company domain</span>' : c.domain_match === 0 ? '<span class="tag warn">other domain</span>' : "") : "";
    return `<div class="contact"><div>
        <b>${esc(c.name || (c.kind || "").replace("_", " ") || "Contact")}</b> <span class="sub">${esc(c.designation || "")}</span><br>
        ${c.email ? `<code>${esc(c.email)}</code> ` : '<span class="sub">no email published</span> '}${dm}
        <span class="tag ${cls}">${esc(label)}</span> ${c.linkedin ? link(c.linkedin, "LinkedIn") : ""}
        <div class="evidence">“${esc(c.evidence || "")}” — ${link(c.source_url, "source")}</div></div>
      <div>${c.email && j.status !== "emailed" ? `<button class="btn small" data-use="${c.id}">Write email</button>` : ""}</div></div>`;
  }).join("") || '<div class="sub">No contacts found yet. Recruiter emails are rarely published — use “Find contacts”, add one you know, or apply via the posting.</div>';

  const skills = (arr, cls) => (arr || []).map((s) => `<span class="tag ${cls}">${esc(s)}</span>`).join("") || '<span class="sub">—</span>';
  const emailBlock = e ? `
    <div class="card">
      <div class="row"><div><label>To</label><input id="em-to" type="email" value="${esc(e.recipient)}" ${e.status !== "draft" ? "disabled" : ""}></div>
        <div><label>Attach resume</label><select id="em-resume" ${e.status !== "draft" ? "disabled" : ""}>${d.resumes.map((r) => `<option ${e.attachment_name === r ? "selected" : ""}>${esc(r)}</option>`).join("")}</select></div></div>
      <label>Subject</label><input id="em-subject" type="text" value="${esc(e.subject)}" ${e.status !== "draft" ? "disabled" : ""}>
      <label>Body</label><textarea id="em-body" ${e.status !== "draft" ? "disabled" : ""}>${esc(e.body)}</textarea>
      <div class="sub" style="margin-top:6px">Status: <b>${esc(e.status)}</b>${e.sent_at ? ` · sent ${esc(e.sent_at)}` : ""}${e.model ? ` · written by ${esc(e.model)}` : ""}${e.error ? ` · <span style="color:var(--bad)">last error: ${esc(e.error)}</span>` : ""}</div>
      ${e.status === "draft" ? `<div class="d-actions"><button class="btn good" id="btn-approve">Review &amp; send…</button><button class="btn" id="btn-save">Save edits</button><button class="btn" id="btn-regen">Rewrite with Gemini</button></div>` : ""}
    </div>` : `<div class="sub">No email yet. ${d.contacts.some((c) => c.email) ? "Pick a contact above and press “Write email”." : "Add or find a contact with an email first."}</div>`;

  $("#drawer-body").innerHTML = `
    <div class="d-head"><div><h2>${esc(j.title)}</h2><div><b>${esc(j.company)}</b> · ${esc(j.location || "—")}</div>
      <div class="sub">${esc(j.site || j.source)} · discovered ${esc(j.discovered_at)} · ${j.posted_at ? "posted " + esc(j.posted_at) : "posting date unknown"}</div></div>
      <button class="close" id="btn-close" aria-label="Close">×</button></div>
    <div class="d-actions">${link(j.url, "Open job posting ↗")}
      ${j.status === "skipped" || j.status === "filtered_out" ? '<button class="btn small" id="btn-restore">Move to matches</button>' : j.status !== "emailed" ? '<button class="btn small" id="btn-skip">Skip this job</button>' : ""}
      <span class="tag">${esc(j.status.replace("_", " "))}</span><span class="sub">${esc(j.status_reason || "")}</span></div>

    ${a ? `<div class="section"><h3>Resume match</h3><div class="grid2">
      <div class="k">Score</div><div>${scoreBadge(a.score)} <span class="tag">${esc(a.relevance)} relevance</span> <span class="tag ${a.recommendation === "apply" ? "good" : a.recommendation === "consider" ? "warn" : "bad"}">${esc(a.recommendation)}</span> <span class="sub">${a.model ? "by " + esc(a.model) : ""}</span></div>
      <div class="k">Required experience</div><div>${esc(a.required_experience)}</div>
      <div class="k">Your experience</div><div>${esc(a.experience_comparison)}</div>
      <div class="k">Matching skills</div><div class="chips">${skills(a.matching_skills, "good")}</div>
      <div class="k">Missing skills</div><div class="chips">${skills(a.missing_skills, "bad")}</div>
      <div class="k">Why</div><div>${esc(a.reasoning)}</div>
      <div class="k">Best resume</div><div>${esc(a.best_resume)}</div></div></div>` : `<div class="section"><div class="sub">Not analysed.</div></div>`}

    <div class="section"><h3>Recruiter &amp; contacts</h3>${contacts}
      <div class="d-actions"><button class="btn small" id="btn-find">Find contacts again</button><button class="btn small" id="btn-add">Add contact manually</button></div>
      <div id="add-form" style="display:none" class="card"><div class="row"><div><label>Email</label><input id="add-email" type="email" placeholder="name@company.com"></div><div><label>Name (optional)</label><input id="add-name" type="text"></div></div>
        <label>Where you found it (optional URL)</label><input id="add-src" type="text"><div class="d-actions"><button class="btn primary small" id="btn-add-save">Add &amp; write email</button></div></div></div>

    <div class="section"><h3>Email</h3>${emailBlock}</div>

    <div class="section"><h3>Job description</h3><div class="card desc">${esc(j.description || j.snippet || "—")}</div>
      ${j.page_status && j.page_status !== "fetched" ? `<div class="sub" style="margin-top:6px">Full page not fetched (${esc(j.page_status)}); details come from the search result.</div>` : ""}</div>`;

  $("#btn-close").onclick = closeDrawer;
  const act = (sel, label, fn) => { const b = $(sel); if (b) b.onclick = () => busy(b, label, fn); };
  act("#btn-skip", "Skipping", async () => { renderJob(await api(`/api/jobs/${j.id}/skip`, { method: "POST" })); loadList(); refreshStatus(); });
  act("#btn-restore", "Restoring", async () => { renderJob(await api(`/api/jobs/${j.id}/restore`, { method: "POST" })); loadList(); refreshStatus(); });
  act("#btn-find", "Searching (this takes ~1 min)", async () => { renderJob(await api(`/api/jobs/${j.id}/find-contacts`, { method: "POST" })); loadList(); });
  $("#btn-add").onclick = () => { const f = $("#add-form"); f.style.display = f.style.display === "none" ? "" : "none"; };
  act("#btn-add-save", "Adding", async () => {
    let r = await api(`/api/jobs/${j.id}/contacts`, { method: "POST", body: { email: $("#add-email").value, name: $("#add-name").value, source: $("#add-src").value } });
    const c = r.contacts.find((x) => x.email === $("#add-email").value.trim().toLowerCase());
    renderJob(await api(`/api/jobs/${j.id}/draft`, { method: "POST", body: { contact_id: c && c.id } }));
    loadList();
  });
  document.querySelectorAll("[data-use]").forEach((b) => (b.onclick = () => busy(b, "Writing", async () => {
    renderJob(await api(`/api/jobs/${j.id}/draft`, { method: "POST", body: { contact_id: +b.dataset.use, resume: $("#em-resume")?.value } }));
    loadList();
  })));
  const edits = () => ({ recipient: $("#em-to").value, subject: $("#em-subject").value, body: $("#em-body").value, resume: $("#em-resume").value });
  act("#btn-save", "Saving", async () => { renderJob(await api(`/api/emails/${e.id}`, { method: "PUT", body: edits() })); toast("Draft saved"); });
  act("#btn-regen", "Rewriting", async () => {
    const c = d.contacts.find((x) => x.email === e.recipient);
    if (!c) throw new Error("Recipient is not in the contact list — add it as a contact first.");
    renderJob(await api(`/api/jobs/${j.id}/draft`, { method: "POST", body: { contact_id: c.id, resume: $("#em-resume").value } }));
  });
  act("#btn-approve", "Saving", async () => {
    const saved = await api(`/api/emails/${e.id}`, { method: "PUT", body: edits() });   // persist what the user sees
    renderJob(saved);
    confirmSend(saved);
  });
}

// ---------------------------------------------------------------- send confirmation
function closeModal() { $("#modal").classList.remove("open"); }
function confirmSend(d) {
  const j = d.job, e = d.email;
  const contact = d.contacts.find((c) => c.email === e.recipient);
  const [vcls, vlabel] = contact ? VERIF[contact.verification] || ["", contact.verification] : ["warn", "not in contact list"];
  $("#modal-body").innerHTML = `
    <h2 style="margin-top:0">Send this application?</h2>
    <div class="grid2">
      <div class="k">To</div><div><b>${esc(e.recipient)}</b> <span class="tag ${vcls}">${esc(vlabel)}</span></div>
      <div class="k">Recruiter</div><div>${esc(e.recruiter_name || "Hiring Team")}</div>
      <div class="k">Role</div><div>${esc(j.title)} — ${esc(j.company)}</div>
      <div class="k">Subject</div><div>${esc(e.subject)}</div>
      <div class="k">Attachment</div><div>📎 ${esc(e.attachment_name)} ${e.attachment_kb != null ? `(${e.attachment_kb} KB)` : '<span class="tag bad">missing</span>'}</div>
      <div class="k">From</div><div>${esc(status?.gmail.account || "—")}</div>
    </div>
    <label>Email body (exactly what will be sent)</label><div class="mail-preview">${esc(e.body)}</div>
    <div class="d-actions" style="justify-content:flex-end"><button class="btn" id="m-cancel">Cancel</button><button class="btn good" id="m-send">Approve &amp; send</button></div>`;
  $("#modal").classList.add("open");
  $("#m-cancel").onclick = closeModal;
  $("#m-send").onclick = () => busy($("#m-send"), "Sending", async () => {
    const r = await api(`/api/emails/${e.id}/send`, { method: "POST", body: { approved: true, recipient: e.recipient, subject: e.subject } });
    closeModal(); toast(r.message); renderJob(r); loadList(); refreshStatus();
  });
}

// ---------------------------------------------------------------- boot
refreshStatus().then(loadList);
pollEvents();
setInterval(refreshStatus, 3000);
setInterval(pollEvents, 3000);
