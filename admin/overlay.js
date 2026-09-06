/* In-game admin console, opened with Shift+` as a modal.
 *
 * Plain ` is left alone so Quake's own console still works. The modal loads the
 * console page in an iframe, so there is one console implementation rather than
 * two. If no console is mounted at /admin/ the key does nothing at all.
 */
(function () {
  "use strict";

  var modal = null;
  var available = false;

  function build() {
    var style = document.createElement("style");
    style.textContent = [
      "#qadmin-backdrop{position:fixed;inset:0;z-index:2147483000;display:flex;",
      "align-items:center;justify-content:center;padding:2rem;",
      "background:rgba(6,4,3,.72);backdrop-filter:blur(3px)}",
      "#qadmin-backdrop[hidden]{display:none}",
      "#qadmin-modal{display:flex;flex-direction:column;width:min(1040px,100%);",
      "height:min(760px,100%);background:#14100e;border:1px solid #3a302b;",
      "border-radius:10px;box-shadow:0 24px 70px rgba(0,0,0,.6);overflow:hidden}",
      ".qadmin-bar{display:flex;align-items:center;gap:1rem;padding:.6rem .9rem;",
      "background:#1e1917;border-bottom:1px solid #3a302b;color:#e8ded6;flex:none;",
      "font:600 12px/1.4 ui-monospace,Menlo,Consolas,monospace;letter-spacing:.12em;",
      "text-transform:uppercase}",
      ".qadmin-hint{margin-left:auto;color:#9b8b80;font-weight:400;text-transform:none;",
      "letter-spacing:0;font-size:12px}",
      ".qadmin-close{background:transparent;border:1px solid #3a302b;color:#e8ded6;",
      "border-radius:4px;cursor:pointer;font-size:1.05rem;line-height:1;padding:.1rem .5rem}",
      ".qadmin-close:hover{background:#2a2320}",
      ".qadmin-frame{flex:1;width:100%;border:0;background:#14100e}"
    ].join("");
    document.head.appendChild(style);

    var backdrop = document.createElement("div");
    backdrop.id = "qadmin-backdrop";
    backdrop.setAttribute("hidden", "");
    backdrop.innerHTML =
      '<div id="qadmin-modal" role="dialog" aria-modal="true" aria-label="QuakeJS-Docker console">' +
        '<div class="qadmin-bar">' +
          '<span>QuakeJS-Docker console</span>' +
          '<span class="qadmin-hint">` or Esc to close</span>' +
          '<button type="button" class="qadmin-close" aria-label="Close">&times;</button>' +
        '</div>' +
        '<iframe class="qadmin-frame" title="QuakeJS-Docker console"></iframe>' +
      '</div>';
    document.body.appendChild(backdrop);

    backdrop.querySelector(".qadmin-close").addEventListener("click", close);
    backdrop.addEventListener("mousedown", function (event) {
      if (event.target === backdrop) close();  // click outside the dialog
    });
    return backdrop;
  }

  function tellConsole(visible) {
    if (!modal) return;
    var frame = modal.querySelector(".qadmin-frame");
    if (!frame || !frame.getAttribute("src") || !frame.contentWindow) return;
    try {
      frame.contentWindow.postMessage(
        { type: "qadmin-visible", visible: visible }, window.location.origin);
    } catch (e) { /* not loaded yet; it starts polling on load anyway */ }
  }

  function open() {
    if (!available) return;
    if (!modal) modal = build();
    var frame = modal.querySelector(".qadmin-frame");
    // Load on first open so the auth prompt only appears when asked for.
    if (!frame.getAttribute("src")) frame.setAttribute("src", "/admin/");
    modal.removeAttribute("hidden");
    if (document.exitPointerLock) document.exitPointerLock();
    frame.focus();
    tellConsole(true);
  }

  function close() {
    // Tell the console to stop polling: it keeps running while hidden, and its
    // work competes with the game for the tab's main thread.
    tellConsole(false);
    if (modal) modal.setAttribute("hidden", "");
    var canvas = document.querySelector("canvas");
    if (canvas) canvas.focus();
  }

  function isOpen() {
    return !!modal && !modal.hasAttribute("hidden");
  }

  // Capture phase: the game canvas swallows keydown, so claim the key first.
  // Plain ` opens this console. Shift+` is deliberately left alone: the client
  // maps keyCode 192 to the Quake console key regardless of shift, so letting it
  // through is what opens the game's own console.
  window.addEventListener("keydown", function (event) {
    var plain = event.code === "Backquote" && !event.shiftKey
      && !event.ctrlKey && !event.metaKey && !event.altKey;
    if (plain) {
      if (!available) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      isOpen() ? close() : open();
    } else if (event.key === "Escape" && isOpen()) {
      event.preventDefault();
      event.stopImmediatePropagation();
      close();
    }
  }, true);

  // The console runs in an iframe, whose keystrokes never reach this window, so
  // it forwards the close chord as a message instead.
  window.addEventListener("message", function (event) {
    if (event.origin !== window.location.origin) return;
    if (event.data && event.data.type === "qadmin-close") close();
  });

  // Unauthenticated probe: tells us whether a console is mounted here at all,
  // without provoking a browser auth dialog on page load. Retried a couple of
  // times so a hiccup during startup does not leave the key permanently dead.
  function probe(attempt) {
    fetch("/admin/api/ping", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        available = !!(d && d.console);
        if (!available && attempt < 3) setTimeout(function () { probe(attempt + 1); }, 2000);
      })
      .catch(function () {
        available = false;
        if (attempt < 3) setTimeout(function () { probe(attempt + 1); }, 2000);
      });
  }
  probe(0);
})();
