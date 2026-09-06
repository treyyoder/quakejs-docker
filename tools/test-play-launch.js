#!/usr/bin/env node
// The game-launch arguments: the saved name is added, sanitised, and never
// overrides a name given in the query; "connect" always goes last.
//
//     node tools/test-play-launch.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const source = fs.readFileSync(path.join(__dirname, "..", "admin", "landing", "play-launch.js"), "utf8");
const store = {};
global.window = {};
global.localStorage = { getItem: key => (key in store ? store[key] : null) };
new Function(source)();
const launch = global.window.quakejsLaunch;

const page = ["+set", "com_hunkMegs", "256", "+set", "fs_cdn", "h:8080", "+connect", "h:8080"];

// No saved name, no query: the page's arguments, connect last, untouched.
assert.deepStrictEqual(launch(page, []), page);

// A saved name is set both ways - the startup variable, and the startup
// command that runs after the config files - and connect comes last.
store["qadmin.name"] = "Trey";
assert.deepStrictEqual(launch(page, []),
  ["+set", "com_hunkMegs", "256", "+set", "fs_cdn", "h:8080", "+set", "name", "Trey",
   "+set", "launchname", "set name Trey", "+vstr", "launchname", "+connect", "h:8080"]);

// Query arguments ride along, and a name given there wins over the saved one.
assert.deepStrictEqual(launch(page, ["+set", "r_picmip", "3"]).slice(6, 11),
  ["+set", "name", "Trey", "+set", "r_picmip"]);
const alex = launch(page, ["+set", "name", "Alex", "+set", "r_picmip", "3"]);
assert.deepStrictEqual(alex.filter((a, i, all) => all[i - 1] === "name"), ["Alex"]);
assert.strictEqual(alex[alex.indexOf("launchname") + 1], "set name Alex");
assert.deepStrictEqual(alex.slice(-2), ["+connect", "h:8080"]);

// The saved name cannot carry a command, a quote, or non-printables, and is clipped.
store["qadmin.name"] = ' Tréy; +quit "x" $y\\z ' + "a".repeat(40);
const withJunk = launch(page, []);
assert.strictEqual(withJunk[withJunk.indexOf("name") + 1], ("Try quit x yz " + "a".repeat(40)).slice(0, 31));
assert.strictEqual(withJunk.indexOf("+quit"), -1);

// An empty name after cleaning adds nothing.
store["qadmin.name"] = "éé";
assert.deepStrictEqual(launch(page, []), page);

// The inputs are not modified.
store["qadmin.name"] = "Trey";
const original = page.slice();
launch(page, ["+set", "x", "1"]);
assert.deepStrictEqual(page, original);

console.log("play-launch: 7 checks passed");
