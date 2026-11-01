// Helper admin: list helpers, create/edit one (name, purpose, port), upload its Markdown files, start/stop it.
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; };
const fmtDate = (ts) => new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
const fmtSize = (b) => b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(1)} KB` : `${(b / 1048576).toFixed(1)} MB`;
const helperUrl = (port) => `${location.protocol}//${location.hostname}:${port}/`;
const STATUS = { running: "Running", stopped: "Stopped", starting: "Starting…", failed: "Failed" };
let poll = null;

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (res.status === 401) { location.href = "/login"; throw new Error("Not logged in"); }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}

const badge = (status) => `<span class="badge ${status}">${STATUS[status] || status}</span>`;

// ---------- routing: #/  #/new  #/h/<id>[/settings|files|users|log] ----------
const TABS = ["settings", "files", "users", "log"];
let shownHelper = null;  // id of the helper page on screen, so switching tabs doesn't reload it

function route() {
  const m = location.hash.match(/^#\/h\/([a-z0-9-]+)(?:\/([a-z]+))?$/);
  const tab = m && TABS.includes(m[2]) ? m[2] : "settings";
  if (m && m[1] === shownHelper && $("#view .tabs")) return selectTab(tab);  // same helper: keep the page (and unsaved edits)
  clearInterval(poll);
  shownHelper = null;
  const view = $("#view");
  view.innerHTML = "";
  (location.hash === "#/new" ? showEdit(null) : m ? showEdit(m[1], tab) : showList()).catch((e) => {
    view.innerHTML = `<p class="error">${esc(e.message)}</p><p><a href="#/">← All helpers</a></p>`;
  });
}

// ---------- list ----------
async function showList() {
  document.title = "Helper admin";
  const { helpers } = await api("/api/helpers");
  const view = $("#view");
  view.append($("#list-tpl").content.cloneNode(true));
  $(".empty", view).hidden = helpers.length > 0;
  for (const h of helpers) {
    const card = document.createElement("a");
    card.className = "card";
    card.href = `#/h/${h.id}`;
    card.innerHTML = `<div class="card-head"><strong>${esc(h.name)}</strong>${badge(h.status)}</div>
      <p class="purpose">${esc(h.purpose)}</p>
      <div class="card-meta">Port ${h.port} · ${h.files} file${h.files === 1 ? "" : "s"} (${fmtSize(h.bytes)}) · ${h.users} user${h.users === 1 ? "" : "s"}</div>`;
    if (h.status === "running") {
      const open = document.createElement("a");
      open.className = "card-open";
      open.href = helperUrl(h.port);
      open.target = "_blank";
      open.rel = "noopener";
      open.textContent = "Open ↗";
      open.addEventListener("click", (e) => e.stopPropagation());
      card.querySelector(".card-head").append(open);
    }
    $(".cards", view).append(card);
  }
  poll = setInterval(async () => {  // keep the status badges current
    try {
      const { helpers: now } = await api("/api/helpers");
      if (now.map((h) => h.id + h.status).join() !== helpers.map((h) => h.id + h.status).join()) route();
    } catch {}
  }, 4000);
}

function selectTab(tab) {
  const view = $("#view");
  for (const a of view.querySelectorAll(".tab")) {
    const on = a.dataset.tab === tab;
    a.classList.toggle("active", on);
    a.setAttribute("aria-selected", String(on));
  }
  for (const el of view.querySelectorAll("[data-tab]:not(.tab)")) el.hidden = el.dataset.tab !== tab;
  if (tab === "log") { const pre = $(".log", view); pre.scrollTop = pre.scrollHeight; }
}

// ---------- create / edit ----------
async function showEdit(id, tab = "settings") {
  const view = $("#view");
  lastFiles = "";
  view.append($("#edit-tpl").content.cloneNode(true));
  const form = $(".helper-form", view);
  const portInput = $("#f-port");
  const preview = () => { $(".url-preview", view).textContent = portInput.value ? helperUrl(portInput.value) : "…"; };
  portInput.addEventListener("input", preview);

  if (id === null) {
    document.title = "New helper – Helper admin";
    $(".edit-title", view).textContent = "New helper";
    $(".save", view).textContent = "Create helper";
    const { admin_port, helpers } = await api("/api/helpers");
    const used = new Set([admin_port, ...helpers.map((h) => h.port)]);
    let port = 8011;
    while (used.has(port)) port++;
    portInput.value = port;
    preview();
    $("#f-name").focus();
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      await submit(form, "POST", "/api/helpers", (h) => { location.hash = `#/h/${h.id}/files`; });
    });
    return;
  }

  let h = await api(`/api/helpers/${id}`);
  document.title = `${h.name} – Helper admin`;
  $(".edit-title", view).textContent = h.name;
  $("#f-name").value = h.name;
  $("#f-purpose").value = h.purpose;
  portInput.value = h.port;
  preview();
  $(".controls", view).hidden = false;
  const tabs = $(".tabs", view);
  tabs.hidden = false;
  for (const a of tabs.querySelectorAll(".tab")) a.href = `#/h/${id}/${a.dataset.tab}`;
  shownHelper = id;
  selectTab(tab);
  renderState(h);
  renderFiles(h.file_list);
  $(".admin-name", view).textContent = h.admin_user || "admin";
  renderUsers(id, h.user_list);

  $(".add-user", view).addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const err = $(".user-error", view);
    err.hidden = true;
    try {
      const res = await api(`/api/helpers/${id}/users`, { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(form))) });
      form.reset();
      renderUsers(id, res.users);
    } catch (e2) {
      err.textContent = e2.message;
      err.hidden = false;
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    await submit(form, "PUT", `/api/helpers/${id}`, (updated) => {
      $(".edit-title", view).textContent = updated.name;
      document.title = `${updated.name} – Helper admin`;
      renderState(updated);
    });
  });

  for (const action of ["start", "stop", "restart"]) {
    $(`.controls .${action}`, view).addEventListener("click", async (e) => {
      e.target.disabled = true;
      try { renderState(await api(`/api/helpers/${id}/${action}`, { method: "POST", body: "{}" })); }
      catch (err) { alert(err.message); }
      finally { e.target.disabled = false; refresh(); }
    });
  }

  $(".delete", view).addEventListener("click", async () => {
    if (!confirm(`Delete “${h.name}”? It stops, and its files and chats move to the trash folder on the server.`)) return;
    try {
      await api(`/api/helpers/${id}`, { method: "DELETE" });
      location.hash = "#/";
    } catch (err) { alert(err.message); }
  });

  // uploading: button or drag and drop
  const input = $(".upload input", view);
  input.addEventListener("change", () => { upload(id, [...input.files]); input.value = ""; });
  const drop = $(".drop", view);
  const panel = $(".files-panel", view);
  panel.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  panel.addEventListener("dragleave", (e) => { if (!panel.contains(e.relatedTarget)) drop.classList.remove("over"); });
  panel.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    upload(id, [...e.dataTransfer.files]);
  });

  async function refresh() {
    try {
      h = await api(`/api/helpers/${id}`);
      renderState(h);
    } catch {}
  }
  poll = setInterval(refresh, 3000);
}

async function submit(form, method, path, done) {
  const err = $(".form-error", form);
  const saved = $(".saved", form);
  err.hidden = true;
  saved.hidden = true;
  const data = Object.fromEntries(new FormData(form));
  $(".save", form).disabled = true;
  try {
    done(await api(path, { method, body: JSON.stringify(data) }));
    saved.hidden = false;
    setTimeout(() => { saved.hidden = true; }, 2500);
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  } finally {
    $(".save", form).disabled = false;
  }
}

function renderState(h) {
  const view = $("#view");
  $(".status", view).innerHTML = badge(h.status);
  const open = $(".open", view);
  open.href = helperUrl(h.port);
  open.hidden = h.status !== "running";
  $(".start", view).hidden = h.status === "running" || h.status === "starting";
  $(".restart", view).hidden = h.status !== "running";
  $(".stop", view).hidden = !h.enabled;
  if (h.log) {
    const pre = $(".log", view);
    const atEnd = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 20;
    pre.textContent = h.log.join("\n") || "(nothing yet)";
    if (atEnd) pre.scrollTop = pre.scrollHeight;
  }
  if (h.file_list) renderFiles(h.file_list);
}

let lastFiles = "";
function renderFiles(files) {
  const key = JSON.stringify(files);
  if (key === lastFiles) return;  // polling: don't redraw (and lose focus) when nothing changed
  lastFiles = key;
  const view = $("#view");
  $(".files-count", view).textContent = files.length;
  const id = location.hash.split("/")[2];
  const body = $(".files tbody", view);
  body.innerHTML = "";
  $(".files", view).hidden = !files.length;
  $(".no-files", view).hidden = files.length > 0;
  for (const f of files) {
    const tr = document.createElement("tr");
    const url = `/api/helpers/${id}/files/${encodeURIComponent(f.name)}`;
    const from = (f.sources || []).map((s) =>
      `<a href="/api/helpers/${id}/originals/${encodeURIComponent(s)}">${esc(s)}</a>`).join(", ");
    tr.innerHTML = `<td><a href="${url}" target="_blank" rel="noopener">${esc(f.name)}</a>${from ? `<span class="from">converted from ${from}</span>` : ""}</td>
      <td>${fmtSize(f.size)}</td><td>${fmtDate(f.modified)}</td>
      <td><button type="button" class="delete-file" title="Delete file" aria-label="Delete ${esc(f.name)}">✕</button></td>`;
    tr.querySelector(".delete-file").onclick = async () => {
      if (!confirm(`Delete ${f.name}${f.sources?.length ? ` and ${f.sources.join(", ")}` : ""}?`)) return;
      try {
        await api(url, { method: "DELETE" });
        renderFiles((await api(`/api/helpers/${id}`)).file_list);
      } catch (e) { alert(e.message); }
    };
    body.append(tr);
  }
}

function renderUsers(id, users) {
  const view = $("#view");
  $(".users-count", view).textContent = users.length;
  const body = $(".users tbody", view);
  body.innerHTML = "";
  $(".users", view).hidden = !users.length;
  $(".no-users", view).hidden = users.length > 0;
  for (const u of users) {
    const tr = document.createElement("tr");
    const url = `/api/helpers/${id}/users/${encodeURIComponent(u.name)}`;
    tr.innerHTML = `<td class="uname">${esc(u.name)}</td><td>${fmtDate(u.created)}</td><td>${fmtDate(u.changed)}</td>
      <td class="user-actions"><button type="button" class="button small set-pw">New password</button>
        <button type="button" class="delete-file remove" title="Remove user" aria-label="Remove ${esc(u.name)}">✕</button></td>`;
    tr.querySelector(".remove").onclick = async () => {
      if (!confirm(`Remove ${u.name}? They are logged out at once; their chats are kept.`)) return;
      try { renderUsers(id, (await api(url, { method: "DELETE" })).users); } catch (e) { alert(e.message); }
    };
    tr.querySelector(".set-pw").onclick = () => {
      // inline form in a row under the user, so the password isn't typed into a browser prompt
      if (tr.nextElementSibling?.classList.contains("pw-row")) return tr.nextElementSibling.querySelector("input").focus();
      const row = document.createElement("tr");
      row.className = "pw-row";
      row.innerHTML = `<td colspan="4"><form class="pw-form">
        <input type="password" placeholder="New password for ${esc(u.name)} (8+ characters)" autocomplete="new-password" required minlength="8">
        <button type="submit" class="button primary small">Save</button>
        <button type="button" class="button small cancel">Cancel</button>
        <span class="error" hidden></span></form></td>`;
      tr.after(row);
      const input = row.querySelector("input");
      input.focus();
      row.querySelector(".cancel").onclick = () => row.remove();
      row.querySelector("form").onsubmit = async (e) => {
        e.preventDefault();
        try {
          renderUsers(id, (await api(url, { method: "PUT", body: JSON.stringify({ password: input.value }) })).users);
        } catch (err) {
          const span = row.querySelector(".error");
          span.textContent = err.message;
          span.hidden = false;
        }
      };
    };
    body.append(tr);
  }
}

async function upload(id, files) {
  const list = $(".upload-status");
  for (const file of files) {
    const li = document.createElement("li");
    const converting = /\.(pdf|docx)$/i.test(file.name);
    li.textContent = `${file.name}: ${converting ? "uploading and converting to Markdown…" : "uploading…"}`;
    list.append(li);
    try {
      const res = await fetch(`/api/helpers/${id}/files/${encodeURIComponent(file.name)}`, { method: "PUT", body: file });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || res.statusText);
      const what = body.converted_from
        ? `converted to ${body.name}${body.pages ? `, ${body.pages} page${body.pages > 1 ? "s" : ""}` : ""}${body.replaced ? ", replaced the old version" : ""}`
        : body.replaced ? "replaced" : "uploaded";
      li.textContent = `${file.name}: ${what} (${fmtSize(body.size)})`;
      li.className = "ok";
      if (body.warning) {
        li.textContent += `. ${body.warning}`;
        li.className = "warn";  // stays visible
      }
    } catch (e) {
      li.textContent = `${file.name}: ${e.message}`;
      li.className = "error";
    }
  }
  try { renderFiles((await api(`/api/helpers/${id}`)).file_list); } catch {}
  setTimeout(() => { list.querySelectorAll("li.ok").forEach((li) => li.remove()); }, 6000);
}

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", route);
