#!/usr/bin/env python3
"""Unit tests for the console's pure parts.

The parsers (status, chat, the game log, EXTRA_PAKS), the settings spec and
its validation, the file follower both tailers share, client attribution
through the proxy, leaderboard pruning, base-pak recognition, and
build-config.py end to end. Nothing here needs a running server.

    python3 tools/test-admin.py     # on the host, against admin/
    python3 /tools/test-admin.py    # inside the image, against /admin

The image build runs these, and the smoke suite runs them again inside the
built image, so a parser regression cannot reach a published image.
"""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
_checkout = HERE.parent / "admin"
ADMIN = pathlib.Path(os.environ.get("ADMIN_DIR") or (_checkout if _checkout.is_dir() else "/admin"))
sys.path.insert(0, str(ADMIN))

from qadmin import auth, chat, config, game, settings, stats, web  # noqa: E402
from qadmin.assets import is_base_pak  # noqa: E402
from qadmin.follow import FINGERPRINT_BYTES, Follower  # noqa: E402

BS = chr(92)


def load_script(name):
    """Import one of the hyphenated scripts beside the package."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").replace(".py", ""),
                                                  ADMIN / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------ game: status
class StatusParsing(unittest.TestCase):
    HEADER = ("map: q3dm1\n"
              "num score ping name            lastmsg address               qport rate\n"
              "--- ----- ---- --------------- ------- --------------------- ----- -----\n")

    def rows(self, *rows):
        return game.parse_status(self.HEADER + "\n".join(rows) + "\n")

    def test_bot_and_map(self):
        current, players = self.rows(
            "  0     5    0 Sarge^7               0 bot                       0     0")
        self.assertEqual(current, "q3dm1")
        self.assertEqual(players, [{"num": 0, "score": 5, "ping": 0, "name": "Sarge",
                                    "bot": True, "address": None}])

    def test_name_with_a_run_of_spaces_and_a_long_idle(self):
        # A padded name column and a seven-digit lastmsg used to break the row.
        _, players = self.rows(
            "  1    12   48 Player  One      1000000 203.0.113.9:27960     12345 25000")
        self.assertEqual(players[0]["name"], "Player  One")
        self.assertEqual(players[0]["address"], "203.0.113.9")
        self.assertEqual((players[0]["score"], players[0]["ping"]), (12, 48))
        self.assertFalse(players[0]["bot"])

    def test_name_ending_in_digits(self):
        _, players = self.rows(
            "  2     3   50 Agent 7                 5 198.51.100.4:27961    10 8000")
        self.assertEqual(players[0]["name"], "Agent 7")
        self.assertEqual(players[0]["address"], "198.51.100.4")

    def test_connecting_client_is_skipped_and_colours_stripped(self):
        _, players = self.rows(
            "  3     0 CNCT                       0 192.0.2.1:27960       0 0",
            "  4     1   30 ^1Red^7Baron            9 192.0.2.2:27960      11 9000")
        self.assertEqual([p["num"] for p in players], [4])
        self.assertEqual(players[0]["name"], "RedBaron")

    def test_two_overlapping_outputs_list_each_slot_once(self):
        row = "  0     5    0 Sarge^7               0 bot                       0     0"
        later = "  0     9    0 Sarge^7               0 bot                       0     0"
        _, players = self.rows(row, later)
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0]["score"], 9)

    def test_banlist(self):
        text = "IP ban list:\n  203.0.113.9\n  198.51.100.0\n  203.0.113.9\n"
        self.assertEqual(game.parse_banlist(text), ["198.51.100.0", "203.0.113.9"])


# ------------------------------------------------------------------ chat
class ChatParsing(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(chat.parse_chat_line("say: Bob: hello there\n"),
                         ("say", "Bob", None, "hello there"))
        self.assertEqual(chat.parse_chat_line("sayteam: ^1Bob^7: go go"),
                         ("team", "^1Bob^7", None, "go go"))
        self.assertEqual(chat.parse_chat_line("tell: Bob to Alice: psst"),
                         ("tell", "Bob", "Alice", "psst"))
        self.assertIsNone(chat.parse_chat_line("broadcast: print \"Bob entered\""))
        self.assertIsNone(chat.parse_chat_line(""))


class MessageVisibility(unittest.TestCase):
    def setUp(self):
        chat._messages.clear()
        chat._message_seq[0] = 0
        chat.add_message("say", "^1Bob^7", None, "public")
        chat.add_message("sent-pm", "alice", "Bob", "private", token="t1")
        chat.add_message("tell", "Bob", "Carol", "between players")

    def test_sender_sees_public_and_their_own_private(self):
        kinds = [m["kind"] for m in chat.messages_since(0, token="t1")]
        self.assertEqual(kinds, ["say", "sent-pm"])

    def test_stranger_and_nobody_see_public_only(self):
        self.assertEqual([m["kind"] for m in chat.messages_since(0, token="t2")], ["say"])
        self.assertEqual([m["kind"] for m in chat.messages_since(0)], ["say"])

    def test_admin_sees_everything_and_never_the_token(self):
        out = chat.messages_since(0, admin=True)
        self.assertEqual([m["kind"] for m in out], ["say", "sent-pm", "tell"])
        self.assertFalse(any("token" in m for m in out))
        self.assertEqual(out[0]["from"], "Bob")     # colours stripped

    def test_since_is_exclusive(self):
        self.assertEqual([m["seq"] for m in chat.messages_since(2, admin=True)], [3])


class ChatThrottle(unittest.TestCase):
    def setUp(self):
        for throttle in (chat.CHAT_PER_BROWSER, chat.CHAT_PER_ADDRESS, chat.CHAT_GLOBAL,
                         chat.READS_PER_ADDRESS, chat.READS_GLOBAL):
            throttle._hits.clear()

    def test_burst_per_address_and_per_browser(self):
        now = 1000.0
        # Three browsers behind one address: the address runs out first.
        allowed = sum(chat.chat_allowed("b%d" % (i % 3), now, address="203.0.113.9")[0]
                      for i in range(chat.GUEST_BURST * 3 + 5))
        self.assertEqual(allowed, chat.GUEST_BURST * 3)
        self.assertIn("address", chat.chat_allowed("b9", now, address="203.0.113.9")[1])
        self.assertTrue(chat.chat_allowed("b9", now, address="203.0.113.10")[0])

    def test_public_reads_are_bounded_per_address(self):
        now = 1000.0
        allowed = sum(chat.reads_allowed("203.0.113.9", now) for _ in range(200))
        self.assertEqual(allowed, chat.READS_PER_ADDRESS.burst)
        self.assertTrue(chat.reads_allowed("203.0.113.10", now))
        self.assertTrue(chat.reads_allowed("203.0.113.9", now + 11))

    def test_burst_per_browser(self):
        now = 1000.0
        for _ in range(chat.GUEST_BURST):
            self.assertEqual(chat.chat_allowed("browser-a", now)[0], True)
        allowed, why = chat.chat_allowed("browser-a", now)
        self.assertFalse(allowed)
        self.assertIn("too quickly", why)
        # Another browser is unaffected; the window passing frees the first.
        self.assertTrue(chat.chat_allowed("browser-b", now)[0])
        self.assertTrue(chat.chat_allowed("browser-a", now + chat.GUEST_WINDOW + 1)[0])


# --------------------------------------------------------------- game log
def userinfo(slot, name, bot=False):
    pairs = ["n", name, "t", "0", "model", "sarge"]
    if bot:
        pairs += ["skill", "3"]
    return "ClientUserinfoChanged: %d %s" % (slot, BS.join(pairs))


class GameLog(unittest.TestCase):
    def setUp(self):
        stats._stats["players"] = {}
        stats._slots.clear()
        stats._present.clear()
        stats._recent.clear()
        for line in (userinfo(0, "Sarge", bot=True), userinfo(1, "^1Trey^7")):
            stats.handle_game_line(line, live=False)

    def test_bots_are_told_apart_by_skill_and_names_are_exact(self):
        self.assertEqual(stats._slots, {0: {"name": "Sarge", "bot": True},
                                        1: {"name": "Trey", "bot": False}})
        self.assertIs(stats.bot_status("Sarge"), True)
        self.assertIs(stats.bot_status("Trey"), False)
        self.assertIsNone(stats.bot_status("Nobody"))

    def test_kills_deaths_suicides_and_the_world(self):
        stats.handle_game_line("Kill: 0 1 10: Sarge killed Trey by MOD_ROCKET")
        stats.handle_game_line("Kill: 1 1 3: Trey killed Trey by MOD_ROCKET_SPLASH")
        stats.handle_game_line("Kill: 1022 1 19: <world> killed Trey by MOD_FALLING")
        trey, sarge = stats._stats["players"]["Trey"], stats._stats["players"]["Sarge"]
        self.assertEqual((trey["deaths"], trey["suicides"], trey["kills"]), (3, 1, 0))
        self.assertEqual(sarge["kills"], 1)

    def test_score_line_counts_a_match_and_keeps_the_best(self):
        stats.handle_game_line("score: 7  ping: 40  client: 1 Trey")
        stats.handle_game_line("score: 4  ping: 40  client: 1 Trey")
        trey = stats._stats["players"]["Trey"]
        self.assertEqual((trey["matches"], trey["best"]), (2, 7))

    def test_annotate_overlays_exact_names_by_slot(self):
        players = [{"num": 1, "name": "Tre", "bot": False},      # status truncated it
                   {"num": 0, "name": "Sarge", "bot": False},    # status did not know
                   {"num": 5, "name": "Stranger", "bot": False}]  # log has no slot 5
        stats.annotate(players)
        self.assertEqual([p["name"] for p in players], ["Trey", "Sarge", "Stranger"])
        self.assertEqual([p["bot"] for p in players], [False, True, False])

    def test_a_row_born_of_a_kill_still_knows_a_bot_is_a_bot(self):
        # The leaderboard was replaced (an import) with the bots still in: the
        # first kill line recreates their rows, which must not make them players.
        stats._stats["players"] = {}
        stats.handle_game_line("Kill: 1 0 10: Trey killed Sarge by MOD_ROCKET")
        self.assertTrue(stats._stats["players"]["Sarge"]["bot"])
        self.assertFalse(stats._stats["players"]["Trey"]["bot"])
        self.assertGreater(stats._stats["players"]["Trey"]["seen"], 0)   # not pruned at the next save
        self.assertEqual([r["name"] for r in stats.leaderboard()["players"]], ["Trey"])

    def test_the_map_is_remembered_from_initgame(self):
        stats.handle_game_line("InitGame: " + BS + "sv_hostname" + BS + "quakejs" + BS + "mapname"
                               + BS + "q3dm7" + BS + "g_gametype" + BS + "0")
        self.assertEqual(stats.current_map(), "q3dm7")
        stats.handle_game_line("InitGame: " + BS + "mapname" + BS + "pro-q3dm13" + BS + "sv_hostname" + BS + "x")
        self.assertEqual(stats.current_map(), "pro-q3dm13")
        stats.handle_game_line("ShutdownGame:")
        self.assertEqual(stats.current_map(), "pro-q3dm13")   # the last one stands until the next

    def test_disconnect_and_map_change_forget_slots(self):
        stats.handle_game_line("ClientDisconnect: 1")
        self.assertNotIn(1, stats._slots)
        stats.handle_game_line("ShutdownGame:")
        self.assertEqual(stats._slots, {})

    def test_leaderboard_ranks_players_and_hides_bots(self):
        stats.handle_game_line("Kill: 0 1 10: Sarge killed Trey by MOD_ROCKET")
        stats.handle_game_line("Kill: 1 0 10: Trey killed Sarge by MOD_ROCKET")
        stats.handle_game_line("Kill: 1 0 10: Trey killed Sarge by MOD_ROCKET")
        board = stats.leaderboard()["players"]
        self.assertEqual([r["name"] for r in board], ["Trey"])
        self.assertEqual((board[0]["kills"], board[0]["ratio"]), (2, 2.0))

    def test_prune_drops_stale_names_and_caps_by_most_recent(self):
        now = time.time()
        stats._stats["players"] = {"stale": {"kills": 1, "seen": now - 400 * 86400}}
        stats._stats["players"].update(
            {"p%d" % i: {"kills": i, "seen": now - i} for i in range(stats.STATS_MAX_PLAYERS + 50)})
        stats.prune_stats()
        names = stats._stats["players"]
        self.assertNotIn("stale", names)
        self.assertEqual(len(names), stats.STATS_MAX_PLAYERS)
        self.assertIn("p0", names)
        self.assertNotIn("p%d" % (stats.STATS_MAX_PLAYERS + 49), names)


# ------------------------------------------------------------ the follower
class FollowerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.dir.name) / "log"

    def tearDown(self):
        self.dir.cleanup()

    def write(self, text):
        self.path.write_text(text, newline="")   # the bytes as given, on any host

    def test_missing_file_then_lines_then_a_partial_line_held_back(self):
        follower = Follower(self.path)
        self.assertEqual(follower.poll(), [])
        self.write("a\nb\npart")
        self.assertEqual(follower.poll(), ["a", "b"])
        with self.path.open("a", newline="") as handle:
            handle.write("ial\nc\n")
        self.assertEqual(follower.poll(), ["partial", "c"])
        self.assertEqual(follower.poll(), [])

    def test_truncation_reads_from_the_start_again(self):
        follower = Follower(self.path)
        self.write("one\ntwo\nthree\n")
        self.assertEqual(follower.poll(), ["one", "two", "three"])
        self.write("x\n")
        self.assertEqual(follower.poll(), ["x"])
        self.assertTrue(follower.reset)

    def test_a_different_file_of_greater_size_is_a_reset(self):
        follower = Follower(self.path)
        first = "A" * FINGERPRINT_BYTES + "\nold\n"
        self.write(first)
        self.assertEqual(len(follower.poll()), 2)
        self.write("B" * FINGERPRINT_BYTES + "\nnew\nlines\n")
        self.assertEqual(follower.poll(), ["B" * FINGERPRINT_BYTES, "new", "lines"])
        self.assertTrue(follower.reset)

    def test_a_short_file_growing_past_the_fingerprint_is_not_a_reset(self):
        follower = Follower(self.path)
        self.write("short\n")
        self.assertEqual(follower.poll(), ["short"])
        with self.path.open("a", newline="") as handle:
            handle.write("x" * (FINGERPRINT_BYTES * 2) + "\n")
        self.assertEqual(len(follower.poll()), 1)
        self.assertFalse(follower.reset)

    def test_a_raising_handler_costs_one_batch_not_the_thread(self):
        follower = Follower(self.path)
        seen = []

        def on_lines(lines):
            if "bad" in lines:
                raise KeyError("bad line")
            seen.extend(lines)

        self.write("good\nbad\n")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            follower.step(on_lines, "test")
        self.assertIn("KeyError", out.getvalue())
        with self.path.open("a", newline="") as handle:
            handle.write("after\n")
        follower.step(on_lines, "test")
        self.assertEqual(seen, ["after"])


# ------------------------------------------------------- client attribution
class ClientAddress(unittest.TestCase):
    def test_forwarded_address_through_trusted_hops(self):
        trusted = auth.trusted_proxies("172.30.0.0/16")
        cases = [
            ("127.0.0.1", "203.0.113.9", "203.0.113.9"),
            ("127.0.0.1", "9.9.9.9, 203.0.113.9", "203.0.113.9"),
            ("127.0.0.1", "203.0.113.9, 172.30.0.5", "203.0.113.9"),
            ("127.0.0.1", None, "127.0.0.1"),
            ("10.0.0.9", "1.2.3.4", "10.0.0.9"),          # not from our proxy: header ignored
            ("::ffff:127.0.0.1", "203.0.113.9", "203.0.113.9"),
            ("127.0.0.1", "garbage, 203.0.113.9", "203.0.113.9"),
            ("127.0.0.1", "203.0.113.9, garbage", "127.0.0.1"),
        ]
        for peer, forwarded, want in cases:
            with self.subTest(peer=peer, forwarded=forwarded):
                self.assertEqual(auth.client_ip(peer, forwarded, trusted), want)

    def test_unreadable_trusted_entries_are_skipped(self):
        with contextlib.redirect_stdout(io.StringIO()):
            networks = auth.trusted_proxies("10.0.0.5, not-a-network 172.18.0.0/16")
        self.assertEqual([str(n) for n in networks], ["10.0.0.5/32", "172.18.0.0/16"])

    def test_requests_time_out(self):
        self.assertEqual(web.Handler.timeout, config.REQUEST_TIMEOUT)
        self.assertGreater(config.REQUEST_TIMEOUT, 0)


# ---------------------------------------------------------------- settings
class SettingsSpec(unittest.TestCase):
    def test_numbers_are_ranged(self):
        self.assertEqual(settings.coerce_setting("timelimit", "20"), 20)
        with self.assertRaises(ValueError):
            settings.coerce_setting("timelimit", "999")
        with self.assertRaises(ValueError):
            settings.coerce_setting("timelimit", "abc")
        with self.assertRaises(ValueError):
            settings.coerce_setting("no_such_cvar", "1")

    def test_text_loses_what_the_command_parser_would_eat(self):
        self.assertEqual(settings.coerce_setting("sv_hostname", 'My "Server"; $x' + BS + 'y'),
                         "My Server xy")
        self.assertEqual(len(settings.coerce_setting("sv_hostname", "x" * 200)), 63)
        self.assertEqual(settings.clean_text("tab\tand\nnewline", 40), "tabandnewline")

    def test_secrets_are_described_as_present_never_as_values(self):
        spec = settings.describe({"sv_privatePassword": "hunter2", "timelimit": "20"})
        self.assertEqual((spec["sv_privatePassword"]["value"], spec["sv_privatePassword"]["present"]),
                         ("", True))
        self.assertEqual(spec["timelimit"]["value"], "20")
        self.assertFalse(spec["g_motd"].get("present", False))

    def test_saved_view_blanks_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = config.SETTINGS_FILE
            config.SETTINGS_FILE = pathlib.Path(tmp) / "settings.json"
            try:
                config.SETTINGS_FILE.write_text(json.dumps({"sv_privatePassword": "h", "timelimit": 5}))
                self.assertEqual(settings.saved_view(), {"sv_privatePassword": "", "timelimit": 5})
            finally:
                config.SETTINGS_FILE = original


class BuildConfig(unittest.TestCase):
    """build-config.py folds every console setting into server.cfg, validated
    by the same spec the console uses - the drift that lost g_allowVote once
    cannot recur, because there is no second list to fall behind."""

    def test_every_setting_is_folded_in_and_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            (tmp / "base.cfg").write_text(
                'set d1 "map q3dm1 ; set nextmap vstr d2"\n'
                'set d2 "map q3dm2 ; set nextmap vstr d1"\n'
                'vstr d1\n')
            values = {}
            for name, spec in settings.SETTINGS.items():
                values[name] = spec["min"] if spec["kind"] == "int" else "Name x"
            values["g_motd"] = 'hi "there"; $x'
            values["timelimit"] = 9999           # out of range: must be dropped
            values["bogus"] = 1                  # not a console setting: must be dropped
            (tmp / "settings.json").write_text(json.dumps(values))
            (tmp / "rotation.json").write_text(json.dumps(["q3tourney2", "bad name", "q3dm7"]))
            result = subprocess.run(
                [sys.executable, str(ADMIN / "build-config.py"),
                 str(tmp / "base.cfg"), str(tmp), str(tmp / "out.cfg")],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            out = (tmp / "out.cfg").read_text()
        for name, spec in settings.SETTINGS.items():
            if name in ("g_motd", "timelimit"):
                continue
            expected = 'seta %s "%s"' % (name, spec["min"] if spec["kind"] == "int" else "Name x")
            self.assertIn(expected, out, name)
        self.assertIn('seta g_motd "hi there x"', out)
        self.assertNotIn("timelimit", out)
        self.assertNotIn("bogus", out)
        self.assertIn('set d1 "map q3tourney2 ; set nextmap vstr d2"', out)
        self.assertIn('set d2 "map q3dm7 ; set nextmap vstr d1"', out)
        self.assertNotIn("q3dm2", out)          # the shipped chain was replaced
        self.assertTrue(out.rstrip().endswith("vstr d1"))
        self.assertEqual(out.count("vstr d1\n"), 1)


# ------------------------------------------------------------------ assets
class BasePaks(unittest.TestCase):
    def test_the_games_own_paks_are_recognised_with_or_without_a_checksum(self):
        for name in ("pak0.pk3", "63455183-pak0.pk3", "12-pak8.pk3", "PAK1.PK3"):
            self.assertTrue(is_base_pak(name), name)
        for name in ("955937415-trespass.pk3", "pak0extra.pk3", "rustgrad.pk3", "pak.pk3"):
            self.assertFalse(is_base_pak(name), name)


# -------------------------------------------------------------- EXTRA_PAKS
class ExtraPaks(unittest.TestCase):
    def test_entries(self):
        paks = load_script("fetch-paks.py")
        digest = "a" * 64
        with contextlib.redirect_stdout(io.StringIO()) as out:
            parsed = paks.parse(
                "pak0.pk3=https://files.example/q3/slim.pk3#sha256=%s, "
                "https://files.example/q3/pak1.pk3 "
                "https://files.example/get?file=pak2.pk3&x=1 "
                "pak3.pk3=https://files.example/three#notahash" % digest)
        self.assertEqual(parsed, [
            ("pak0.pk3", "https://files.example/q3/slim.pk3", digest),
            ("pak1.pk3", "https://files.example/q3/pak1.pk3", None),
            ("get", "https://files.example/get?file=pak2.pk3&x=1", None),
            ("pak3.pk3", "https://files.example/three", None),
        ])
        self.assertIn("ignoring unreadable checksum", out.getvalue())
        self.assertEqual(paks.parse("  "), [])


# ----------------------------------------------------------- the low items
class WebhookMentions(unittest.TestCase):
    """S9: a player's name never becomes a mention. "@everyone" is a legal
    Quake name and the webhook posts as the server."""

    def test_discord_disallows_every_mention(self):
        payload = stats.webhook_payload("https://discord.com/api/webhooks/1/x",
                                        "@everyone is playing on q3")
        self.assertEqual(payload, {"content": "@everyone is playing on q3",
                                   "allowed_mentions": {"parse": []}})

    def test_slack_markup_is_escaped_to_text(self):
        payload = stats.webhook_payload("https://hooks.slack.com/services/x",
                                        "<!everyone> & <@U1> is playing")
        self.assertEqual(payload, {"text": "&lt;!everyone&gt; &amp; &lt;@U1&gt; is playing"})


class DownloadCap(unittest.TestCase):
    """S10: EXTRA_PAKS cannot fill the volume."""

    def test_copy_stops_past_the_limit_and_keeps_what_fits(self):
        paks = load_script("fetch-paks.py")
        dest = io.BytesIO()
        self.assertEqual(paks.copy_capped(io.BytesIO(b"x" * 100), dest, limit=100), 100)
        self.assertEqual(len(dest.getvalue()), 100)
        with self.assertRaises(ValueError) as caught:
            paks.copy_capped(io.BytesIO(b"x" * 101), io.BytesIO(), limit=100)
        self.assertIn("limit", str(caught.exception))
        self.assertGreater(paks.MAX_BYTES, 600 * 1024 * 1024)   # room for retail pak0


class InnerPakBound(unittest.TestCase):
    """S11: a zip that says it holds a pk3 which inflates past the limit is
    refused before a byte of it is inflated."""

    def zip_with(self, name, payload):
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(name, payload)
        return buffer.getvalue()

    def test_a_small_inner_pk3_passes_and_a_large_one_is_refused(self):
        from qadmin import assets
        inner = self.zip_with("maps/x.bsp", b"bsp")
        outer = self.zip_with("maps-pack/x.pk3", inner)
        self.assertEqual(assets.pk3s_in(outer, "pack.zip", limit=len(inner) + 10),
                         [("x.pk3", inner)])
        # 1 MiB of zeros deflates to a few hundred bytes; the limit is checked
        # against what it would inflate to.
        big = self.zip_with("maps-pack/bomb.pk3", b"\0" * (1 << 20))
        self.assertLess(len(big), 1 << 20)
        with self.assertRaises(ValueError) as caught:
            assets.pk3s_in(big, "pack.zip", limit=1 << 19)
        self.assertIn("would unpack", str(caught.exception))


# ------------------------------------------------------- the enhancements
class StatePaths:
    """Point every state file at a temporary directory for one test."""

    def __enter__(self):
        from qadmin import audit, backup, bans, crashes
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.saved = (config.STATE, config.SETTINGS_FILE, config.ROTATION_FILE,
                      config.STATS_FILE, audit.AUDIT_FILE, bans.BANS_FILE,
                      crashes.CRASH_DIR, backup.BACKUP_DIR)
        config.STATE = root / "admin.json"
        config.SETTINGS_FILE = root / "settings.json"
        config.ROTATION_FILE = root / "rotation.json"
        config.STATS_FILE = root / "stats.json"
        audit.AUDIT_FILE = root / "audit.jsonl"
        bans.BANS_FILE = root / "bans.json"
        crashes.CRASH_DIR = root / "crashes"
        backup.BACKUP_DIR = root / "backups"
        return root

    def __exit__(self, *exc):
        from qadmin import audit, backup, bans, crashes
        (config.STATE, config.SETTINGS_FILE, config.ROTATION_FILE,
         config.STATS_FILE, audit.AUDIT_FILE, bans.BANS_FILE,
         crashes.CRASH_DIR, backup.BACKUP_DIR) = self.saved
        self.tmp.cleanup()


class TempBans(unittest.TestCase):
    def test_record_expire_and_restore_after_a_restart(self):
        from qadmin import bans
        with StatePaths(), contextlib.redirect_stdout(io.StringIO()):
            sent = []
            bans.record("203.0.113.9", "spawn camping", hours=1, now=1000)
            bans.record("203.0.113.10", "", hours=None, now=1000)
            # In step with the game: nothing to send.
            rows = bans.sync(listed=["203.0.113.9", "203.0.113.10"], now=1500, send=sent.append)
            self.assertEqual(sent, [])
            self.assertEqual([r["ip"] for r in rows], ["203.0.113.9", "203.0.113.10"])
            self.assertEqual((rows[0]["reason"], rows[0]["remaining"], rows[0]["enforced"]),
                             ("spawn camping", 3600 - 500, True))
            self.assertIsNone(rows[1]["expires"])
            # The game server restarted with an empty list: both put back.
            sent.clear()
            bans.sync(listed=[], now=1600, send=sent.append)
            self.assertEqual(sorted(sent), ["addip 203.0.113.10", "addip 203.0.113.9"])
            # Time is up for the first: lifted and forgotten; the other stays.
            sent.clear()
            rows = bans.sync(listed=["203.0.113.9", "203.0.113.10"], now=1000 + 3601, send=sent.append)
            self.assertEqual(sent, ["removeip 203.0.113.9"])
            self.assertEqual([r["ip"] for r in rows], ["203.0.113.10"])
            self.assertEqual(list(bans.load()), ["203.0.113.10"])
            # A ban the game has that nobody noted is still shown, without a note.
            rows = bans.sync(listed=["198.51.100.1", "203.0.113.10"], now=5000, send=sent.append)
            self.assertEqual([(r["ip"], r["reason"]) for r in rows],
                             [("198.51.100.1", ""), ("203.0.113.10", "")])
            bans.forget("203.0.113.10")
            self.assertEqual(bans.load(), {})


class CrashNotes(unittest.TestCase):
    def test_crash_lines_are_recognised_and_kept_twenty_deep(self):
        from qadmin import crashes
        self.assertEqual(crashes.is_crash("----- Server Shutdown (Server crashed: G_Alloc: failed on allocation of 9088 bytes"),
                         "G_Alloc: failed on allocation of 9088 bytes")
        self.assertIsNone(crashes.is_crash("ShutdownGame:"))
        with StatePaths(), contextlib.redirect_stdout(io.StringIO()):
            for i in range(crashes.KEEP + 3):
                crashes.record("G_Alloc: failed", ["line %d" % j for j in range(300)],
                               "q3dm1", 27, now=1_700_000_000 + i)
            notes = crashes.recent()
            self.assertEqual(len(notes), crashes.KEEP)
            self.assertEqual(notes[0]["at"], 1_700_000_000 + crashes.KEEP + 2)   # newest first
            self.assertEqual((notes[0]["map"], notes[0]["bots"], len(notes[0]["tail"])),
                             ("q3dm1", 27, crashes.TAIL))
            self.assertEqual(notes[0]["tail"][-1], "line 299")


class ScheduledBackups(unittest.TestCase):
    def test_a_backup_a_day_seven_kept(self):
        from qadmin import backup
        with StatePaths(), contextlib.redirect_stdout(io.StringIO()):
            settings.save_rotation(["q3dm7"])
            self.assertTrue(backup.backup_due(now=0))
            for i in range(backup.BACKUP_KEEP + 2):
                backup.write_backup(now=1_700_000_000 + i * 86400)
            names = [b["name"] for b in backup.list_backups()]
            self.assertEqual(len(names), backup.BACKUP_KEEP)
            self.assertEqual(names, sorted(names, reverse=True))
            self.assertTrue(all(backup.BACKUP_NAME_RE.match(n) for n in names))
            newest = backup.BACKUP_DIR / names[0]
            self.assertEqual(json.loads(newest.read_text())["rotation"], ["q3dm7"])
            # Due once a day, judged by the newest file's age.
            self.assertFalse(backup.backup_due(now=newest.stat().st_mtime + 3600))
            self.assertTrue(backup.backup_due(now=newest.stat().st_mtime + 86400))


class MatchPresets(unittest.TestCase):
    def test_every_preset_value_passes_the_spec(self):
        for key, preset in settings.PRESETS.items():
            self.assertTrue(preset["label"] and preset["blurb"], key)
            self.assertIn("g_gametype", preset["values"], key)
            for name, value in preset["values"].items():
                self.assertEqual(settings.coerce_setting(name, value), value, (key, name))


class AuditTrail(unittest.TestCase):
    def test_details_are_whitelisted_and_secrets_redacted(self):
        from qadmin import audit
        self.assertEqual(audit.detail("/api/kick", {"num": 3, "extra": "x"}), {"num": 3})
        self.assertEqual(audit.detail("/api/password", {"current": "a", "new": "b"}), {})
        self.assertEqual(audit.detail("/api/login", {"user": "admin", "password": "p"}), {})
        got = audit.detail("/api/settings", {"settings": {"timelimit": 5, "sv_privatePassword": "h"}})
        self.assertEqual(got, {"settings": {"timelimit": 5, "sv_privatePassword": "(secret)"}})
        self.assertEqual(audit.detail("/api/upload", None, {"name": ["map.pk3"]}), {"name": "map.pk3"})

    def test_entries_are_kept_newest_last_and_the_file_is_capped(self):
        from qadmin import audit
        with StatePaths(), contextlib.redirect_stdout(io.StringIO()):
            for i in range(5):
                audit.record("203.0.113.9", "/api/kick", {"num": i})
            entries = audit.recent(3)
            self.assertEqual([e["detail"]["num"] for e in entries], [2, 3, 4])
            self.assertEqual(entries[-1]["actor"], "203.0.113.9")
            audit.AUDIT_MAX_BYTES, saved = 2000, audit.AUDIT_MAX_BYTES
            try:
                for i in range(100):
                    audit.record("203.0.113.9", "/api/say", {"message": "x" * 40})
            finally:
                audit.AUDIT_MAX_BYTES = saved
            self.assertLessEqual(audit.AUDIT_FILE.stat().st_size, 2000)
            self.assertEqual(audit.recent(1)[0]["detail"]["message"], "x" * 40)


class Backups(unittest.TestCase):
    def test_export_round_trips_through_import_with_validation(self):
        from qadmin import backup
        with StatePaths(), contextlib.redirect_stdout(io.StringIO()):
            settings.save_settings({"timelimit": 15, "sv_hostname": "Home"})
            settings.save_rotation(["q3dm7", "q3dm1"])
            # seen must be recent: an import saves, and a save prunes the stale.
            stats._stats["players"] = {"Trey": {"kills": 3, "deaths": 1, "suicides": 0,
                                                "matches": 1, "best": 3, "bot": False,
                                                "seen": int(time.time())}}
            auth.set_password("correct horse battery")
            bundle = backup.export_state()
            self.assertEqual(bundle["format"], backup.FORMAT)
            self.assertEqual(bundle["rotation"], ["q3dm7", "q3dm1"])
            self.assertIn("hash", bundle["credentials"])

            # Wipe, then restore with some junk mixed in.
            settings.save_rotation([])
            config.SETTINGS_FILE.unlink()
            stats._stats["players"] = {}
            bundle["settings"]["bogus"] = 1
            bundle["settings"]["timelimit"] = 99999
            bundle["rotation"].append("../etc")
            bundle["stats"]["players"]["Bad"] = "not a row"
            bundle["stats"]["players"]["Trey"]["kills"] = "7"
            applied, reauth = backup.import_state(bundle)
            self.assertEqual(applied, {"settings": 1, "rotation": 2, "players": 1, "credentials": True})
            self.assertFalse(reauth)      # the same credentials came back
            self.assertEqual(settings.saved_settings(), {"sv_hostname": "Home"})
            self.assertEqual(settings.saved_rotation(), ["q3dm7", "q3dm1"])
            self.assertEqual(stats._stats["players"]["Trey"]["kills"], 7)
            self.assertTrue(auth.check_password("correct horse battery"))

            bundle["credentials"]["hash"] = "00" * 32
            self.assertTrue(backup.import_state(bundle)[1])   # different: everyone signed out
            bundle["credentials"] = {"salt": "zz", "hash": "00", "iterations": 1}
            with self.assertRaises(ValueError):
                backup.import_state(bundle)
            with self.assertRaises(ValueError):
                backup.import_state({"format": "something-else"})


class PasswordRounds(unittest.TestCase):
    def test_rounds_are_current_and_old_hashes_are_upgraded(self):
        self.assertGreaterEqual(config.PBKDF2_ROUNDS, 600_000)
        with StatePaths():
            salt = b"\x01" * 16
            auth.store_credentials({"salt": salt.hex(), "iterations": 1000,
                                    "hash": auth._hash("old password!", salt, 1000)})
            self.assertTrue(auth.check_password("old password!"))
            self.assertTrue(auth.needs_rehash())
            auth.set_password("old password!")
            self.assertFalse(auth.needs_rehash())
            self.assertEqual(auth.stored_credentials()["iterations"], config.PBKDF2_ROUNDS)
            self.assertTrue(auth.check_password("old password!"))


class StrictPage(unittest.TestCase):
    """The console page runs under a policy with nothing inline, so neither
    the page nor the markup its script generates may carry an inline style
    or event handler - the browser would refuse it, silently to the user."""

    def test_nothing_inline_in_the_page_or_generated_markup(self):
        import re
        html = (ADMIN / "index.html").read_text(encoding="utf-8")
        js = (ADMIN / "console.js").read_text(encoding="utf-8")
        self.assertNotIn(' style="', html)
        self.assertNotIn("<style", html)
        self.assertIsNone(re.search(r'\son[a-z]+="', html))
        self.assertNotIn('style="', js)
        self.assertIsNone(re.search(r'\son[a-z]+=\\?"', js))   # also onerror=\" in a string
        self.assertNotIn("<script", js)

    def test_every_tab_is_a_direct_child_of_main(self):
        """Tabs are shown one at a time by hiding the others; one nested inside
        another is hidden whenever it is selected. The Stats tab shipped that
        way once and was invisible for weeks."""
        from html.parser import HTMLParser

        class Nest(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.parents = [], {}

            def handle_starttag(self, tag, attrs):
                a = dict(attrs)
                if tag == "div":
                    if a.get("id", "").startswith("tab-"):
                        self.parents[a["id"]] = next((i for i in reversed(self.stack) if i), None)
                    self.stack.append(a.get("id"))

            def handle_endtag(self, tag):
                if tag == "div" and self.stack:
                    self.stack.pop()

        nest = Nest()
        nest.feed((ADMIN / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(sorted(nest.parents), ["tab-chat", "tab-log", "tab-maps", "tab-match",
                                                "tab-players", "tab-server", "tab-stats"])
        self.assertTrue(all(parent == "main" for parent in nest.parents.values()), nest.parents)


class LvlWorld(unittest.TestCase):
    PAGE = ('<div><b>Filename:</b> q3map-padcrash.zip<br><b>Filesize:</b> 7.83 MB</div>'
            '<a href="#" class="dlLnk" data-dl="lvl">..::LvL</a>'
            '<a href="#" class="dlLnk" data-dl="FSS">FSS (Europe)</a>'
            '<a href="#" class="dlLnk" data-dl="MHG">MHG (FTP)</a>'
            '<p>sha256 941e81f5e676f6c23b552979377a4b268142eb9b6eca03082401c899e1f10619</p>'
            '<script>function dlLink(e){ var s=this.getAttribute("data-dl");'
            ' location="/dl/"+s+"/2582/1bbba96abdf1d8d38559a1f3d3dac3d5/0ab13be974750778f20d57159eaca4a0"; }</script>')

    def setUp(self):
        from qadmin import assets
        self.assets = assets
        assets._mirror_down.clear()

    def test_page_yields_the_mirrors_on_offer_and_the_visit_token(self):
        meta = self.assets.parse_lvl_page(self.PAGE, "2582")
        self.assertEqual(meta["mirrors"], ["lvl", "FSS"])          # MHG is FTP: dropped
        self.assertEqual(meta["filename"], "q3map-padcrash.zip")
        self.assertEqual(meta["sha256"], "941e81f5e676f6c23b552979377a4b268142eb9b6eca03082401c899e1f10619")
        self.assertTrue(meta["urls"]["FSS"].endswith("/dl/FSS/2582/1bbba96abdf1d8d38559a1f3d3dac3d5/0ab13be974750778f20d57159eaca4a0"))
        self.assertEqual(meta["url"], meta["urls"]["lvl"])
        with self.assertRaises(ValueError):
            self.assets.parse_lvl_page("<b>Filename:</b> x.zip", "1")

    def test_download_falls_through_to_a_mirror_that_serves_a_zip(self):
        import urllib.error
        meta = self.assets.parse_lvl_page(self.PAGE, "2582")
        calls = []

        def fake_fetch(url, referer=None, limit=None, jar=None, verify=True):
            calls.append(url.split("/dl/")[1].split("/")[0])
            if "/dl/lvl/" in url:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            return b"PK\x03\x04 zip bytes"

        original = self.assets._fetch
        self.assets._fetch = fake_fetch
        try:
            blob, mirror = self.assets.lvl_download(meta, jar=None)
            self.assertEqual((mirror, calls), ("FSS", ["lvl", "FSS"]))
            # The refusal is remembered: next time the mirror goes first.
            calls.clear()
            self.assertEqual(self.assets.lvl_download(meta, jar=None)[1], "FSS")
            self.assertEqual(calls, ["FSS"])
            # Every host refusing: every reason is in the error.
            self.assets._fetch = lambda *a, **k: b"<html>not a zip"
            with self.assertRaises(ValueError) as caught:
                self.assets.lvl_download(meta, jar=None)
            self.assertIn("FSS sent a page", str(caught.exception))
            self.assertIn("lvl sent a page", str(caught.exception))
        finally:
            self.assets._fetch = original

    def test_an_unverifiable_mirror_chain_is_accepted_only_on_a_published_hash(self):
        import ssl
        import urllib.error
        meta = self.assets.parse_lvl_page(self.PAGE, "2582")
        seen = []

        def fake_fetch(url, referer=None, limit=None, jar=None, verify=True):
            key = url.split("/dl/")[1].split("/")[0]
            seen.append((key, verify))
            if key == "lvl":
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            if verify:
                raise urllib.error.URLError(ssl.SSLCertVerificationError("unable to get local issuer certificate"))
            return b"PK\x03\x04 zip bytes"

        original = self.assets._fetch
        self.assets._fetch = fake_fetch
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                blob, mirror = self.assets.lvl_download(meta, jar=None)
            self.assertEqual(mirror, "FSS")
            self.assertEqual(seen, [("lvl", True), ("FSS", True), ("FSS", False)])
            self.assertIn("certificate chain incomplete", out.getvalue())
            # No hash from the page: the transport must verify, so this fails.
            seen.clear()
            self.assets._mirror_down.clear()
            meta["sha256"] = None
            with self.assertRaises(ValueError) as caught:
                self.assets.lvl_download(meta, jar=None)
            self.assertIn("FSS SSLCertVerificationError", str(caught.exception))
            self.assertNotIn(("FSS", False), seen)
        finally:
            self.assets._fetch = original


class BotRoom(unittest.TestCase):
    def test_serverinfo_parses_padded_columns_and_ignores_noise(self):
        info = game.parse_serverinfo("Server info settings:\nsv_hostname          ^1My^7 Server\n"
                                     "sv_maxclients        12\nsay: Bob: hello there\n"
                                     "sv_privateClients    0\n")
        self.assertEqual(info["sv_maxclients"], "12")
        self.assertEqual(info["sv_hostname"], "^1My^7 Server")
        self.assertNotIn("say:", info)
        self.assertNotIn("Server", info)

    def test_room_is_the_smaller_of_free_slots_and_the_module_ceiling(self):
        humans = [{"num": i, "bot": False} for i in range(2)]
        bots = [{"num": 10 + i, "bot": True} for i in range(3)]
        room = game.bot_room(humans + bots, {"sv_maxclients": "12", "sv_privateClients": "0"})
        self.assertEqual((room["room"], room["slots"], room["pool"]), (7, 7, config.BOT_CEILING - 3))
        room = game.bot_room(humans + bots, {"sv_maxclients": "64", "sv_privateClients": "2"})
        self.assertEqual((room["room"], room["slots"]), (config.BOT_CEILING - 3, 64 - 2 - 5))
        # The server not answering: the slots are unknown and do not limit.
        self.assertEqual(game.bot_room(bots, {})["room"], config.BOT_CEILING - 3)
        # The ceiling stays below the measured failure point (26 on the largest demo map).
        self.assertTrue(8 <= config.BOT_CEILING <= 26, config.BOT_CEILING)


if __name__ == "__main__":
    unittest.main(verbosity=1)
