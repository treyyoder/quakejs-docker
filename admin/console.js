const $ = id => document.getElementById(id);
let state = null, settingsSpec = null, rotation = [], knownMaps = [], pollTimer = null;
let activeTab = "chat";
// The console sits in an iframe on the game page and keeps running when the
// modal is hidden. Polling and re-rendering there steals main-thread time from
// the game, so both are suspended while it is not on screen.
let consoleVisible = true;

function show(el, text, kind) {
  el.textContent = text;
  el.className = text ? "msg show " + kind : "msg";
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function mb(bytes) { return (bytes / 1048576).toFixed(0) + " MB"; }

async function api(path, body) {
  const res = await fetch(path, body ? {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  } : {});
  let data;
  try { data = await res.json(); } catch { throw new Error("HTTP " + res.status); }
  if (!res.ok || data.error) throw new Error(data.error || "HTTP " + res.status);
  return data;
}

// --- confirmation modal ----------------------------------------------------
// Replaces window.confirm so destructive actions never raise a browser dialog.
let confirmResolve = null;

function askConfirm(text, label) {
  $("confirmtext").textContent = text;
  $("confirmyes").textContent = label || "Confirm";
  $("confirm").hidden = false;
  $("confirmyes").focus();
  return new Promise(resolve => { confirmResolve = resolve; });
}

function settleConfirm(answer) {
  if (!confirmResolve) return;
  const resolve = confirmResolve;
  confirmResolve = null;
  $("confirm").hidden = true;
  resolve(answer);
}

function confirmOpen() { return confirmResolve !== null; }

$("confirmyes").onclick = () => settleConfirm(true);
$("confirmno").onclick = () => settleConfirm(false);
$("confirm").addEventListener("mousedown", e => {
  if (e.target === $("confirm")) settleConfirm(false);  // click outside the box
});

// --- views -----------------------------------------------------------------
function view(name) {
  $("signin").hidden = name !== "signin";
  $("pwpanel").hidden = name !== "password";
  $("main").hidden = name === "signin" || name === "password";
  $("signout").hidden = !isAdmin;
  $("changepw").hidden = !isAdmin;
  $("adminlogin").hidden = isAdmin || name === "signin";
  document.querySelectorAll("[data-admin]").forEach(b => (b.hidden = !isAdmin));
  applyPolling(name === "main");
}

function applyPolling(active) {
  const want = active && consoleVisible;
  if (want && isAdmin && !pollTimer) pollTimer = setInterval(refresh, 15000);
  if ((!want || !isAdmin) && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (want && !publicTimer) publicTimer = setInterval(refreshPublic, 10000);
  if (!want && publicTimer) { clearInterval(publicTimer); publicTimer = null; }
}

window.addEventListener("message", e => {
  if (e.origin !== window.location.origin) return;
  if (e.data && e.data.type === "qadmin-visible") {
    consoleVisible = !!e.data.visible;
    applyPolling(!$("main").hidden);
    if (consoleVisible) { refreshPublic(); if (isAdmin) refresh(); }
  }
});

function tab(name) {
  if (!isAdmin && name !== "chat" && name !== "stats") name = "chat";
  activeTab = name;
  document.querySelectorAll(".tabs button").forEach(b =>
    b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  ["chat", "stats", "server", "match", "players", "maps", "log"].forEach(t => {
    $("tab-" + t).hidden = t !== name;
  });
  if (name === "chat") { refreshPublic(); selectThread(activeThread); }
  if (name === "server" && state) fillMapPicker();
  if (name === "server") loadBackups();
  if (name === "players" && state) { renderPlayers(state.players); fillBots(state.bots); fillBotRoom(state.bot_room); }
  if (name === "maps" && state) { renderRotation(); renderMaps(); }
  if (name === "match" && !settingsSpec) loadSettings();
  if (name === "match") loadPresets();
  if (name === "players") loadBans();
  if (name === "log") { loadLog(); loadAudit(); loadCrashes(); }
  if (name === "stats") loadStats();
}
document.querySelectorAll(".tabs button").forEach(b => (b.onclick = () => tab(b.dataset.tab)));

// --- auth ------------------------------------------------------------------
async function start() {
  $("myname").value = savedName();
  try { $("lowdetail").checked = localStorage.getItem(DETAIL_KEY) === "1"; } catch {}
  let session = {};
  try { session = await api("api/session"); isAdmin = !!session.authenticated; } catch { isAdmin = false; }
  view("main");
  tab("chat");
  await refreshPublic();
  if (isAdmin) await refresh();
}

$("adminlogin").onclick = () => { view("signin"); $("pass").focus(); };
$("signincancel").onclick = () => { view("main"); tab("chat"); };

$("signinform").onsubmit = async e => {
  e.preventDefault();
  $("signinbtn").disabled = true;
  try {
    await api("api/login", { user: $("user").value, password: $("pass").value });
    $("pass").value = "";
    show($("signinmsg"), "", "ok");
    isAdmin = true;
    view("main");
    tab("server");
    await refresh();
  } catch (err) { show($("signinmsg"), err.message, "err"); }
  finally { $("signinbtn").disabled = false; }
};

$("signout").onclick = async () => {
  try { await api("api/logout", {}); } catch {}
  isAdmin = false;
  view("main");
  tab("chat");
};

$("changepw").onclick = () => {
  ["pwcur", "pwnew", "pwconf"].forEach(id => ($(id).value = ""));
  show($("pwmsg"), "", "ok");
  view("password"); $("pwcur").focus();
};
$("pwcancel").onclick = () => { view("main"); tab("server"); };

$("pwform").onsubmit = async e => {
  e.preventDefault();
  if ($("pwnew").value !== $("pwconf").value) return show($("pwmsg"), "New passwords do not match.", "err");
  if ($("pwnew").value.length < 8) return show($("pwmsg"), "New password must be at least 8 characters.", "err");
  try {
    await api("api/password", { current: $("pwcur").value, new: $("pwnew").value });
    isAdmin = false;
    view("signin");
    show($("signinmsg"), "Password changed. Sign in again.", "ok");
    $("pass").focus();
  } catch (err) { show($("pwmsg"), err.message, "err"); }
};

// --- server tab ------------------------------------------------------------
function setShot(img, fallback, name) {
  if (!name) { img.hidden = true; if (fallback) fallback.hidden = false; return; }
  img.onload = () => { img.hidden = false; if (fallback) fallback.hidden = true; };
  img.onerror = () => { img.hidden = true; if (fallback) fallback.hidden = false; };
  img.src = "api/levelshot/" + encodeURIComponent(name);
}

function mapsForCurrentType() {
  if (!state) return [];
  const type = (state.gametypeKey || "").toLowerCase();
  if (!type) return state.maps;
  const fits = state.maps.filter(m => {
    const types = (state.arenas[m] || {}).types || [];
    return types.length === 0 || types.includes(type);
  });
  return fits.length ? fits : state.maps;
}

function fillMapPicker() {
  const list = mapsForCurrentType();
  if (JSON.stringify(list) === JSON.stringify(knownMaps)) return;
  knownMaps = list;
  const current = $("mapsel").value;
  $("mapsel").innerHTML = list.map(m => {
    const long = (state.arenas[m] || {}).longname;
    return '<option value="' + esc(m) + '">' + esc(long ? m + " - " + long : m) + "</option>";
  }).join("");
  if (list.includes(current)) $("mapsel").value = current;
  else if (state.map && list.includes(state.map)) $("mapsel").value = state.map;
  setShot($("pickshot"), null, $("mapsel").value);
}
$("mapsel").onchange = () => setShot($("pickshot"), null, $("mapsel").value);

$("mapgo").onclick = async () => {
  const map = $("mapsel").value;
  show($("mapmsg"), "Loading " + map + "...", "work");
  try {
    await api("api/map", { map });
    setTimeout(refresh, 2500);
    show($("mapmsg"), "Switched to " + map + ".", "ok");
  } catch (e) { show($("mapmsg"), e.message, "err"); }
};

$("restart").onclick = async () => {
  if (!await askConfirm("Restart the game server? Connected players will be dropped.", "Restart")) return;
  show($("mapmsg"), "Restarting...", "work");
  try {
    await api("api/restart", {});
    setTimeout(refresh, 9000);
    show($("mapmsg"), "Restart sent; the server comes back in a few seconds.", "ok");
  } catch (e) { show($("mapmsg"), e.message, "err"); }
};

$("saygo").onclick = async () => {
  const message = $("saytext").value.trim();
  if (!message) return;
  try {
    await api("api/say", { message });
    $("saytext").value = "";
    show($("saymsg"), "Sent.", "ok");
  } catch (e) { show($("saymsg"), e.message, "err"); }
};

$("joinbtn").onclick = () => {
  // The client reads "+set" arguments from the query string, so a reserved-slot
  // password can be handed straight to the game.
  const pw = (settingsSpec && settingsSpec.sv_privatePassword || {}).value;
  window.open(location.origin + launchArgs(pw ? ["set password " + pw] : []), "_blank");
};

// --- players tab -----------------------------------------------------------
function renderPlayers(players) {
  const host = $("players");
  if (!players.length) { host.innerHTML = '<p class="empty">Nobody connected.</p>'; return; }
  // How many connected players share each address. A ban matches the address,
  // so banning a shared one takes every player behind it - which is what used
  // to happen to everybody at once, before the server learned to read
  // X-Forwarded-For and see past the proxy.
  const sharing = {};
  players.forEach(p => { if (p.address) sharing[p.address] = (sharing[p.address] || 0) + 1; });
  host.innerHTML =
    "<table><thead><tr><th>Name</th><th>Score</th><th>Ping</th><th>Address</th><th></th></tr></thead><tbody>" +
    players.map(p => {
      const shared = p.address && sharing[p.address] > 1;
      return "<tr><td>" + esc(p.name) + " " +
      (p.bot ? '<span class="tag bot">bot</span>' : '<span class="tag">human</span>') +
      "</td><td>" + p.score + "</td><td>" + (p.bot ? "-" : p.ping) + "</td><td>" +
      (p.address
        ? esc(p.address) + (shared ? ' <span class="tag shared">shared x' + sharing[p.address] + '</span>' : "")
        : "-") +
      "</td><td>" +
      '<select class="tiny auto" data-team="' + p.num + '">' +
        '<option value="">team...</option><option value="red">red</option>' +
        '<option value="blue">blue</option><option value="spectator">spectator</option>' +
        '<option value="free">free</option></select> ' +
      '<button class="ghost tiny" data-kick="' + p.num + '">Kick</button>' +
      (p.address ? ' <button class="ghost tiny" data-ban="' + p.num + '">Ban</button>' : "") +
      "</td></tr>";
    }).join("") + "</tbody></table>";

  host.querySelectorAll("[data-kick]").forEach(btn => {
    btn.onclick = async () => {
      btn.disabled = true;
      try { await api("api/kick", { num: Number(btn.dataset.kick) }); await refresh(); }
      catch (e) { btn.disabled = false; show($("playermsg"), e.message, "err"); }
    };
  });
  host.querySelectorAll("[data-ban]").forEach(btn => {
    btn.onclick = async () => {
      const p = players.find(x => x.num === Number(btn.dataset.ban));
      if (!p) return;
      const others = sharing[p.address] - 1;
      const warning = others > 0
        ? " " + others + (others > 1 ? " other players are" : " other player is") +
          " connected from that address and will be shut out too."
        : "";
      if (!await askConfirm("Ban " + p.name + " at " + p.address + "?" + warning, "Ban")) return;
      btn.disabled = true;
      try {
        // addip only filters new connections, so the player has to be kicked
        // as well or the ban does nothing until they next reconnect.
        await api("api/ban", banRequest(p.address));
        await api("api/kick", { num: p.num });
        await loadBans();
        await refresh();
      } catch (e) { btn.disabled = false; show($("playermsg"), e.message, "err"); }
    };
  });
  host.querySelectorAll("[data-team]").forEach(sel => {
    sel.onchange = async () => {
      if (!sel.value) return;
      try {
        await api("api/team", { num: Number(sel.dataset.team), team: sel.value });
        show($("playermsg"), "Moved to " + sel.value + ".", "ok");
        setTimeout(refresh, 1200);
      } catch (e) { show($("playermsg"), e.message, "err"); }
      sel.value = "";
    };
  });
}

// A ban is an address with, when given, a reason and an end. The fields on
// the Bans panel serve both the panel's button and the per-player one.
function banRequest(ip) {
  const hours = $("banhours").value;
  return { ip, reason: $("banreason").value.trim(), hours: hours ? Number(hours) : null };
}
function remaining(seconds) {
  if (seconds == null) return "for good";
  if (seconds < 3600) return Math.max(1, Math.round(seconds / 60)) + " min left";
  if (seconds < 86400) return Math.round(seconds / 3600) + " h left";
  return Math.round(seconds / 86400) + " d left";
}
async function loadBans() {
  try {
    const { bans } = await api("api/bans");
    $("banlist").innerHTML = bans.length
      ? '<table class="bans">' + bans.map(b =>
          "<tr><td>" + esc(b.ip) + "</td><td>" + esc(b.reason || "") + '</td><td class="t">' + esc(remaining(b.remaining)) +
          (b.enforced ? "" : ' <span class="tag">not enforced by this game module</span>') +
          '</td><td><button class="ghost tiny" data-unban="' + esc(b.ip) + '">Unban</button></td></tr>').join("") + "</table>"
      : '<p class="empty">Nobody is banned.</p>';
    $("banlist").querySelectorAll("[data-unban]").forEach(btn => {
      btn.onclick = async () => {
        try { await api("api/unban", { ip: btn.dataset.unban }); await loadBans(); }
        catch (e) { show($("banmsg"), e.message, "err"); }
      };
    });
  } catch (e) { /* the state poll surfaces auth failures */ }
}

$("bango").onclick = async () => {
  try {
    const r = await api("api/ban", banRequest($("banip").value.trim()));
    $("banip").value = ""; $("banreason").value = "";
    show($("banmsg"), "Banned " + r.ip + " " + remaining(r.expires ? r.expires - Date.now() / 1000 : null) + ".", "ok");
    await loadBans();
  } catch (e) { show($("banmsg"), e.message, "err"); }
};

// --- messenger -------------------------------------------------------------
// Threads are assembled from two sources: messages this console sent (recorded
// server-side as they go out) and player chat tailed from the server log. A
// console user is not a connected client, so a player cannot address a reply
// back to them - replies arrive in the Everyone thread instead.
const NAME_KEY = "qadmin.name";
const DETAIL_KEY = "qadmin.lowdetail";
// The server copes with a full game easily; the browser is the limit, because it
// renders Quake in JS. These trim the client's per-frame work and cap its frame
// rate so the render loop stops starving the network pump, which is what shows
// up in game as "connection interrupted".
const LOW_DETAIL = [
  // Firing is the worst case: every rocket, plasma ball and lightning bolt casts
  // a dynamic light, and each impact leaves a mark and ejects brass. Those are
  // the first things to drop when the client cannot keep up.
  "set r_dynamiclight 0", "set cg_marks 0", "set cg_brassTime 0",
  "set cg_gibs 0", "set r_flares 0", "set cg_railTrailTime 0",
  // General render cost.
  "set r_vertexlight 1", "set r_picmip 3", "set r_fastsky 1", "set cg_shadows 0",
  // Cap the render loop so it stops starving the network pump.
  "set com_maxfps 60",
];

function launchArgs(extra) {
  const args = extra.slice();
  if ($("lowdetail").checked) args.push.apply(args, LOW_DETAIL);
  return "/play.html?" + args.map(encodeURIComponent).join("&");
}
const EVERYONE = " everyone";
let isAdmin = false;
let publicTimer = null;
let messages = [];
let lastSeq = 0;
let activeThread = EVERYONE;
let roster = [];
let unread = {};

function savedName() {
  try { return localStorage.getItem(NAME_KEY) || ""; } catch { return ""; }
}
function rememberName(name) {
  try { localStorage.setItem(NAME_KEY, name); } catch { /* private browsing */ }
}
function myName() { return ($("myname").value || "").trim() || "guest"; }

function threadFor(m) {
  if (m.kind === "tell") return m.from;
  if (m.kind === "sent-pm") return m.to;
  return EVERYONE;
}

function isMine(m) {
  return m.kind === "sent" || m.kind === "sent-pm" ||
         (m.from || "").toLowerCase() === myName().toLowerCase();
}

function renderContacts() {
  // Humans only. A bot cannot read a private message, so listing them as
  // contacts is just noise.
  const isBot = name => (roster.find(p => p.name === name) || {}).bot === true;
  const names = roster.filter(p => !p.bot).map(p => p.name);
  messages.forEach(m => {
    const t = threadFor(m);
    if (t !== EVERYONE && !isBot(t) && names.indexOf(t) < 0) names.push(t);
  });

  const rows = [{ id: EVERYONE, label: "Everyone", note: "public" }].concat(
    names.filter((n, i) => n && names.indexOf(n) === i)
         .map(n => ({ id: n, label: n,
                      note: roster.some(p => p.name === n) ? "" : "away" })));

  $("contactlist").innerHTML = rows.map(r =>
    '<li data-thread="' + esc(r.id) + '"' +
    (r.id === activeThread ? ' aria-current="true"' : "") + ">" +
    "<span>" + esc(r.label) + "</span>" +
    (r.note ? '<span class="tag">' + esc(r.note) + "</span>" : "") +
    (unread[r.id] ? '<span class="unread">' + unread[r.id] + "</span>" : "") +
    "</li>").join("");

  if (rows.length === 1) {
    $("contactlist").innerHTML +=
      '<li class="empty plain">No other players online.</li>';
  }

  $("contactlist").querySelectorAll("[data-thread]").forEach(li => {
    li.onclick = () => selectThread(li.dataset.thread);
  });
}

function selectThread(id) {
  activeThread = id;
  delete unread[id];
  $("threadname").textContent = id === EVERYONE ? "Everyone" : id;
  $("threadnote").textContent = id === EVERYONE ? "public" : "private";
  renderContacts();
  renderThread();
}

function renderThread() {
  const host = $("bubbles");
  const pinned = host.scrollHeight - host.scrollTop - host.clientHeight < 60;
  const shown = messages.filter(m => threadFor(m) === activeThread);
  host.innerHTML = shown.length
    ? shown.map(m =>
        '<div class="bubble' + (isMine(m) ? " mine" : "") +
        (m.kind === "team" ? " team" : "") + '">' +
        '<span class="who">' + esc(m.from) +
        (m.kind === "team" ? " (team)" : "") +
        (m.kind === "sent-pm" ? " to " + esc(m.to) : "") + "</span>" +
        esc(m.text) + "</div>").join("")
    : '<p class="empty">No messages yet.</p>';
  if (pinned) host.scrollTop = host.scrollHeight;
}

// Only one poll in flight at a time. lastSeq only moves once a response has
// arrived, so two overlapping polls would both ask from the same sequence
// number and both append the same messages.
let pollingMessages = false;

async function pollMessages() {
  if (pollingMessages) return;
  pollingMessages = true;
  try {
    const d = await api("api/messages?since=" + lastSeq);
    // Belt and braces: never append anything already counted.
    const fresh = (d.messages || []).filter(m => m.seq > lastSeq);
    if (!fresh.length) return;
    fresh.forEach(m => {
      lastSeq = Math.max(lastSeq, m.seq);
      messages.push(m);
      const t = threadFor(m);
      const fromBot = (roster.find(p => p.name === t) || {}).bot === true;
      if (t !== activeThread && !isMine(m) && !fromBot) unread[t] = (unread[t] || 0) + 1;
    });
    if (messages.length > 500) messages = messages.slice(-500);
    renderContacts();
    renderThread();
  } catch (e) { /* the public poll surfaces connectivity problems */ }
  finally { pollingMessages = false; }
}

async function refreshPublic() {
  try {
    const d = await api("api/public");
    roster = d.players || [];
    renderContacts();
    if (!isAdmin) {
      $("status").innerHTML = "<b>" + esc(d.map || "?") + "</b> &middot; " +
        roster.length + " connected" + (d.hostname ? " &middot; " + esc(d.hostname) : "");
    }
  } catch (e) { /* keep the last known roster */ }
  await pollMessages();
}

async function sendMessage() {
  const text = $("composetext").value.trim();
  if (!text) return;
  const name = myName();
  rememberName(name);
  try {
    if (activeThread === EVERYONE) {
      await api("api/chat", { name, message: text });
    } else {
      const target = roster.find(p => p.name === activeThread);
      if (!target) return show($("chatmsg"), activeThread + " is not connected right now.", "err");
      await api("api/pm", { name, to: target.num, message: text });
    }
    $("composetext").value = "";
    show($("chatmsg"), "", "ok");
    await pollMessages();
  } catch (e) { show($("chatmsg"), e.message, "err"); }
}

$("composesend").onclick = sendMessage;
$("composetext").onkeydown = e => {
  if (e.key === "Enter") { e.preventDefault(); sendMessage(); }
};

// Saving the name is playing under it. The client only takes a name at
// launch, so over the game this reloads it - after asking, since it rejoins.
// As a page of its own the console just remembers the name; the next launch
// of the game applies it, from here or from the front page.
$("namesave").onclick = async () => {
  const name = $("myname").value.trim();
  if (!name) return show($("namemsg"), "Pick a name first.", "err");
  rememberName(name);
  if (window.parent === window)
    return show($("namemsg"), "Saved - the game will use it next time you join.", "ok");
  if (!await askConfirm("Reload the game as " + name + "? You will rejoin the server.", "Reload"))
    return show($("namemsg"), "Saved - the game will use it next time you join.", "ok");
  window.parent.location = launchArgs(["set name " + name]);
};

$("lowdetail").onchange = () => {
  try { localStorage.setItem(DETAIL_KEY, $("lowdetail").checked ? "1" : "0"); } catch {}
  show($("namemsg"), $("lowdetail").checked
    ? "Low detail will apply next time you load the game."
    : "Full detail will apply next time you load the game.", "ok");
};

// --- bots ------------------------------------------------------------------
function fillBots(bots) {
  if ($("botsel").options.length) return;
  $("botsel").innerHTML = bots.map(b =>
    '<option value="' + esc(b) + '">' + esc(b) + "</option>").join("");
}
// How many more fit right now: free client slots, or the game module's
// memory ceiling, whichever is lower. The server refuses more either way;
// this keeps the field honest.
function fillBotRoom(room) {
  if (!room) return;
  const input = $("botcount");
  input.max = Math.max(1, room.room);
  if (Number(input.value) > room.room) input.value = Math.max(1, room.room);
  $("botroom").textContent = room.room > 0 ? "up to " + room.room : "none fit";
  $("botroom").title = room.slots + " of " + room.maxclients + " client slots free; the game module holds " +
    room.ceiling + " bots at most";
}

$("botgo").onclick = async () => {
  try {
    const count = Number($("botcount").value) || 1;
    const r = await api("api/bot", {
      name: $("botsel").value,
      skill: Number($("botskill").value),
      count: count,
    });
    show($("botmsg"), "Added " + r.count + " " + r.bot +
      (r.count === 1 ? "" : "s") + " at skill " + r.skill + ".", "ok");
    setTimeout(refresh, 1500 + 400 * r.count);
  } catch (e) { show($("botmsg"), e.message, "err"); }
};

$("botclear").onclick = async () => {
  const bots = (state ? state.players : []).filter(p => p.bot);
  if (!bots.length) return show($("botmsg"), "No bots connected.", "work");
  if (!await askConfirm("Remove " + bots.length + " bot" + (bots.length === 1 ? "" : "s") + " from the server?", "Remove")) return;
  show($("botmsg"), "Removing " + bots.length + " bot(s)...", "work");
  try {
    // Kick highest slot first: removing a client renumbers the ones above it.
    for (const b of bots.slice().sort((x, y) => y.num - x.num)) {
      await api("api/kick", { num: b.num });
    }
    show($("botmsg"), "Removed. If bots reappear, lower Min players on the Match tab.", "ok");
    setTimeout(refresh, 1800);
  } catch (e) { show($("botmsg"), e.message, "err"); }
};

// --- match tab -------------------------------------------------------------
let presets = [];
async function loadPresets() {
  if (presets.length) return;
  try {
    presets = (await api("api/presets")).presets;
    $("preset").innerHTML = presets.map(p => '<option value="' + esc(p.key) + '">' + esc(p.label) + "</option>").join("");
    $("preset").onchange = () => {
      const p = presets.find(x => x.key === $("preset").value);
      $("presethint").textContent = p ? p.blurb : "";
    };
    $("preset").onchange();
  } catch (e) { /* the state poll surfaces auth failures */ }
}
$("presetgo").onclick = async () => {
  const p = presets.find(x => x.key === $("preset").value);
  if (!p) return;
  show($("presetmsg"), "Applying " + p.label + "...", "work");
  try {
    const r = await api("api/settings", { settings: p.values, restart: false });
    // The game type is latched: it reads back old until the map restart the
    // change triggers has finished - seconds, more with a big pak - so wait
    // for it to land before showing the form again.
    if (r.reloaded) {
      const want = String(p.values.g_gametype);
      for (let i = 0; i < 15; i++) {
        await new Promise(done => setTimeout(done, 2000));
        try { if ((await api("api/settings")).settings.g_gametype.value === want) break; } catch (e) { /* keep waiting */ }
      }
    }
    settingsSpec = null;
    await loadSettings();
    show($("presetmsg"), p.label + " applied" + (r.reloaded ? "; the map restarted for the new game type" : "") + ". Fill the rotation on the Maps tab to match.", "ok");
  } catch (e) { show($("presetmsg"), e.message, "err"); }
};

const GROUPS = {
  rules: ["g_gametype", "timelimit", "fraglimit", "capturelimit"],
  network: ["sv_fps"],
  slots: ["sv_maxclients", "bot_minplayers", "sv_privateClients", "sv_privatePassword"],
  mutators: ["g_gravity", "g_speed", "g_quadfactor", "g_weaponrespawn", "g_friendlyfire", "g_inactivity"],
  identity: ["sv_hostname", "g_motd"],
};

function field(name, spec) {
  const id = "set_" + name;
  const val = spec.value == null ? "" : spec.value;
  let control;
  if (name === "g_gametype") {
    control = '<select id="' + id + '" data-setting="' + name + '">' +
      Object.entries(state.gametypes).map(([k, v]) =>
        '<option value="' + k + '"' + (String(val) === k ? " selected" : "") + ">" +
        esc(v) + "</option>").join("") + "</select>";
  } else if (spec.kind === "int" && spec.min === 0 && spec.max === 1) {
    control = '<select id="' + id + '" data-setting="' + name + '">' +
      ["0", "1"].map(v => '<option value="' + v + '"' +
        (String(val) === v ? " selected" : "") + ">" + (v === "1" ? "on" : "off") +
        "</option>").join("") + "</select>";
  } else {
    const type = spec.secret ? "password" : (spec.kind === "int" ? "number" : "text");
    const bounds = spec.kind === "int" ? ' min="' + spec.min + '" max="' + spec.max + '"' : "";
    // A secret never comes back from the server. Blank means keep it; the
    // checkbox is the only way to clear it, and sends an explicit null.
    const hint = spec.secret ? ' placeholder="' + (spec.present ? '(set - leave blank to keep)' : '(not set)') + '"' : '';
    control = '<input id="' + id + '" data-setting="' + name + '" autocomplete="off"'
      + ' type="' + type + '"' + hint +
      bounds + ' value="' + esc(val) + '">' +
      (spec.secret && spec.present
        ? '<label class="check after">' +
          '<input type="checkbox" data-clear="' + name + '">Clear it</label>'
        : '');
  }
  return '<div><label for="' + id + '">' + esc(spec.label) + "</label>" + control + "</div>";
}

async function loadSettings() {
  try {
    const data = await api("api/settings");
    settingsSpec = data.settings;
    for (const group of Object.keys(GROUPS)) {
      $(group).innerHTML = GROUPS[group]
        .filter(n => settingsSpec[n]).map(n => field(n, settingsSpec[n])).join("");
    }
    show($("setmsg"), "", "ok");
  } catch (e) { show($("setmsg"), e.message, "err"); }
}
$("reloadsettings").onclick = loadSettings;

$("savesettings").onclick = async () => {
  const settings = {};
  document.querySelectorAll("[data-setting]").forEach(el => {
    const name = el.dataset.setting;
    const original = (settingsSpec[name] || {}).value;
    if (String(el.value) !== String(original == null ? "" : original)) settings[name] = el.value;
  });
  document.querySelectorAll("[data-clear]").forEach(el => {
    if (el.checked) settings[el.dataset.clear] = null;
  });
  if (!Object.keys(settings).length) return show($("setmsg"), "Nothing changed.", "work");
  const restarting = Object.keys(settings).some(n => settingsSpec[n].restart);
  if (restarting && !await askConfirm("These settings restart the server. Connected players will be dropped.", "Save and restart")) return;
  show($("setmsg"), "Applying...", "work");
  try {
    const r = await api("api/settings", { settings });
    const notes = [];
    if (r.reloaded) notes.push("map reloaded");
    if (r.restarted) notes.push("server restarting");
    show($("setmsg"), "Saved " + Object.keys(r.applied).join(", ") +
      (notes.length ? " (" + notes.join("; ") + ")" : ""), "ok");
    setTimeout(() => { loadSettings(); refresh(); }, r.restarted ? 9000 : 1500);
  } catch (e) { show($("setmsg"), e.message, "err"); }
};

// --- maps tab --------------------------------------------------------------
let mapsSignature = "";
function renderMaps() {
  const signature = state.maps.join(",") + "|" + state.removable.join(",");
  if (signature === mapsSignature) return;   // avoid re-decoding every thumbnail
  mapsSignature = signature;
  const removable = new Set(state.removable);
  $("usage").textContent = state.usage.paks + " paks, " + mb(state.usage.bytes);
  $("maplist").innerHTML = state.maps.map(m => {
    const meta = state.arenas[m] || { types: [], longname: null };
    return '<div class="mapcard">' +
      '<img class="shot" loading="lazy" alt="" src="api/levelshot/' + encodeURIComponent(m) +
        '">' +
      '<div class="name">' + esc(m) + "</div>" +
      '<div class="meta">' + esc(meta.longname || "") + "</div>" +
      '<div class="meta">' + esc(meta.types.join(" ") || "any") + "</div>" +
      '<div class="acts"><button class="ghost tiny" data-play="' + esc(m) + '">Play</button>' +
      (removable.has(m) ? '<button class="ghost tiny" data-remove="' + esc(m) + '">Remove</button>' : "") +
      "</div></div>";
  }).join("");
  // A map without a browser-readable levelshot answers 404; hide the broken
  // image. Attached here rather than inline: the page's policy allows no
  // inline handlers, and the error event fires after this task anyway.
  $("maplist").querySelectorAll("img.shot").forEach(img => { img.onerror = () => { img.hidden = true; }; });

  $("maplist").querySelectorAll("[data-play]").forEach(b => {
    b.onclick = async () => {
      try {
        await api("api/map", { map: b.dataset.play });
        show($("mapsmsg"), "Loading " + b.dataset.play + "...", "ok");
        setTimeout(refresh, 2500);
      } catch (e) { show($("mapsmsg"), e.message, "err"); }
    };
  });
  $("maplist").querySelectorAll("[data-remove]").forEach(b => {
    b.onclick = async () => {
      if (!await askConfirm("Delete the map " + b.dataset.remove + "? Its pk3 is removed and the server restarts.", "Delete")) return;
      show($("mapsmsg"), "Removing " + b.dataset.remove + "...", "work");
      try {
        await api("api/uninstall", { map: b.dataset.remove });
        show($("mapsmsg"), "Removed. Server restarting.", "ok");
        setTimeout(refresh, 9000);
      } catch (e) { show($("mapsmsg"), e.message, "err"); }
    };
  });
}

function renderRotation() {
  $("rotcount").textContent = rotation.length ? rotation.length + " maps" : "using server.cfg";
  $("rotlist").innerHTML = rotation.length
    ? rotation.map((m, i) =>
        '<li><span class="n">' + (i + 1) + '</span><span class="grow">' + esc(m) + "</span>" +
        '<button class="ghost tiny" data-up="' + i + '"' + (i ? "" : " disabled") + ">up</button>" +
        '<button class="ghost tiny" data-down="' + i + '"' + (i < rotation.length - 1 ? "" : " disabled") + ">down</button>" +
        '<button class="ghost tiny" data-del="' + i + '">remove</button></li>').join("")
    : '<li><span class="empty">Empty - the rotation baked into server.cfg is used.</span></li>';
  $("rotadd").innerHTML = state.maps.map(m => '<option value="' + esc(m) + '">' + esc(m) + "</option>").join("");
  $("rotlist").querySelectorAll("[data-up]").forEach(b => b.onclick = () => {
    const i = Number(b.dataset.up);
    const tmp = rotation[i - 1]; rotation[i - 1] = rotation[i]; rotation[i] = tmp;
    renderRotation();
  });
  $("rotlist").querySelectorAll("[data-down]").forEach(b => b.onclick = () => {
    const i = Number(b.dataset.down);
    const tmp = rotation[i + 1]; rotation[i + 1] = rotation[i]; rotation[i] = tmp;
    renderRotation();
  });
  $("rotlist").querySelectorAll("[data-del]").forEach(b => b.onclick = () => {
    rotation.splice(Number(b.dataset.del), 1); renderRotation();
  });
}
$("rotaddbtn").onclick = () => { rotation.push($("rotadd").value); renderRotation(); };
// Presets fill the list from what is installed. The stock ones are only
// there once pak0.pk3 has been supplied; the demo ships four maps.
const STOCK = { dm: /^q3dm\d+$/, tourney: /^q3tourney\d+$/, ctf: /^q3ctf\d+$/ };
const byNumber = (a, b) => a.localeCompare(b, undefined, { numeric: true });
function presetMaps(kind) {
  const maps = state ? state.maps.slice() : [];
  if (kind === "type") return mapsForCurrentType().slice().sort(byNumber);
  if (kind === "all") return maps.sort(byNumber);
  return maps.filter(m => STOCK[kind].test(m)).sort(byNumber);
}
$("rotfill").onclick = () => {
  const picked = presetMaps($("rotpreset").value);
  if (!picked.length) return show($("rotmsg"), "No installed map matches that preset.", "err");
  rotation = picked; renderRotation();
  show($("rotmsg"), picked.length + " maps listed - Save rotation to keep them.", "ok");
};
$("rotclear").onclick = () => { rotation = []; renderRotation(); show($("rotmsg"), "", "ok"); };
$("rotsave").onclick = async () => {
  try {
    const r = await api("api/rotation", { maps: rotation });
    show($("rotmsg"), r.note, "ok");
  } catch (e) { show($("rotmsg"), e.message, "err"); }
};

$("lookup").onclick = async () => {
  const ref = $("ref").value.trim();
  if (!ref) return;
  show($("instmsg"), "Looking up...", "work");
  $("install").disabled = $("force").disabled = true;
  try {
    const m = await api("api/lookup", { ref });
    show($("instmsg"), m.filename + " - " + (m.filesize || "?") + " MiB\n" + m.page, "ok");
    $("install").disabled = false;
  } catch (e) { show($("instmsg"), e.message, "err"); }
};

async function doInstall(force) {
  const ref = $("ref").value.trim();
  show($("instmsg"), "Downloading and verifying... this can take a minute.", "work");
  $("install").disabled = $("force").disabled = true;
  try {
    const r = await api("api/install", { ref, force });
    show($("instmsg"), "Installed " + r.meta.filename + "\nMaps: " +
      (r.maps.join(", ") || "none found") + "\nServer restarting...", "ok");
    setTimeout(refresh, 9000);
  } catch (e) {
    show($("instmsg"), e.message, "err");
    $("install").disabled = false;
    if (/shaders missing/.test(e.message)) $("force").disabled = false;
  }
}
$("install").onclick = () => doInstall(false);
$("force").onclick = () => doInstall(true);

// --- uploading a pk3 -------------------------------------------------------
// One control for both jobs, because the server decides by filename rather than
// by where the file came from: pak0.pk3 - pak8.pk3 are the game's own content
// and go in whole, anything else is treated as a map and screened for textures
// this server does not have. XMLHttpRequest rather than fetch, since a base pak
// runs to hundreds of megabytes and only XHR reports upload progress.
const BASE_PAK_NAME = /^pak\d+\.pk3$/i;
const pk3input = $("pk3");

pk3input.onchange = () => {
  $("upload").disabled = !pk3input.files.length;
  $("uploadforce").disabled = true;
  if (!pk3input.files.length) return;
  const file = pk3input.files[0];
  show($("upmsg"), file.name + " - " + (file.size / 1048576).toFixed(1) + " MiB" +
    (BASE_PAK_NAME.test(file.name) ? " - game assets, installed as-is" : ""), "ok");
};

function doUpload(force) {
  const file = pk3input.files[0];
  if (!file) return;
  $("upload").disabled = $("uploadforce").disabled = true;
  const req = new XMLHttpRequest();
  req.open("POST", "api/upload?name=" + encodeURIComponent(file.name) +
    (force ? "&force=1" : ""));
  req.setRequestHeader("Content-Type", "application/octet-stream");
  req.upload.onprogress = e => {
    if (!e.lengthComputable) return;
    show($("upmsg"), "Uploading " + Math.round(100 * e.loaded / e.total) + "% (" +
      (e.loaded / 1048576).toFixed(0) + " of " + (e.total / 1048576).toFixed(0) +
      " MiB)", "work");
  };
  // Fires when the body has gone but before the server answers, which for a
  // map is the slow part: it is unpacked and screened before anything replies.
  req.upload.onload = () => show($("upmsg"), "Installing...", "work");
  req.onload = () => {
    let data = {};
    try { data = JSON.parse(req.responseText); } catch {}
    if (req.status >= 200 && req.status < 300 && data.ok) {
      const bots = (data.bots || []).length;
      show($("upmsg"), "Installed " + data.files.join(", ") +
        "\nMaps: " + (data.maps.join(", ") || "none") +
        (bots ? "\nBots: " + bots : "") +
        "\nServer restarting...", "ok");
      pk3input.value = "";
      setTimeout(refresh, 12000);
      return;
    }
    const message = data.error || ("HTTP " + req.status);
    show($("upmsg"), message, "err");
    $("upload").disabled = false;
    if (/shaders missing/.test(message)) $("uploadforce").disabled = false;
  };
  req.onerror = () => {
    show($("upmsg"), "the upload did not complete", "err");
    $("upload").disabled = false;
  };
  req.send(file);
}
$("upload").onclick = () => doUpload(false);
$("uploadforce").onclick = () => doUpload(true);

// --- clearing the browser's copy of the game files -------------------------
// The client keeps every pak it has downloaded in an IndexedDB store named
// "/base", and re-downloads one whose checksum no longer matches what the
// server offers. If that replacement cannot complete the client retries from
// the top, and the loading loop is far too fast to clear storage by hand -
// so the way out has to live on a page that is not the game.
//
// The game page holds the store open, so deleting it only works from the
// console in its own tab, not from the in-game overlay.
const GAME_STORE = "/base";

$("resetstore").onclick = async () => {
  if (window.parent !== window) {
    show($("namemsg"), "Open the console in its own tab to do this - the game " +
      "page has the files open. Visit /admin/ directly.", "err");
    return;
  }
  const ok = await askConfirm(
    "Clear this browser's copy of the game files? They download again next time " +
    "you play. Close any tab running the game first, or they cannot be cleared.",
    "Clear");
  if (!ok) return;
  show($("namemsg"), "Clearing...", "work");
  const outcome = await new Promise(resolve => {
    let settled = false;
    const done = value => { if (!settled) { settled = true; resolve(value); } };
    const request = indexedDB.deleteDatabase(GAME_STORE);
    request.onsuccess = () => done("cleared");
    request.onerror = () => done("failed: " + (request.error && request.error.message));
    request.onblocked = () => done("blocked");
    setTimeout(() => done("blocked"), 8000);
  });
  if (outcome === "cleared") {
    show($("namemsg"), "Cleared. Open the game again and it will download what " +
      "this server offers.", "ok");
  } else if (outcome === "blocked") {
    show($("namemsg"), "Something still has the game files open. Close every tab " +
      "running the game, then try again.", "err");
  } else {
    show($("namemsg"), outcome, "err");
  }
};

// --- stats -----------------------------------------------------------------
function renderStatRows(host, rows) {
  if (!rows.length) { $(host).innerHTML = '<p class="empty">Nothing recorded yet.</p>'; return; }
  $(host).innerHTML =
    "<table><thead><tr><th>#</th><th>Name</th><th>Kills</th><th>Deaths</th>" +
    "<th>K/D</th><th>Best</th><th>Matches</th></tr></thead><tbody>" +
    rows.map((r, i) =>
      "<tr><td>" + (i + 1) + "</td><td>" + esc(r.name) + "</td><td>" + r.kills +
      "</td><td>" + r.deaths + "</td><td>" + r.ratio + "</td><td>" + r.best +
      "</td><td>" + r.matches + "</td></tr>").join("") + "</tbody></table>";
}

async function loadStats() {
  try {
    const s = await api("api/stats");
    renderStatRows("statsplayers", s.players);
  } catch (e) {
    $("statsplayers").innerHTML = '<p class="empty">' + esc(e.message) + "</p>";
  }
}

// --- log -------------------------------------------------------------------
async function loadAudit() {
  try {
    const entries = (await api("api/audit?limit=200")).entries.slice().reverse();
    $("auditcount").textContent = entries.length ? entries.length + " entries" : "";
    $("audit").innerHTML = entries.length
      ? '<table class="audit">' + entries.map(e =>
          '<tr><td class="t">' + esc(new Date(e.at * 1000).toLocaleString()) + '</td><td class="ip">' + esc(e.actor) +
          '</td><td class="a">' + esc(e.action.replace(/^\/api\//, "")) + "</td><td>" +
          esc(Object.keys(e.detail || {}).length ? JSON.stringify(e.detail) : "") + "</td></tr>").join("") + "</table>"
      : '<p class="hint">Nothing yet.</p>';
  } catch (e) { /* the state poll surfaces auth failures */ }
}

async function loadCrashes() {
  try {
    const { crashes } = await api("api/crashes");
    $("crashcount").textContent = crashes.length ? crashes.length + " kept" : "";
    $("crashes").innerHTML = crashes.length
      ? crashes.map(c => "<details><summary>" + esc(new Date(c.at * 1000).toLocaleString()) + " - " + esc(c.reason) +
          " on " + esc(c.map || "?") + " with " + (c.bots == null ? "?" : c.bots) + " bots</summary><pre>" +
          esc(c.tail.join("\n")) + "</pre></details>").join("")
      : '<p class="hint">None recorded.</p>';
  } catch (e) { /* the state poll surfaces auth failures */ }
}

async function loadLog() {
  try {
    const el = $("log");
    // Follow new output unless the reader has scrolled up to look at something.
    const pinned = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    el.textContent = (await api("api/log")).log || "(empty)";
    if (pinned) el.scrollTop = el.scrollHeight;
  } catch (e) { /* the state poll surfaces auth failures */ }
}

// --- backup ----------------------------------------------------------------
async function loadBackups() {
  try {
    const { backups, keep, every_hours } = await api("api/backups");
    $("backuplist").innerHTML = (backups.length
      ? "Automatic: " + backups.map(b => '<a href="api/backups/' + encodeURIComponent(b.name) + '">' + esc(b.name) + "</a>").join(", ")
      : "No automatic backup yet") + " - written every " + every_hours + " h, " + keep + " kept in the state volume.";
  } catch (e) { /* the state poll surfaces auth failures */ }
}
$("backupnow").onclick = async () => {
  try {
    const r = await api("api/backups", {});
    show($("backupmsg"), "Wrote " + r.name + ".", "ok");
    await loadBackups();
  } catch (e) { show($("backupmsg"), e.message, "err"); }
};

$("exportbtn").onclick = () => { location.href = "api/export"; };
$("importbtn").onclick = () => $("importfile").click();
$("importfile").onchange = async () => {
  const file = $("importfile").files[0];
  if (!file) return;
  show($("backupmsg"), "Importing " + file.name + "...", "work");
  try {
    const r = await fetch("api/import", { method: "POST", body: await file.text(),
      credentials: "same-origin", headers: { "Content-Type": "application/json" } });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    const a = data.applied;
    show($("backupmsg"), "Imported " + (a.settings || 0) + " settings, " + (a.rotation || 0) +
      " rotation maps, " + (a.players || 0) + " players" + (a.credentials ? " and the password" : "") +
      ". " + data.note + ".", "ok");
    rotation = [];   // refilled from the next state poll
    if (data.reauth) {
      isAdmin = false;
      view("signin");
      show($("signinmsg"), "The password came from the backup. Sign in with it.", "ok");
    }
  } catch (e) { show($("backupmsg"), e.message, "err"); }
  $("importfile").value = "";
};

// --- shared refresh --------------------------------------------------------
const TYPE_KEYS = { "0": "ffa", "1": "tourney", "2": "single", "3": "team", "4": "ctf" };

async function refresh() {
  try {
    state = await api("api/state");
    const gt = settingsSpec && settingsSpec.g_gametype ? settingsSpec.g_gametype.value : null;
    state.gametypeKey = TYPE_KEYS[gt] || "";
    $("curmap").textContent = state.map || "unknown";
    const meta = state.arenas[state.map] || {};
    $("curmeta").textContent = [meta.longname, (meta.types || []).join(" ")]
      .filter(Boolean).join(" - ");
    setShot($("curshot"), $("curnoshot"), state.map);
    $("status").innerHTML = "<b>" + esc(state.map || "?") + "</b> &middot; " +
      state.players.length + " connected &middot; " + state.maps.length + " maps &middot; " +
      mb(state.usage.bytes);
    if (activeTab === "server") fillMapPicker();
    if (activeTab === "players") { renderPlayers(state.players); fillBots(state.bots); fillBotRoom(state.bot_room); }
    if (!rotation.length) rotation = state.rotation.slice();
    if (activeTab === "maps") { renderRotation(); renderMaps(); }
  } catch (e) {
    if (/authentication required/i.test(e.message)) {
      isAdmin = false;
      view("main");
      tab("chat");
      return;
    }
    $("curmap").textContent = "unreachable";
    show($("mapmsg"), e.message, "err");
  }
  if (!$("tab-log").hidden) loadLog();
}

// --- close the surrounding modal -------------------------------------------
// Keystrokes inside an iframe never reach the parent window, so the console has
// to forward the close key itself or the overlay could only be closed by
// clicking away from it.
window.addEventListener("keydown", e => {
  const editable = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target || {}).tagName || "");
  const plain = e.code === "Backquote" && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey;
  // ` types a backtick, so leave it alone while a field has focus; Esc always closes.
  if (e.key === "Escape" && confirmOpen()) {
    e.preventDefault();
    e.stopImmediatePropagation();
    settleConfirm(false);
    return;
  }
  if (confirmOpen()) return;   // keep the console open behind the confirmation
  if (e.key === "Escape" || (plain && !editable)) {
    e.preventDefault();
    if (window.parent !== window) {
      window.parent.postMessage({ type: "qadmin-close" }, window.location.origin);
    }
  }
}, true);

start();
