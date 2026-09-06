// The front page reads the console's public endpoints: who is on, which map,
// the leaderboard, and the current map's picture. Nothing here needs a
// session, and when the console is disabled (ADMIN_PASSWORD empty, so /admin/
// answers 503) the live panels simply hide and the Join button stands alone.
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var POLL = 10000;
  var lastMap = null;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function get(path) {
    return fetch(path, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    });
  }

  function showRoster(pub) {
    if (pub.hostname) {
      $("hostname").textContent = pub.hostname;
      $("hostname").hidden = false;
    }
    $("map").textContent = pub.map || "no map loaded";
    if (pub.map && pub.map !== lastMap) {
      // The picture is served for the current map only, so the URL is the
      // same; the query string just defeats the cache when the map changes.
      lastMap = pub.map;
      var img = $("shot");
      img.hidden = true;
      img.onload = function () { img.hidden = false; };
      img.onerror = function () { img.hidden = true; };
      img.src = "/admin/api/public/levelshot?map=" + encodeURIComponent(pub.map);
    }
    var players = pub.players || [];
    var humans = players.filter(function (p) { return !p.bot; }).length;
    $("count").textContent = players.length
      ? humans + " player" + (humans === 1 ? "" : "s") + (players.length > humans ? ", " + (players.length - humans) + " bots" : "")
      : "";
    $("players").innerHTML = players.length
      ? players.map(function (p) {
          return "<li>" + esc(p.name) + (p.bot ? ' <span class="tag bot">bot</span>' : "") + "</li>";
        }).join("")
      : '<li class="muted">Nobody yet - be the first.</li>';
  }

  function showBoard(stats) {
    var rows = (stats.players || []).slice(0, 10);
    $("board").hidden = !rows.length;
    $("noboard").hidden = !!rows.length;
    $("board").querySelector("tbody").innerHTML = rows.map(function (r) {
      return "<tr><td>" + esc(r.name) + '</td><td class="n">' + r.kills + '</td><td class="n">' + r.deaths +
        '</td><td class="n">' + Number(r.ratio).toFixed(2) + '</td><td class="n">' + r.best + "</td></tr>";
    }).join("");
  }

  function refresh() {
    get("/admin/api/public").then(function (pub) {
      $("live").hidden = false;
      showRoster(pub);
      return get("/admin/api/stats").then(showBoard, function () {});
    }).catch(function () {
      // No console: nothing live to show, but the game is still there.
      $("live").hidden = true;
      $("foot").textContent = "";
    });
  }

  // The name to play under. Kept where the console keeps it, so a name
  // given on either page is the one the game launches with.
  var KEY = "qadmin.name";
  var nameField = $("name");
  try { nameField.value = localStorage.getItem(KEY) || ""; } catch (e) { /* private browsing */ }
  nameField.addEventListener("input", function () {
    try { localStorage.setItem(KEY, nameField.value.trim()); } catch (e) { /* private browsing */ }
  });

  refresh();
  setInterval(refresh, POLL);
})();
