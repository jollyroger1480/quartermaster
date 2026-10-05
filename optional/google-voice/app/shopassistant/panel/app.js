"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, body) {
  const opt = { headers: { "X-Token": window.TOKEN } };
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  const d = await r.json().catch(() => ({ error: "The control panel did not answer." }));
  if (!r.ok) throw new Error(d.error || "Something went wrong.");
  return d;
}
let toastTimer;
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 3800);
}
function flash(id, msg) { const el = $(id); el.textContent = msg; setTimeout(() => { el.textContent = ""; }, 3000); }

// ------------------------------------------------------------ navigation
let page = "home";
function show(p) {
  page = p;
  document.querySelectorAll(".nav").forEach((b) => b.classList.toggle("on", b.dataset.page === p));
  document.querySelectorAll(".page").forEach((s) => s.classList.toggle("on", s.id === "page-" + p));
  if (p === "calls") loadCalls();
  if (p === "business" || p === "settings") loadSettings();
  if (p === "knowledge") loadKnowledge();
  if (p === "settings") loadLog();
  window.scrollTo(0, 0);
}
document.querySelectorAll(".nav").forEach((b) => b.addEventListener("click", () => show(b.dataset.page)));

// ------------------------------------------------------------------ state
let state = null;
async function refresh() {
  try { state = await api("/api/state"); } catch (e) { return; }
  const s = state;
  $("brandBy").textContent = "by " + s.brand.maker;
  if (s.brand.logo) { $("logo").src = "logo.png"; $("logo").hidden = false; }
  $("railFoot").innerHTML = "Version " + esc(s.brand.version) + '<br><a href="' + esc(s.brand.maker_url) + '" target="_blank" rel="noopener">' + esc(s.brand.maker) + "</a>";
  $("about").textContent = s.brand.name + " " + s.brand.version + ", by " + s.brand.maker + ". Open-source parts are credited in CREDITS.md.";
  $("homeTitle").textContent = s.shop ? s.shop : "Phone assistant";
  $("callCount").textContent = s.calls || "";

  const on = s.running && !s.wanted_off;
  const pw = $("power");
  pw.setAttribute("aria-pressed", on ? "true" : "false");
  $("powerText").textContent = on ? "On" : "Off";
  let line = "The assistant is off. Calls ring through as usual.", sub = "";
  if (on && s.listening) { line = "Answering calls."; sub = "Connected to Google Voice. Last check-in " + Math.round(s.heartbeat_age) + " seconds ago."; }
  else if (on) { line = "Starting up…"; sub = "Loading the voice and opening Google Voice. This takes a minute or two the first time."; }
  if (s.fault && on) { line = "On, but something needs attention."; sub = s.fault; }
  $("powerState").textContent = line;
  $("powerSub").textContent = sub;
  $("autostart").checked = s.autostart;
  $("autoWrap").hidden = !s.can_autostart;

  const banner = $("banner");
  if (!s.engine_ok) {
    const missing = Object.keys(s.engine).filter((k) => !s.engine[k]).join(", ");
    banner.hidden = false; banner.className = "banner bad";
    banner.textContent = "Some parts are not installed (" + missing + "). Run Install again from the app folder.";
  } else banner.hidden = true;

  const todo = s.steps.filter((x) => !x.done).length;
  $("setupBlock").hidden = todo === 0;
  $("steps").innerHTML = s.steps.map((x) =>
    '<li class="' + (x.done ? "done" : "") + '"><div><div class="step-title">' + esc(x.title) + '</div><div class="step-hint">' + esc(x.hint) +
    "</div></div>" + (x.done || x.page === "home" ? "<span></span>" : '<button class="btn" data-go="' + esc(x.page) + '">Open</button>') + "</li>").join("");
  document.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => show(b.dataset.go)));
  $("vNote").textContent = s.steps.find((x) => x.id === "voice").done ? "Signed in. The assistant has connected to Google Voice from this computer." : "";
  if (page === "home") loadRecent();
}

$("power").addEventListener("click", async () => {
  const pw = $("power");
  const turnOn = pw.getAttribute("aria-pressed") !== "true";
  if (turnOn && state) {
    const blocking = state.steps.filter((x) => !x.done && x.id !== "test" && x.id !== "voice");
    if (blocking.length) { toast("Finish this first: " + blocking[0].title.toLowerCase() + "."); return; }
  }
  pw.classList.add("busy");
  $("powerState").textContent = turnOn ? "Starting…" : "Stopping…";
  try { await api("/api/bot", { action: turnOn ? "start" : "stop" }); toast(turnOn ? "Assistant started." : "Assistant stopped."); }
  catch (e) { toast(e.message); }
  pw.classList.remove("busy");
  refresh();
});
$("autostart").addEventListener("change", async (e) => {
  try {
    const r = await api("/api/autostart", { on: e.target.checked });
    toast(r.ok ? (e.target.checked ? "It will start with this computer." : "It will no longer start by itself.") : r.detail || "That did not work.");
  } catch (err) { toast(err.message); }
  refresh();
});

// ------------------------------------------------------------------ calls
function slip(c) {
  const rows = (c.summary || []).filter((l) => !/^\(/.test(l)).map((l) => {
    const m = l.match(/^([A-Za-z ]{2,12}):\s*(.*)$/);
    if (!m) return "<div>" + esc(l) + "</div>";
    if (m[1] === "Caller") {                      // the number is already the slip's heading
      const name = m[2].split(/\s[-\u2013\u2014]\s/)[0].trim();
      return /^unknown$/i.test(name) ? "" : "<div><b>From:</b> " + esc(name) + "</div>";
    }
    if (/^none$/i.test(m[2]) || /^unknown$/i.test(m[2])) return "";
    return "<div><b>" + esc(m[1]) + ":</b> " + esc(m[2]) + "</div>";
  }).filter(Boolean).slice(0, 5).join("");
  const quiet = !c.caller_turns;
  return '<button class="slip' + (quiet ? " quiet" : "") + '" data-call="' + esc(c.id) + '"><div class="slip-head"><span class="slip-num">' +
    esc(c.number || "Unknown number") + '</span><span class="slip-when">' + esc(c.when) + "</span></div>" +
    '<div class="slip-body">' + (quiet ? "Hung up without saying anything." : rows) +
    (c.escalation ? '<div class="flag">Needs you personally</div>' : "") + "</div></button>";
}
function bindSlips(root) { root.querySelectorAll("[data-call]").forEach((b) => b.addEventListener("click", () => openCall(b.dataset.call))); }
async function loadRecent() {
  const d = await api("/api/calls");
  const top = d.calls.slice(0, 3);
  $("recentTitle").hidden = top.length === 0;
  $("recent").innerHTML = top.map(slip).join("");
  bindSlips($("recent"));
}
async function loadCalls() {
  const d = await api("/api/calls");
  $("callsEmpty").hidden = d.calls.length > 0;
  $("callList").innerHTML = d.calls.map(slip).join("");
  bindSlips($("callList"));
}
async function openCall(id) {
  const c = await api("/api/calls/item?id=" + encodeURIComponent(id));
  $("dlgTitle").textContent = (c.number || "Unknown number") + ", " + c.when;
  const names = { caller: "Caller", agent: "Assistant", note: "", meta: "" };
  const audio = (c.audio || []).map((f) =>
    "<div><div class='hint'>" + (f.startsWith("caller") ? "Caller's side" : "Assistant's side") + "</div><audio controls preload='none' src='/api/audio?id=" +
    encodeURIComponent(id) + "&file=" + encodeURIComponent(f) + "&t=" + encodeURIComponent(window.TOKEN) + "'></audio></div>").join("");
  $("dlgBody").innerHTML =
    (c.summary.length ? '<div class="sumbox">' + c.summary.map(esc).join("<br>") + "</div>" : "") +
    c.turns.filter((t) => t.who !== "meta").map((t) =>
      '<div class="turn ' + (t.who === "note" ? "note" : "") + '"><div class="who">' + esc(names[t.who] ?? t.who) + "</div><div>" + esc(t.text) + "</div></div>").join("") + audio;
  $("callDlg").showModal();
}
$("dlgClose").addEventListener("click", () => $("callDlg").close());

// --------------------------------------------------------------- settings
let settings = null;
function getPath(o, path) { return path.split(".").reduce((a, k) => (a == null ? a : a[k]), o); }
function fillForm(form) {
  form.querySelectorAll("[name]").forEach((el) => {
    const v = getPath(settings, el.name);
    if (el.type === "checkbox") el.checked = !!v;
    else if (el.type === "radio") el.checked = String(v) === el.value;
    else el.value = v == null ? "" : v;
  });
}
function readForm(form) {
  const changes = {};
  form.querySelectorAll("[name]").forEach((el) => {
    if (el.type === "radio" && !el.checked) return;
    const [sec, key] = el.name.split(".");
    let v = el.type === "checkbox" ? el.checked : el.value;
    if (el.type === "number") v = Number(v);
    else if (typeof v === "string") v = v.trim();
    (changes[sec] = changes[sec] || {})[key] = v;
  });
  return changes;
}
async function loadSettings() {
  settings = await api("/api/settings");
  fillForm($("bizForm")); fillForm($("tuneForm"));
  $("greetHint").textContent = "Built-in greeting: “" + settings.defaults.greeting + "”";
  $("aiBase").value = settings.ai.base_url; $("aiModel").value = settings.ai.model; $("aiBackup").value = settings.ai.backup_model;
  $("keyInput").placeholder = settings.ai.has_key ? "A key is saved. Paste a new one to replace it." : "gsk_…";
  $("keyStatus").textContent = settings.ai.has_key ? (settings.ai.tested_ok ? "Connected." : "A key is saved but the last test failed.") : "No key saved yet.";
}
$("bizForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { settings = await api("/api/settings", { changes: readForm(e.target) }); flash("bizSaved", "Saved"); loadSettings(); refresh(); }
  catch (err) { toast(err.message); }
});
$("tuneForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { settings = await api("/api/settings", { changes: readForm(e.target) }); flash("tuneSaved", "Saved"); }
  catch (err) { toast(err.message); }
});
$("keyForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = $("keyInput").value.trim();
  $("keyStatus").textContent = "Testing…";
  try {
    const r = key ? await api("/api/key", { key }) : await api("/api/key/test", {});
    $("keyStatus").textContent = r.detail; $("keyInput").value = "";
    if (r.ok) { toast("AI service connected."); loadSettings(); }
  } catch (err) { $("keyStatus").textContent = err.message; }
  refresh();
});
$("aiSave").addEventListener("click", async () => {
  try { await api("/api/settings", { ai: { base_url: $("aiBase").value, model: $("aiModel").value, backup_model: $("aiBackup").value } }); toast("AI service saved. Press Save and test to check it."); }
  catch (err) { toast(err.message); }
});
$("hearGreeting").addEventListener("click", async (e) => {
  const text = $("bizForm").querySelector('[name="call.greeting"]').value.trim() || (settings && settings.defaults.greeting);
  e.target.disabled = true; e.target.textContent = "Making the audio…";
  try {
    const r = await fetch("/api/say", { method: "POST", headers: { "X-Token": window.TOKEN, "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
    if (!r.ok) throw new Error((await r.json()).error);
    new Audio(URL.createObjectURL(await r.blob())).play();
  } catch (err) { toast(err.message); }
  e.target.disabled = false; e.target.textContent = "Hear the greeting";
});

// -------------------------------------------------------------- knowledge
let kName = null;
async function loadKnowledge(pick) {
  const d = await api("/api/knowledge");
  $("kFile").innerHTML = d.files.map((f) => "<option>" + esc(f.name) + "</option>").join("");
  kName = pick || kName || (d.files[0] && d.files[0].name);
  if (kName) { $("kFile").value = kName; const f = await api("/api/knowledge/file?name=" + encodeURIComponent(kName)); $("kText").value = f.text; }
  $("kWarn").hidden = !d.unfinished;
}
$("kFile").addEventListener("change", (e) => { kName = e.target.value; loadKnowledge(kName); });
$("kNew").addEventListener("click", async () => {
  let name = prompt("Name for the new file (for example: prices)");
  if (!name) return;
  name = name.trim().replace(/\.(md|txt)$/i, "") + ".md";
  try { await api("/api/knowledge/file", { name, text: "# " + name.replace(/\.md$/, "") + "\n\n" }); kName = name; loadKnowledge(name); }
  catch (err) { toast(err.message); }
});
$("kSave").addEventListener("click", async () => {
  try { const r = await api("/api/knowledge/file", { name: kName, text: $("kText").value }); flash("kSaved", "Saved"); $("kWarn").hidden = !r.unfinished; refresh(); }
  catch (err) { toast(err.message); }
});

// ------------------------------------------------------------------- chat
let history = [];
function bubble(cls, text, small) {
  const d = document.createElement("div"); d.className = "msg " + cls; d.textContent = text;
  if (small) { const s = document.createElement("small"); s.textContent = small; d.appendChild(s); }
  $("chat").appendChild(d); $("chat").scrollTop = $("chat").scrollHeight; return d;
}
$("chatForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("chatText").value.trim(); if (!text) return;
  $("chatText").value = ""; bubble("them", text);
  const wait = bubble("bot", "…");
  try {
    const r = await api("/api/chat", { mode: "call", history, text });
    wait.remove();
    bubble("bot", r.reply, r.seconds + " s" + (r.used_knowledge ? "" : ". Nothing in your notes matched this question."));
    history.push({ role: "user", content: text }, { role: "assistant", content: r.reply });
  } catch (err) { wait.remove(); bubble("bot", err.message); }
});
$("chatClear").addEventListener("click", () => { history = []; $("chat").innerHTML = ""; });

// ------------------------------------------------------------------ voice
$("vSignin").addEventListener("click", async () => {
  try { const r = await api("/api/voice", { action: "signin" }); $("vNote").textContent = r.detail; } catch (err) { toast(err.message); }
});

// ----------------------------------------------------------- checks / log
$("runChecks").addEventListener("click", async (e) => {
  e.target.disabled = true; $("checks").textContent = "Checking…";
  try {
    const r = await api("/api/checks", {});
    $("checks").innerHTML = r.rows.map((x) => '<div class="checkrow ' + (x.ok ? "" : "bad") + '"><span class="mark">' + (x.ok ? "OK" : "Fix") +
      "</span><span>" + esc(x.label) + '</span><span class="detail">' + esc(x.detail) + "</span></div>").join("");
  } catch (err) { $("checks").textContent = err.message; }
  e.target.disabled = false;
});
$("runBench").addEventListener("click", async (e) => {
  e.target.disabled = true; const out = $("benchOut"); out.hidden = false;
  out.textContent = "Testing. This takes a few minutes the first time because it downloads the listening models…";
  try {
    const j = await api("/api/job", { kind: "bench" });
    for (;;) {
      await new Promise((r) => setTimeout(r, 3000));
      const s = await api("/api/job?id=" + j.id);
      if (s.done) { out.textContent = s.output || "No output."; break; }
    }
  } catch (err) { out.textContent = err.message; }
  e.target.disabled = false;
});
async function loadLog() {
  try { const d = await api("/api/log?n=150"); $("log").textContent = d.lines.join("\n") || "Nothing yet. The log fills in once the assistant has been turned on."; $("log").scrollTop = $("log").scrollHeight; } catch (e) { /* ignore */ }
}

refresh();
setInterval(() => { refresh(); if (page === "settings") loadLog(); }, 5000);
