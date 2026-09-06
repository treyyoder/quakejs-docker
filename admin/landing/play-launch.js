// How the game is launched. The page builds its own arguments (the content
// host, the connect address); the query string may carry more - the console's
// Join and "Use name in game" pass "set" commands that way; and the name saved
// by the front page or the console is added too. That name is in this browser's
// storage, which both pages share, so a name given anywhere applies everywhere.
// "connect" goes last, so the client introduces itself with the right name
// rather than renaming itself a moment after arriving.
(function () {
  "use strict";
  var KEY = "qadmin.name";

  function savedName() {
    var name = "";
    try { name = localStorage.getItem(KEY) || ""; } catch (e) { /* private browsing */ }
    // Printable, and without anything that could reach the command buffer as
    // more than a name. The game clips names at 31 characters anyway.
    return name.replace(/[^\x20-\x7e]/g, "").replace(/[+;"$\\]/g, "").trim().slice(0, 31);
  }

  // The name given in the query as "+set name X ...", or "".
  function queryName(extra) {
    var at = extra.indexOf("name");
    if (at < 1 || extra[at - 1] !== "+set") return "";
    var end = at + 1;
    while (end < extra.length && extra[end].charAt(0) !== "+") end++;
    return extra.slice(at + 1, end).join(" ");
  }

  window.quakejsLaunch = function (args, extra) {
    args = args.slice();
    extra = extra.slice();
    var name = queryName(extra) || savedName();
    if (name) {
      // Two ways, for two generations of engine. "+set name" is the command
      // line's own, and newer ioquake3 re-applies it after its config files;
      // the build in use here lets q3config.cfg - which remembers
      // UnnamedPlayer - win. A startup command, unlike a startup variable,
      // runs after the configs in every build, so "vstr" of a cvar holding
      // "set name ..." is what actually lands.
      if (!queryName(extra)) extra = ["+set", "name", name].concat(extra);
      extra = extra.concat(["+set", "launchname", "set name " + name, "+vstr", "launchname"]);
    }
    var at = args.indexOf("+connect");
    var connect = at < 0 ? [] : args.splice(at, 2);
    return args.concat(extra, connect);
  };
})();
