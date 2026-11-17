# -*- coding: utf8 -*-
"""Mock-data tests for voice commands, history, discover, SignalScope,
nzbdav auto-play, the btmon parser and push-to-talk.

Run:  python3 -m unittest discover -s tests -v
"""

import json
import os
import sqlite3
import struct
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kodi_stubs  # noqa: E402

KODI = kodi_stubs.install()

from lib import title_mapper, voice_commands, watch_history, nzbdav_play  # noqa: E402

SELF = "plugin.program.autocompletion"
ROP = "The.Lord.of.the.Rings.The.Rings.of.Power"


def make_db(path, rows):
    """rows: (dir, filename, lastPlayed, playCount, resume_s, total_s)."""
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE path (idPath INTEGER PRIMARY KEY, strPath TEXT);"
        "CREATE TABLE files (idFile INTEGER PRIMARY KEY, idPath INT, strFilename TEXT,"
        " playCount INT, lastPlayed TEXT);"
        "CREATE TABLE bookmark (idBookmark INTEGER PRIMARY KEY, idFile INT,"
        " timeInSeconds REAL, totalTimeInSeconds REAL, type INT);"
    )
    for i, (d, f, last, plays, pos, total) in enumerate(rows, 1):
        con.execute("INSERT INTO path VALUES (?,?)", (i, d))
        con.execute("INSERT INTO files VALUES (?,?,?,?,?)", (i, i, f, plays, last))
        if pos:
            con.execute("INSERT INTO bookmark VALUES (?,?,?,?,1)", (i, i, pos, total))
    con.commit()
    con.close()


class Base(unittest.TestCase):
    def setUp(self):
        KODI.reset()
        self.tmp = tempfile.mkdtemp()
        KODI.paths["special://database/"] = self.tmp + "/"
        KODI.paths["special://profile/addon_data/"] = self.tmp + "/addon_data/"
        KODI.settings[SELF] = {"llm_provider": "anthropic", "llm_key_anthropic": "k"}
        KODI.addons = [
            {"addonid": "plugin.video.youtube", "name": "YouTube", "type": "xbmc.python.pluginsource"},
            {"addonid": "script.xbmc4lyfe.signalscope", "name": "SignalScope", "type": "xbmc.service"},
            {"addonid": "repository.p3i", "name": "p3i", "type": "xbmc.addon.repository"},
        ]
        self.dl = os.path.join(self.tmp, "downloads")
        os.makedirs(self.dl)
        voice_commands._last_history = []

    def episode(self, name):
        d = os.path.join(self.dl, name + " [NZBDAV copy 01]")
        os.makedirs(d)
        p = os.path.join(d, name + ".mkv")
        open(p, "w").close()
        return d, name + ".mkv"

    def run_plan(self, *actions, say=""):
        return voice_commands.execute({"actions": list(actions), "say": say},
                                      lambda q: KODI.builtins.append("BINGIE:" + q))


class TestInterpret(Base):
    def test_parses_json_and_caps_actions(self):
        reply = 'Sure!\n{"actions": [' + ",".join(['{"action":"volume_up"}'] * 9) + '], "say": "ok"}'
        title_mapper.call_llm = lambda prompt, timeout=20: reply
        plan = voice_commands.interpret("louder louder")
        self.assertEqual(len(plan["actions"]), 6)

    def test_garbage_reply_becomes_notify(self):
        title_mapper.call_llm = lambda prompt, timeout=20: "I cannot do that"
        plan = voice_commands.interpret("blah")
        self.assertEqual(plan["actions"][0]["action"], "notify")

    def test_llm_down_returns_none(self):
        title_mapper.call_llm = lambda prompt, timeout=20: None
        self.assertIsNone(voice_commands.interpret("pause"))

    def test_prompt_contains_history_and_addons(self):
        d, f = self.episode(ROP + ".S03E02.The.Gift.of.Men.2160p.AMZN.WEB-DL")
        make_db(os.path.join(self.tmp, "MyVideos131.db"), [(d + "/", f, "2026-11-16 14:12:58", 0, 2189, 4112)])
        seen = {}
        title_mapper.call_llm = lambda prompt, timeout=20: seen.setdefault("p", prompt) and '{"actions":[]}'
        voice_commands.interpret("continue lord of the rings")
        self.assertIn("Rings of Power S03E02", seen["p"])
        self.assertIn("stopped at 36:29", seen["p"])
        self.assertIn("YouTube", seen["p"])
        self.assertNotIn("p3i", seen["p"].split("Installed add-ons you can open:")[1].split("\n")[0])


class TestActions(Base):
    def test_pause_only_when_playing(self):
        self.run_plan({"action": "pause"})
        self.assertEqual(KODI.builtins, [])
        KODI.cond["Player.Playing"] = True
        self.run_plan({"action": "pause"})
        self.assertEqual(KODI.builtins, ["PlayerControl(Play)"])

    def test_play_only_when_paused(self):
        KODI.cond["Player.Paused"] = True
        self.run_plan({"action": "play"})
        self.assertEqual(KODI.builtins, ["PlayerControl(Play)"])

    def test_seek_clamped_and_needs_media(self):
        self.run_plan({"action": "seek", "seconds": -30})
        self.assertEqual(KODI.builtins, [])
        KODI.cond["Player.HasMedia"] = True
        self.run_plan({"action": "seek", "seconds": 99999})
        self.assertEqual(KODI.builtins, ["Seek(3600)"])

    def test_unknown_and_injection_rejected(self):
        done = self.run_plan(
            {"action": "Reboot"},
            {"action": "open", "window": "home),Reboot("},
            {"action": "run_builtin", "cmd": "Powerdown"},
        )
        self.assertEqual(done, [])
        self.assertEqual(KODI.builtins, [])

    def test_search_bingie_when_already_on_top(self):
        KODI.window_id = 11109
        self.run_plan({"action": "search", "query": "The Bear"})
        self.assertEqual(KODI.builtins, ["BINGIE:The Bear"])

    def test_search_from_fullscreen_backs_out_first(self):
        KODI.cond["Window.IsVisible(fullscreenvideo)"] = True
        self.run_plan({"action": "search", "query": "The Bear"})
        self.assertEqual(KODI.builtins[:2], ["Action(Back)", "ActivateWindow(1109)"])

    def test_search_bingie_opens_window_and_sanitises(self):
        self.run_plan({"action": "search", "query": 'six feet "under")\\'})
        self.assertEqual(KODI.builtins[0], "ActivateWindow(1109)")
        self.assertEqual(KODI.builtins[1], "BINGIE:six feet under )")

    def test_open_addon_and_settings(self):
        self.run_plan({"action": "open_addon", "name": "you tube"})
        self.run_plan({"action": "open_addon_settings", "name": "this"})
        self.run_plan({"action": "open_addon", "name": "p3i"})  # repository: not launchable
        self.assertEqual(KODI.builtins, ["RunAddon(plugin.video.youtube)",
                                         "Addon.OpenSettings(plugin.program.autocompletion)"])

    def test_signalscope_commands(self):
        self.run_plan({"action": "signalscope_atmos"}, {"action": "signalscope_dv"},
                      {"action": "signalscope_overlay"}, {"action": "signalscope_inspector"})
        self.assertEqual(KODI.builtins, [
            "NotifyAll(script.xbmc4lyfe.signalscope,toggle_atmos)",
            "NotifyAll(script.xbmc4lyfe.signalscope,toggle_dv)",
            "RunScript(script.xbmc4lyfe.signalscope,overlay)",
            "RunScript(script.xbmc4lyfe.signalscope,inspector)",
        ])

    def test_chain_runs_in_order(self):
        KODI.cond["Player.HasMedia"] = True
        done = self.run_plan({"action": "open", "window": "home"}, {"action": "seek", "seconds": 120},
                             {"action": "volume_down"}, {"action": "subtitles_toggle"})
        self.assertEqual(done, ["open", "seek", "volume_down", "subtitles_toggle"])
        self.assertEqual(KODI.builtins, ["ActivateWindow(home)", "Seek(120)",
                                         "Action(VolumeDown)", "ShowSubtitles"])


class TestDiscover(Base):
    def setUp(self):
        super().setUp()
        KODI.cond["System.AddonIsEnabled(plugin.video.tmdb.bingie.helper)"] = True

    def url(self):
        b = KODI.builtins[0]
        self.assertTrue(b.startswith('ActivateWindow(videos,"plugin://plugin.video.tmdb.bingie.helper/?'))
        return b

    def test_top_rated_scifi_last_3_years(self):
        self.run_plan({"action": "discover", "type": "movie", "genres": ["science fiction"],
                       "years_back": 3, "sort": "rating"})
        u = self.url()
        self.assertIn("with_genres=Science%20Fiction", u)
        self.assertIn("sort_by=vote_average.desc", u)
        self.assertIn("vote_count.gte=300", u)
        self.assertIn("primary_release_date.gte=", u)

    def test_tv_decade_and_bad_genre_dropped(self):
        self.run_plan({"action": "discover", "type": "tv", "genres": ["Comedy", "Bogus"],
                       "year_from": 1990, "year_to": 1999, "sort": "popularity"})
        u = self.url()
        self.assertIn("tmdb_type=tv", u)
        self.assertIn("with_genres=Comedy&", u)
        self.assertIn("first_air_date.gte=1990-01-01", u)
        self.assertIn("first_air_date.lte=1999-12-31", u)
        self.assertNotIn("Bogus", u)

    def test_no_helper_installed(self):
        KODI.cond.clear()
        self.assertEqual(self.run_plan({"action": "discover", "type": "movie"}), [])


class TestHistoryAndResume(Base):
    def setUp(self):
        super().setUp()
        self.e2 = self.episode(ROP + ".S03E02.The.Gift.of.Men.2160p.AMZN.WEB-DL")
        self.e3 = self.episode(ROP + ".S03E03.The.Secret.Fire.2160p.AMZN.WEB-DL")
        self.sfu = self.episode("Six.Feet.Under.S03E06.Making.Love.Work.1080p.AMZN.WEB-DL")
        make_db(os.path.join(self.tmp, "MyVideos131.db"), [
            ("special://skin/", "bingie_intro_2160p.mp4", "2026-11-16 14:13:22", 13, 0, 0),
            (self.e2[0] + "/", self.e2[1], "2026-11-16 14:12:58", 0, 2189, 4112),
            (self.sfu[0] + "/", self.sfu[1], "2026-11-16 01:17:00", 1, 0, 0),
        ])

        def rpc(req):
            if req["method"] == "Player.Open":
                KODI.cond["Player.HasMedia"] = True
            return None

        KODI.on_rpc = rpc

    def test_recent_skips_intro_and_names(self):
        items = watch_history.recent()
        self.assertEqual([i["name"] for i in items], [
            "The Lord of the Rings The Rings of Power S03E02 - The Gift of Men",
            "Six Feet Under S03E06 - Making Love Work"])
        self.assertEqual(items[0]["resume_s"], 2189)
        self.assertTrue(items[1]["finished"])

    def test_resume_plays_file_with_resume(self):
        voice_commands._last_history = watch_history.recent()
        self.run_plan({"action": "resume", "item": 1})
        req = [r for r in KODI.rpc if r["method"] == "Player.Open"][0]
        self.assertTrue(req["params"]["options"]["resume"])
        self.assertTrue(req["params"]["item"]["file"].endswith("S03E02.The.Gift.of.Men.2160p.AMZN.WEB-DL.mkv"))

    def test_next_episode_local(self):
        voice_commands._last_history = watch_history.recent()
        self.run_plan({"action": "play_next_episode", "item": 1})
        req = [r for r in KODI.rpc if r["method"] == "Player.Open"][0]
        self.assertIn("S03E03", req["params"]["item"]["file"])

    def test_bad_item_index_rejected(self):
        voice_commands._last_history = watch_history.recent()
        self.assertEqual(self.run_plan({"action": "resume", "item": 99}), [])

    def test_next_episode_missing_goes_to_nzbdav(self):
        voice_commands._last_history = watch_history.recent()
        calls = []
        orig = (nzbdav_play.available, nzbdav_play.play)
        nzbdav_play.available = lambda: True
        nzbdav_play.play = lambda *a, **k: calls.append((a, k)) or True
        try:
            done = self.run_plan({"action": "play_next_episode", "item": 2})
        finally:
            nzbdav_play.available, nzbdav_play.play = orig
        self.assertEqual(done, ["play_next_episode"])
        self.assertEqual(calls, [(("episode", "Six Feet Under"), {"season": 3, "episode": 7})])
        self.assertIn("Getting Six Feet Under S03E07", KODI.notifications)


class TestNzbdavAutoPlay(Base):
    def setUp(self):
        super().setUp()
        KODI.cond["System.AddonIsEnabled(plugin.video.nzbdav)"] = True
        self.sfile = os.path.join(self.tmp, "addon_data", "plugin.video.nzbdav", "settings.xml")
        os.makedirs(os.path.dirname(self.sfile))
        self.write_disk("false")
        KODI.settings["plugin.video.nzbdav"] = {"auto_select_best": "false"}
        KODI.settings["__on_set__"] = lambda aid, k, v: aid == "plugin.video.nzbdav" and self.write_disk(v)

    def write_disk(self, v):
        with open(self.sfile, "w") as fh:
            fh.write('<settings version="2">\n    <setting id="auto_select_best">%s</setting>\n</settings>' % v)

    def test_episode_enables_autoselect_and_launches_script(self):
        restored = []
        orig = nzbdav_play._restore_later
        nzbdav_play._restore_later = lambda prev: restored.append(prev)
        try:
            ok = nzbdav_play.play("episode", "Six Feet Under", season=3, episode=7)
        finally:
            nzbdav_play._restore_later = orig
        self.assertTrue(ok)
        self.assertEqual(nzbdav_play._on_disk(), "true")
        self.assertEqual(restored, ["false"])
        self.assertEqual(KODI.builtins, [
            "RunScript(special://home/addons/plugin.video.nzbdav/addon.py,tmdb_play,type=episode,"
            "title=Six%20Feet%20Under,season=3,episode=7,ep_season=3,ep_episode=7)"])

    def test_restore_after_new_playback_while_already_playing(self):
        nzbdav_play._set("true")
        KODI.cond["Player.HasMedia"] = True
        KODI.labels["Player.FilenameAndPath"] = "/old/S03E06.mkv"
        import threading
        threading.Timer(0.2, lambda: KODI.labels.update(
            {"Player.FilenameAndPath": "/new/S03E07.mkv"})).start()
        orig_sleep = sys.modules["xbmc"].sleep
        nzbdav_play._restore_later("false")
        deadline = time.time() + 5
        while time.time() < deadline and nzbdav_play._on_disk() != "false":
            time.sleep(0.05)
        sys.modules["xbmc"].sleep = orig_sleep
        self.assertEqual(nzbdav_play._on_disk(), "false")

    def test_already_on_is_left_alone(self):
        self.write_disk("true")
        KODI.settings["plugin.video.nzbdav"]["auto_select_best"] = "true"
        nzbdav_play.play("movie", "Dune Part Two", year=2024)
        self.assertEqual(nzbdav_play._on_disk(), "true")
        self.assertIn("type=movie,title=Dune%20Part%20Two,year=2024", KODI.builtins[0])

    def test_get_episode_action_validates(self):
        orig = nzbdav_play._restore_later
        nzbdav_play._restore_later = lambda prev: None
        self.addCleanup(setattr, nzbdav_play, "_restore_later", orig)
        self.assertEqual(self.run_plan({"action": "get_episode", "title": "Six Feet Under",
                                        "season": "x", "episode": 7}), [])
        self.assertEqual(self.run_plan({"action": "get_episode", "title": "Six Feet Under",
                                        "season": 3, "episode": 7}), ["get_episode"])


class TestBtmonAndPushToTalk(Base):
    def make_service(self):
        import service
        svc = service.VoiceService()
        self.events = []
        svc._on_ble_voice_start = lambda: self.events.append("start")

        class Backend(object):
            _recording = True

            def signal_stream_end(s):
                self.events.append("stop")

            def feed_audio_data(s, h):
                self.events.append("audio:%d" % (len(h) // 2))

        svc._ble_backend = Backend()
        return svc

    def test_parser_multiline_and_singleline(self):
        import subprocess
        svc = self.make_service()
        dump = (
            "> ACL\n        Handle: 0x0042\n          Data[1]:\n        08    .\n"
            "> ACL\n        Handle: 0x003f\n          Data[20]:\n"
            "        01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e 0f 10  ................\n"
            "        11 12 13 14                                      ....\n"
            "> ACL\n        Handle: 0x0042\n          Data[1]: ff\n"
        )
        svc._btmon_proc = subprocess.Popen(["printf", "%s", dump], stdout=subprocess.PIPE)
        svc._dispatch_notification = lambda h, d: self.events.append((h, d))
        svc._btmon_reader()
        svc._btmon_proc.wait()
        svc._btmon_proc.stdout.close()
        self.assertEqual(self.events, [("0x0042", "08"),
                                       ("0x003f", "0102030405060708090a0b0c0d0e0f1011121314"),
                                       ("0x0042", "ff")])

    def test_search_window_under_fullscreen_video_is_not_dictation(self):
        import service
        KODI.window_id = 12005  # fullscreen video on top, 1109 still open beneath
        KODI.cond["Window.IsVisible(1109)"] = True
        self.assertFalse(service.VoiceService()._keyboard_visible())
        KODI.window_id = 11109
        self.assertTrue(service.VoiceService()._keyboard_visible())

    def test_capture_starts_on_press_before_worker(self):
        import service, threading
        svc = service.VoiceService()
        started = []

        class Backend(object):
            _recording = False

            def start_recording(s, external_feed=False):
                started.append(threading.current_thread().name)
                s._recording = True

            def stop_recording(s):
                s._recording = False
                return b""

        svc._ble_backend = Backend()
        svc._keyboard_visible = lambda: False
        svc._commands_enabled = lambda: False
        main = threading.current_thread().name
        svc._on_ble_voice_start()
        time.sleep(0.2)
        self.assertEqual(started, [main])           # in the btmon reader thread
        self.assertFalse(svc._ble_backend._recording)  # discarded when ignored

    def test_release_stops_capture_immediately(self):
        svc = self.make_service()
        svc._last_activation_time = time.time() - 1.0
        svc._dispatch_notification("0x0042", "00")
        self.assertEqual(self.events, ["stop"])

    def test_spurious_release_right_after_press_ignored(self):
        svc = self.make_service()
        svc._last_activation_time = time.time()
        svc._dispatch_notification("0x0042", "00")
        self.assertEqual(self.events, [])

    def test_ignored_audio_does_not_retrigger(self):
        svc = self.make_service()
        svc._ignore_audio_until = time.time() + 3
        svc._dispatch_notification("0x003f", "00" * 20)
        self.assertEqual(self.events, ["audio:20"])


class TestSilenceDetector(unittest.TestCase):
    def test_waits_for_speech_then_silence(self):
        from lib.audio_capture.silence import SilenceDetector
        q = struct.pack("<800h", *([50] * 800))
        loud = struct.pack("<800h", *([3000, -3000] * 400))
        d = SilenceDetector(sample_rate=8000)
        seq = [q] * 15 + [loud] * 10 + [q] * 30
        stop = next(i for i, c in enumerate(seq) if d.process(c))
        self.assertAlmostEqual((stop + 1) * 0.1, 4.0, places=1)


if __name__ == "__main__":
    unittest.main()
