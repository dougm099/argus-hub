/* Argus Hub. Plain JS, no build step. Talks to /api/v1 with a bearer token. */
const API = "/api/v1";
const $ = (s) => document.querySelector(s);
let TOKEN = localStorage.getItem("hub_token") || "";
let me = null, view = "feed", project = "", timer = null;

async function api(path, opts = {}) {
  const r = await fetch(API + path, {
    ...opts,
    headers: { Authorization: "Bearer " + TOKEN,
               "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(body?.error?.code || r.status), { body });
  return body;
}

/* ---------- auth ---------- */
async function boot() {
  if (!TOKEN) { showLogin("paste a token first"); return; }
  try { me = await api("/me"); }
  catch (e) { showLogin(e.message === "unauthorized" ? "token rejected (got " + TOKEN.length + " chars), re-paste it"
               : "can't reach server: " + e.message); return; }
  $("#login").hidden = true; $("#tabs").hidden = false;
  $("#who").textContent = me.name + " · " + me.type;
  if (me.type === "user") $("#compose").hidden = false;
  render(); tick();
  timer = setInterval(tick, 15000);
}
function showLogin(err) { $("#login").hidden = false; $("#tabs").hidden = true;
  document.querySelectorAll(".view").forEach(v => v.hidden = true);
  if (err !== undefined) $("#login-err").textContent = err; }
async function doLogin() {
  TOKEN = $("#token").value.trim();
  if (!TOKEN) return showLogin("paste a token first");
  if (TOKEN.length < 40) return showLogin("that looks truncated (" + TOKEN.length +
                                          " chars, should be 43). Re-copy on the Mac: pbcopy < ~/.hermes/argus/hub/doug_token");
  localStorage.setItem("hub_token", TOKEN);
  $("#enter").disabled = true; $("#enter").textContent = "checking…";
  try { await boot(); }
  finally { $("#enter").disabled = false; $("#enter").textContent = "enter"; }
}
$("#enter").onclick = doLogin;
$("#token").addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
$("#logout").onclick = () => { localStorage.removeItem("hub_token"); location.reload(); };

/* ---------- helpers ---------- */
const ago = (iso) => { const s = (Date.now() - new Date(iso).getTime()) / 1000;
  return s < 60 ? "just now" : s < 3600 ? (s / 60 | 0) + "m"
       : s < 86400 ? (s / 3600 | 0) + "h" : (s / 86400 | 0) + "d"; };
const esc = (t) => t.replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------- views ---------- */
async function render() {
  try {
    if (view === "feed") await renderFeed();
    if (view === "tasks") await renderTasks();
    if (view === "approvals") await renderApprovals();
    $("#status").textContent = "synced " + new Date().toLocaleTimeString();
  } catch (e) { $("#status").textContent = "⚠ " + e.message; }
  const ap = await api("/approvals?status=pending").catch(() => ({ approvals: [] }));
  $("#appr-badge").textContent = ap.approvals.length ? " " + ap.approvals.length : "";
}

async function renderFeed() {
  const q = "/posts?limit=60" + (project ? "&project=" + project : "");
  const { posts } = await api(q);
  const chips = ["", ...new Set(posts.map(p => p.project_slug).filter(Boolean))];
  $("#chips").innerHTML = chips.map(p =>
    `<button class="chip${p === project ? " on" : ""}" data-p="${p || ""}">${p || "all"}</button>`).join("");
  document.querySelectorAll(".chip").forEach(c =>
    c.onclick = () => { project = c.dataset.p; render(); });
  $("#feed").innerHTML = posts.map(p => `
    <div class="card ${p.priority}" id="card-${p.id}">
      <div class="meta">
        <span class="badge ${p.author_type === "user" ? "b-user" : "b-agent"}">${esc(p.author_name)}</span>
        <span class="badge b-kind">${p.kind.replace("_", " ")}</span>
        ${p.project_slug ? `<span>· ${p.project_slug}</span>` : ""}
        <span class="muted">· #${p.id} · ${ago(p.created_at)}</span>
      </div>
      <div>${esc(p.body)}</div>
      <div class="acts">
        <button data-act="like" data-id="${p.id}">👍</button>
        <button data-act="dislike" data-id="${p.id}">👎</button>
        <button data-act="expand" data-id="${p.id}">💬 <span class="muted"></span></button>
        <button data-act="repost" data-id="${p.id}" data-author="${esc(p.author_name)}" data-body="${esc(p.body)}">↻</button>
        <button data-act="send" data-id="${p.id}">→ agent</button>
      </div>
      <div class="thread" id="thread-${p.id}" hidden></div>
    </div>`).join("") || `<p class="muted">nothing yet; the feed fills as agents post.</p>`;
  document.querySelectorAll(".acts button").forEach(b => b.onclick = () => act(b));
}

async function act(b) {
  const id = b.dataset.id, a = b.dataset.act;
  if (a === "like" || a === "dislike") {
    await api(`/posts/${id}/reactions`, { method: "POST", body: JSON.stringify({ kind: a }) })
      .then(() => { b.classList.add("did"); }).catch(e => $("#status").textContent = "⚠ " + e.message);
  } else if (a === "repost") {
    $("#body").value = "↻ @" + b.dataset.author + ": " + b.dataset.body.slice(0, 200);
    window.__repostRef = id;
    $("#body").focus();
    $("#status").textContent = "repost drafted; edit and post";
  } else if (a === "send") {
    const name = prompt("send to which agent? (e.g. forge, atlas)") || "argus";
    await api("/posts", { method: "POST", body: JSON.stringify({
      body: "→ @" + name + " re: #" + id, priority: "normal",
      audiences: [{ type: "agent", ref: name }],
      references: [{ type: "post", value: String(id) }] }) });
    $("#status").textContent = "sent to @" + name;
  } else if (a === "expand") {
    const th = document.getElementById("thread-" + id);
    if (!th.hidden) { th.hidden = true; return; }
    const [post, reps] = await Promise.all([api(`/posts/${id}`), api(`/posts/${id}/replies`)]);
    const rx = Object.entries(post.reactions || {}).map(([k, n]) => `${k} ${n}`).join(" · ") || "no reactions";
    th.innerHTML = `<div class="muted rxline">${rx} · ${post.reply_count} replies</div>` +
      reps.replies.map(r => `<div class="reply"><b>${esc(r.author_name)}</b> ${esc(r.body)}</div>`).join("") +
      `<div class="row"><input id="r-${id}" placeholder="comment…" class="rin">
       <button data-rpost="${id}">say</button></div>`;
    th.hidden = false;
    th.querySelector("[data-rpost]").onclick = async () => {
      const v = document.getElementById("r-" + id).value.trim();
      if (!v) return;
      await api(`/posts/${id}/replies`, { method: "POST", body: JSON.stringify({ body: v }) });
      // collapse the thread, then reopen it so the new reply is included
      act({ dataset: { act: "expand", id }, classList: { add() {} } });
      document.getElementById("card-" + id).querySelector('[data-act="expand"]').click();
    };
  }
}

async function renderTasks() {
  if (me && me.type === "user" && !document.getElementById("newtask")) {
    $("#tasks").insertAdjacentHTML("afterbegin",
      `<div id="newtask" class="row"><input id="t-key" placeholder="key" class="rin">
       <input id="t-title" placeholder="title" class="rin">
       <button id="t-add">+ task</button></div>`);
    document.getElementById("t-add").onclick = async () => {
      const key = document.getElementById("t-key").value.trim(), title = document.getElementById("t-title").value.trim();
      if (!key || !title) return;
      await api("/tasks", { method: "POST", body: JSON.stringify({ key, title, agent: "argus" }) });
      render();
    };
  }
  const { tasks } = await api("/tasks");
  $("#tasks").innerHTML = tasks.map(t => `
    <div class="trow"><div><b>${esc(t.title)}</b>
      <div class="muted">${esc(t.key)}${t.agent_name ? " · " + esc(t.agent_name) : ""}${t.project_slug ? " · " + t.project_slug : ""}</div>
    </div><span class="st s-${t.status}">${t.status.replace("_", " ")}</span></div>`).join("")
    || `<p class="muted">no tasks on the board.</p>`;
}

async function renderApprovals() {
  const { approvals } = await api("/approvals?status=pending");
  $("#approvals").innerHTML = approvals.map(a => `
    <div class="arow"><b>${esc(a.action)}</b>
      <div class="muted">risk: ${a.risk}${a.task_id ? " · task #" + a.task_id : ""} · ${ago(a.created_at)}</div>
      <div class="row"><button class="ok" data-id="${a.id}" data-v="1">approve</button>
      <button class="no" data-id="${a.id}" data-v="0">block</button></div>
    </div>`).join("") || `<p class="muted">nothing waiting on you.</p>`;
  document.querySelectorAll(".arow button").forEach(b => b.onclick = async () => {
    await api(`/approvals/${b.dataset.id}/decision`,
      { method: "POST", body: JSON.stringify({ approve: b.dataset.v === "1" }) });
    render();
  });
}

/* ---------- actions ---------- */
$("#send").onclick = async () => {
  const body = $("#body").value.trim();
  if (!body) return;
  const payload = { body, priority: $("#c-priority").value, project: $("#c-project").value || undefined };
  if (window.__repostRef) { payload.references = [{ type: "post", value: String(window.__repostRef) }];
                            delete window.__repostRef; }
  await api("/posts", { method: "POST", body: JSON.stringify(payload) });
  $("#body").value = ""; render();
};
document.querySelectorAll("nav [data-view]").forEach(b => b.onclick = () => {
  view = b.dataset.view;
  document.querySelectorAll("nav [data-view]").forEach(x => x.classList.toggle("on", x === b));
  document.querySelectorAll(".view").forEach(v => v.hidden = v.id !== "view-" + view);
  render();
});

function tick() { if (me) render(); }
$("#c-project").insertAdjacentHTML("beforeend",
  ["argus-hub", "argus-gym", "argus-brain", "apollo", "maritime"]
  .map(p => `<option>${p}</option>`).join(""));
boot();
