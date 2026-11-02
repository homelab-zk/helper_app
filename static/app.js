// Helper front-end: chat list in a sidebar and a streaming chat view.
const $ = (sel) => document.querySelector(sel);
const state = { chatId: null, runningChat: null, liveEl: null, defaultModel: "opus", name: "Helper", purpose: "", files: [],
  chats: [], pending: [] };  // one answer at a time; pending = screenshots waiting to be sent
const MAX_IMAGES = 4;
const MAX_FILES = 3;
const MAX_FILE_MB = 10;
const DOC_EXT = [".pdf", ".docx", ".txt", ".md", ".markdown", ".log", ".csv", ".json", ".xml", ".yaml", ".yml", ".ini", ".conf", ".cfg"];  // also checked on the server
const fmtSize = (b) => b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(0)} KB` : `${(b / 1048576).toFixed(1)} MB`;
const fileChip = (name, size, href) => `<${href ? `a href="${esc(href)}" download` : "span"} class="file-chip" title="${esc(name)}">` +
  `<span class="file-icon" aria-hidden="true">📄</span><span class="file-name">${esc(name)}</span>` +
  `${size != null ? `<span class="file-size">${fmtSize(size)}</span>` : ""}</${href ? "a" : "span"}>`;
const MAX_SIDE = 1568;  // Claude reads images best up to about this size; larger ones are scaled down first

const EFFORT_LABELS = { "": "Default", low: "Low", medium: "Medium", high: "High", xhigh: "Extra high", max: "Max" };
const PREFS_KEY = "helper-prefs";  // last model/effort chosen, the default for new chats (each port is its own origin)
const SIDE_KEY = "helper-side-hidden";  // wide screens: the reader hid the chat list
const wide = () => matchMedia("(min-width: 761px)").matches;

const md = (text) => DOMPurify.sanitize(marked.parse(text || ""));
const esc = (s) => { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; };
const fmtDate = (ts) => new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  checkLogin(res);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
  return res.json();
}

// session expired or logged out elsewhere: back to the login page
function checkLogin(res) {
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("Not logged in");
  }
}

// ---------- model and effort ----------
async function loadOptions() {
  const opts = await api("/api/options");
  $("#model").innerHTML = opts.models.map((m) => `<option value="${m.id}">${esc(m.label)}</option>`).join("");
  $("#effort").innerHTML = ["", ...opts.efforts].map((e) => `<option value="${e}">${EFFORT_LABELS[e] || e}</option>`).join("");
  state.defaultModel = opts.default_model;
  state.name = opts.name;
  state.purpose = opts.purpose;
  state.files = opts.files;
  $("#brand-name").textContent = opts.name;
  const user = opts.user || "";
  $("#who").textContent = user;
  $("#who-full").textContent = user || "(login is off)";
  $("#avatar").textContent = (user[0] || "?").toUpperCase();
  $("#user-btn").title = user ? `Signed in as ${user}` : "Account";
}

function prefs() {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; } catch { return {}; }
}

function savePrefs() {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify({ model: $("#model").value, effort: $("#effort").value })); } catch {}
}

// a chat reopens with its last choice; new and older chats use the last one picked anywhere
function applyChoice(model, effort) {
  const p = prefs();
  const pick = (sel, ...values) => {
    const v = values.find((x) => x != null && [...sel.options].some((o) => o.value === x));
    if (v != null) sel.value = v;
  };
  pick($("#model"), model, p.model, state.defaultModel);
  pick($("#effort"), effort, p.effort, "");
}

// "claude-opus-5-5" -> "Opus 5.5"
function modelLabel(id) {
  const m = /^claude-([a-z]+)-(\d+)(?:-(\d+))?/.exec(id || "");
  return m ? `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}${m[3] ? "." + m[3] : ""}` : id;
}

// ---------- routing: #/new (also #/)  #/chat/<id> ----------
async function route() {
  const m = location.hash.match(/^#\/chat\/(\d+)$/);
  document.body.classList.remove("side-open");
  closeUserMenu();
  try {
    await showChat(m ? Number(m[1]) : null);
  } catch (e) {
    alert(e.message);
    location.hash = "#/new";
  }
  loadChats();
}

// ---------- sidebar: all chats, newest first, filtered by the search box ----------
async function loadChats() {
  try {
    state.chats = await api("/api/chats");
  } catch (e) {
    return console.error(e);
  }
  renderChats();
}

function renderChats() {
  const q = $("#chat-search").value.trim().toLowerCase();
  const chats = q ? state.chats.filter((c) => c.title.toLowerCase().includes(q)) : state.chats;
  const list = $("#chat-list");
  list.innerHTML = "";
  $("#no-chats").hidden = chats.length > 0;
  $("#no-chats").textContent = q ? "No chats match." : "No chats yet.";
  for (const c of chats) {
    const li = document.createElement("li");
    li.classList.toggle("active", c.id === state.chatId);
    li.innerHTML = `<a href="#/chat/${c.id}"><span class="title">${esc(c.title)}</span>
      <span class="meta">${c.running ? "answering…" : fmtDate(c.updated)}</span></a>
      <button class="delete" title="Delete chat" aria-label="Delete chat">✕</button>`;
    li.querySelector(".delete").onclick = async () => {
      if (!confirm(`Delete “${c.title}”?`)) return;
      await api(`/api/chats/${c.id}`, { method: "DELETE" });
      if (c.id === state.chatId) location.hash = "#/new";  // route() reloads the list
      else loadChats();
    };
    list.append(li);
  }
}

async function showChat(id) {
  state.chatId = id;
  document.body.classList.toggle("in-chat", id !== null);
  $("#web").checked = false;  // web search is opt-in, per chat visit
  const box = $("#messages");
  box.innerHTML = "";
  if (id === null) {
    $("#chat-title").textContent = "New chat";
    document.title = `New chat – ${state.name}`;
    const n = state.files.length;
    box.innerHTML = `<div class="empty-chat"><h2>${esc(state.name)}</h2>
      <p class="purpose">${esc(state.purpose)}</p>
      <p class="source-note">${n ? `Answers come from this helper's ${n} document${n > 1 ? "s" : ""} by default.`
        : "This helper has no documents yet, so answers come from general knowledge."}
        Tick <strong>Web search</strong> below to also search the internet for a question.</p></div>`;
    applyChoice();
    setRunning(state.runningChat);
    $("#question").focus();
    return;
  }
  const chat = await api(`/api/chats/${id}`);
  $("#chat-title").textContent = chat.title;
  document.title = `${chat.title} – ${state.name}`;
  applyChoice(chat.model, chat.effort);
  for (const m of chat.messages) {
    if (m.role === "user") addUser(m.content, (m.images || []).map((n) => `/api/chats/${id}/images/${n}`),
      (m.files || []).map((f) => ({ name: f.name, size: f.size, href: `/api/chats/${id}/files/${encodeURIComponent(f.stored)}` })));
    else box.append(answerEl(m.content, m.tools, 0, m.model, m.effort, m.web));
  }
  setRunning(state.runningChat);
  if (chat.running && state.runningChat === id && state.liveEl) {
    box.append(state.liveEl);  // answer still streaming in this window
  } else if (chat.running) {
    const note = document.createElement("p");
    note.className = "note-box thinking";
    note.textContent = "Still answering… this updates by itself.";
    box.append(note);
    setTimeout(() => { if (state.chatId === id) showChat(id).then(loadChats).catch(console.error); }, 3000);
  }
  scrollDown(true);
  $("#question").focus();
}

// ---------- messages ----------
// images: URLs (saved screenshots, or data: URLs of ones just sent); files: [{name, size, href}]
function addUser(text, images = [], files = []) {
  const el = document.createElement("div");
  el.className = "msg user";
  const shots = images.length ? `<div class="shots">${images.map((u) =>
    `<a href="${esc(u)}" target="_blank" rel="noopener"><img src="${esc(u)}" alt="Screenshot"></a>`).join("")}</div>` : "";
  const docs = files.length ? `<div class="files">${files.map((f) => fileChip(f.name, f.size, f.href)).join("")}</div>` : "";
  el.innerHTML = `<div class="bubble">${shots}${docs}${text ? `<div>${esc(text)}</div>` : ""}</div>`;
  $("#messages").append(el);
}

// ---------- screenshots: pick, paste or drop; scaled down in the browser before sending ----------
function loadImage(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error(`${file.name || "image"}: can't read this image`)); };
    img.src = url;
  });
}

async function prepareImage(file) {
  const img = await loadImage(file);
  const scale = Math.min(1, MAX_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
  const keep = scale === 1 && file.size < 4 * 1024 * 1024 && ["image/png", "image/jpeg", "image/webp", "image/gif"].includes(file.type);
  if (keep) return { media_type: file.type, dataUrl: await new Promise((r) => { const fr = new FileReader(); fr.onload = () => r(fr.result); fr.readAsDataURL(file); }) };
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(img.naturalWidth * scale);
  canvas.height = Math.round(img.naturalHeight * scale);
  canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
  // PNG keeps text in screenshots sharp; fall back to JPEG when that is still too big
  let dataUrl = canvas.toDataURL("image/png");
  let type = "image/png";
  if (dataUrl.length > 6.5e6) { dataUrl = canvas.toDataURL("image/jpeg", 0.9); type = "image/jpeg"; }
  return { media_type: type, dataUrl };
}

const readDataUrl = (file) => new Promise((resolve, reject) => {
  const fr = new FileReader();
  fr.onload = () => resolve(fr.result);
  fr.onerror = () => reject(new Error(`${file.name}: can't read this file`));
  fr.readAsDataURL(file);
});

// screenshots and files from the 📎 button, Ctrl+V or drag and drop
async function addImages(files) {
  for (const f of [...files]) {
    const ext = (f.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
    try {
      if (["image/png", "image/jpeg", "image/webp", "image/gif"].includes(f.type)) {
        if (state.pending.filter((p) => p.kind === "image").length >= MAX_IMAGES) throw new Error(`At most ${MAX_IMAGES} screenshots per question.`);
        state.pending.push({ kind: "image", ...(await prepareImage(f)) });
      } else if (DOC_EXT.includes(ext)) {
        if (state.pending.filter((p) => p.kind === "file").length >= MAX_FILES) throw new Error(`At most ${MAX_FILES} files per question.`);
        if (f.size > MAX_FILE_MB * 1048576) throw new Error(`${f.name}: larger than ${MAX_FILE_MB} MB.`);
        state.pending.push({ kind: "file", name: f.name, size: f.size, dataUrl: await readDataUrl(f) });
      } else {
        throw new Error(`${f.name}: this file type can't be attached. Screenshots (PNG, JPEG, WebP, GIF) and ${DOC_EXT.join(" ")} can.`);
      }
    } catch (e) { alert(e.message); }
  }
  renderPending();
  $("#question").focus();
}

function renderPending() {
  const box = $("#attachments");
  box.innerHTML = "";
  box.hidden = !state.pending.length;
  state.pending.forEach((p, i) => {
    const d = document.createElement("div");
    d.className = "thumb";
    d.innerHTML = (p.kind === "image" ? `<img src="${p.dataUrl}" alt="Screenshot ${i + 1}">` : fileChip(p.name, p.size)) +
      `<button type="button" aria-label="Remove attachment ${i + 1}" title="Remove">✕</button>`;
    d.querySelector("button").onclick = () => { state.pending.splice(i, 1); renderPending(); };
    box.append(d);
  });
}

function setupAttachments() {
  // touch screens have no Ctrl+V: keep the hint short there (📎 opens the photo picker)
  if (matchMedia("(hover: none)").matches) $("#question").placeholder = "Ask a question…";
  $("#attach").addEventListener("click", () => $("#attach-input").click());
  $("#attach-input").addEventListener("change", (e) => { addImages(e.target.files); e.target.value = ""; });
  // Ctrl+V with a screenshot on the clipboard (text pastes as usual)
  $("#question").addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.items || [])].filter((it) => it.kind === "file").map((it) => it.getAsFile()).filter(Boolean);
    if (files.length) { e.preventDefault(); addImages(files); }
  });
  const form = $("#ask-form");
  form.addEventListener("dragover", (e) => { if ([...e.dataTransfer.types].includes("Files")) { e.preventDefault(); form.classList.add("drop-over"); } });
  form.addEventListener("dragleave", (e) => { if (!form.contains(e.relatedTarget)) form.classList.remove("drop-over"); });
  form.addEventListener("drop", (e) => { e.preventDefault(); form.classList.remove("drop-over"); addImages(e.dataTransfer.files); });
}

function answerEl(content, tools = [], seconds = 0, model = null, effort = null, web = false) {
  const el = document.createElement("div");
  el.className = "msg assistant";
  if (tools.length) {
    el.innerHTML = `<details class="tools"><summary>Looked up ${tools.length} thing${tools.length > 1 ? "s" : ""}</summary>
      <ul>${tools.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></details>`;
  }
  const body = document.createElement("div");
  body.className = "body";
  body.innerHTML = md(content);
  el.append(body);
  const meta = [seconds && `Answered in ${seconds} s`, model && modelLabel(model), effort && `${EFFORT_LABELS[effort] || effort} effort`, web && "Web search"];
  if (meta.some(Boolean)) el.insertAdjacentHTML("beforeend", `<div class="meta-line">${esc(meta.filter(Boolean).join(" · "))}</div>`);
  return el;
}

function liveAnswer() {
  const el = document.createElement("div");
  el.className = "msg assistant";
  el.innerHTML = `<ul class="activity"></ul><div class="body"><span class="thinking">Thinking…</span></div>`;
  $("#messages").append(el);
  state.liveEl = el;
  const body = el.querySelector(".body");
  const activity = el.querySelector(".activity");
  let text = "";
  let frame = 0;
  const render = () => { frame = 0; body.innerHTML = md(text); scrollDown(); };
  const note = (cls, t) => {
    const li = document.createElement("li");
    li.className = cls;
    li.textContent = t;
    activity.append(li);
    scrollDown();
  };
  return {
    finished: false,
    text({ delta }) { text += delta; if (!frame) frame = requestAnimationFrame(render); },
    turn() {
      if (text.trim()) note("note", text.trim());
      text = "";
      body.innerHTML = `<span class="thinking">Thinking…</span>`;
    },
    tool({ text: t }) { note("", t); },
    status({ text: t }) { note("note", t); },
    done({ answer, tools, seconds, model, effort, web }) {
      this.finished = true;
      cancelAnimationFrame(frame);
      el.replaceWith(answerEl(answer, tools, seconds, model, effort, web));
      scrollDown();
    },
    error(message) {
      this.finished = true;
      cancelAnimationFrame(frame);
      body.innerHTML = `<p class="error">${esc(message)}</p>`;
    },
  };
}

function scrollDown(force = false) {
  const box = $("#messages");
  if (force || box.scrollHeight - box.scrollTop - box.clientHeight < 120) box.scrollTop = box.scrollHeight;
  if (force) requestAnimationFrame(() => { box.scrollTop = box.scrollHeight; });  // again after layout settles
}

// ---------- asking ----------
async function readEvents(res, handle) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      const event = /^event: (.*)$/m.exec(chunk)?.[1];
      const data = /^data: (.*)$/m.exec(chunk)?.[1];
      if (event) handle(event, data ? JSON.parse(data) : {});
    }
  }
}

async function send(question) {
  question = question.trim();
  const images = state.pending.filter((p) => p.kind === "image");
  const files = state.pending.filter((p) => p.kind === "file");
  if (state.runningChat !== null || (!question && !state.pending.length)) return;
  let live;
  try {
    let id = state.chatId;
    if (id === null) {
      const chat = await api("/api/chats", { method: "POST", body: JSON.stringify({ title: question || (files[0]?.name ?? "Screenshot") }) });
      id = state.chatId = chat.id;
      history.replaceState(null, "", `#/chat/${id}`);
      $("#chat-title").textContent = chat.title;
      $("#messages").innerHTML = "";
      loadChats();
    }
    setRunning(id);
    addUser(question, images.map((p) => p.dataUrl), files.map((p) => ({ name: p.name, size: p.size })));
    state.pending = [];
    renderPending();
    $("#question").value = "";
    autosize();
    live = liveAnswer();
    scrollDown(true);
    const res = await fetch(`/api/chats/${id}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, model: $("#model").value, effort: $("#effort").value, web: $("#web").checked,
        images: images.map((p) => ({ media_type: p.media_type, data: p.dataUrl.split(",")[1] })),
        files: files.map((p) => ({ name: p.name, data: p.dataUrl.split(",")[1] || "" })) }),
    });
    checkLogin(res);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
    loadChats();  // shows "answering…"
    await readEvents(res, (event, data) => live[event]?.(data));
    if (!live.finished) live.error("The connection closed before the answer finished. Reopen the chat to see if it was saved.");
  } catch (e) {
    if (live) live.error(e.message); else alert(e.message);
  } finally {
    state.liveEl = null;
    setRunning(null);
    loadChats();
  }
}

function setRunning(chatId) {
  state.runningChat = chatId;
  $("#send").hidden = chatId !== null;
  $("#stop").hidden = chatId === null;
}

function autosize() {
  const q = $("#question");
  q.style.height = "auto";
  q.style.height = `${q.scrollHeight + 2}px`;
}

// ---------- installing as an app (Android Chrome, desktop Chrome/Edge) ----------
let installPrompt = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();  // show our own button instead of the browser's mini bar
  installPrompt = e;
  $("#install").hidden = false;
});

function setupInstall() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(console.error);
  $("#install").addEventListener("click", async () => {
    if (!installPrompt) return;
    installPrompt.prompt();
    await installPrompt.userChoice;
    installPrompt = null;
    $("#install").hidden = true;
  });
  window.addEventListener("appinstalled", () => { $("#install").hidden = true; });
}

// ---------- top bar: chat list toggle and user menu ----------
function closeUserMenu() {
  $("#user-pop").hidden = true;
  $("#user-btn").setAttribute("aria-expanded", "false");
}

function syncMenuButton() {
  const open = wide() ? !document.body.classList.contains("side-hidden") : document.body.classList.contains("side-open");
  $("#menu").setAttribute("aria-expanded", String(open));
}

function setupNav() {
  // wide screens: ☰ hides/shows the chat list (remembered); phones: it slides in over the chat
  try { document.body.classList.toggle("side-hidden", localStorage.getItem(SIDE_KEY) === "1"); } catch {}
  $("#menu").addEventListener("click", () => {
    if (wide()) {
      const hidden = document.body.classList.toggle("side-hidden");
      try { localStorage.setItem(SIDE_KEY, hidden ? "1" : "0"); } catch {}
    } else {
      document.body.classList.toggle("side-open");
    }
    syncMenuButton();
  });
  $("#backdrop").addEventListener("click", () => { document.body.classList.remove("side-open"); syncMenuButton(); });
  matchMedia("(min-width: 761px)").addEventListener("change", () => { document.body.classList.remove("side-open"); syncMenuButton(); });
  syncMenuButton();

  $("#user-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    const open = $("#user-pop").hidden;
    $("#user-pop").hidden = !open;
    $("#user-btn").setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".user-menu")) closeUserMenu(); });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    closeUserMenu();
    document.body.classList.remove("side-open");
    syncMenuButton();
  });

  $("#chat-search").addEventListener("input", renderChats);
  // phone: picking a chat closes the drawer (route() does that); wide: nothing to do
}

// ---------- wiring ----------
window.addEventListener("DOMContentLoaded", async () => {
  marked.setOptions({ gfm: true });
  $("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); send($("#question").value); });
  $("#question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send($("#question").value); }
  });
  $("#question").addEventListener("input", autosize);
  $("#stop").addEventListener("click", () => {
    if (state.runningChat !== null) api(`/api/chats/${state.runningChat}/stop`, { method: "POST", body: "{}" });
  });
  // "+ New" while already on #/new doesn't fire hashchange, so reset by hand
  document.querySelectorAll("a.new-chat").forEach((a) => a.addEventListener("click", (e) => {
    if (location.hash === "#/new") { e.preventDefault(); route(); }
  }));
  setupNav();
  setupAttachments();
  // stay at the newest message when the layout changes size (app window settling, rotation,
  // keyboard), unless the reader has scrolled up
  const box = $("#messages");
  let atBottom = true;
  box.addEventListener("scroll", () => { atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120; });
  new ResizeObserver(() => { if (atBottom) box.scrollTop = box.scrollHeight; }).observe(box);
  $("#model").addEventListener("change", savePrefs);
  $("#effort").addEventListener("change", savePrefs);
  window.addEventListener("hashchange", route);
  await loadOptions().catch(console.error);
  route();
  setupInstall();
});
