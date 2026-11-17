#!/usr/bin/env python3
"""Live end-to-end run against headless Kodi 21 in the test VM.

Each scenario: put Kodi in a starting state over JSON-RPC, "hold the mic"
by dropping pre-encoded UR02 audio frames for the fake btmon, let the real
add-on do everything (BLE parse -> ADPCM decode -> ElevenLabs STT ->
Anthropic -> actions), then assert on Kodi's real state, the add-on's log and
what the stub add-ons recorded.  Run inside the VM after setup_vm.sh/seed.sh.
"""
import glob
import json
import os
import shutil
import sys
import time
import urllib.request

E2E = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.expanduser("~/.kodi/temp/kodi.log")
R = "/tmp/remote"
SELF = "plugin.program.autocompletion"


def rpc(method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request("http://127.0.0.1:8080/jsonrpc", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=15)).get("result")


def kodi_builtin(cmd, wait=1.5):
    """Run a Kodi builtin inside Kodi (JSON-RPC has no ExecuteBuiltin)."""
    path = "/tmp/e2e_builtin.py"
    with open(path, "w") as fh:
        fh.write("import xbmc\nxbmc.executebuiltin(%r)\n" % cmd)
    rpc("Addons.ExecuteAddon", {"addonid": "script.e2e.exec", "params": [path]})
    time.sleep(wait)


def label(name):
    return rpc("XBMC.GetInfoLabels", {"labels": [name]})[name]


def cond(name):
    return rpc("XBMC.GetInfoBooleans", {"booleans": [name]})[name]


def window():
    return rpc("GUI.GetProperties", {"properties": ["currentwindow"]})["currentwindow"]


def player():
    act = rpc("Player.GetActivePlayers") or []
    if not act:
        return None
    pid = act[0]["playerid"]
    item = rpc("Player.GetItem", {"playerid": pid, "properties": ["file"]})["item"]
    props = rpc("Player.GetProperties", {"playerid": pid, "properties": ["time", "speed"]})
    t = props["time"]
    return {"file": item.get("file", ""), "secs": t["hours"] * 3600 + t["minutes"] * 60 + t["seconds"],
            "speed": props["speed"]}


def stop_player():
    for p in rpc("Player.GetActivePlayers") or []:
        rpc("Player.Stop", {"playerid": p["playerid"]})
    time.sleep(1.5)


def log_size():
    return os.path.getsize(LOG)


def log_since(pos):
    with open(LOG, "rb") as fh:
        fh.seek(pos)
        return fh.read().decode("utf-8", "replace")


def stub_log(name):
    p = os.path.join(R, name + ".log")
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p) if l.strip()]


def hold_mic(utterance, timeout=45):
    """Press-talk-release, then wait until the add-on has finished."""
    pos = log_size()
    t0 = time.time()
    dst = os.path.join(R, "press", "%d_%s.frames" % (time.time() * 1000, utterance))
    shutil.copyfile(os.path.join(E2E, "utterances", utterance + ".frames"), dst + ".tmp")
    os.rename(dst + ".tmp", dst)  # atomic: the fake remote never sees a partial file
    done_markers = ("Voice command: ", "Voice keyboard: injecting text", "too short", "Heard nothing",
                    "Voice command STT failed", "Voice keyboard worker error", "unavailable")
    last_change, seen = time.time(), ""
    while time.time() - t0 < timeout:
        # The dictation path may ask the user to pick a title: pick the first.
        if cond("Window.IsActive(selectdialog)"):
            rpc("Input.Select")
        voice = "\n".join(l for l in log_since(pos).splitlines() if "Voice" in l)
        if voice != seen:
            seen, last_change = voice, time.time()
        # done = a terminal marker seen and the add-on quiet for 8 s (chained
        # workflows log one line per step, several seconds apart)
        if any(m in voice for m in done_markers) and time.time() - last_change > 8.0:
            break
        time.sleep(0.25)
    return log_since(pos), time.time() - t0


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, detail))


def heard(out):
    for line in out.splitlines():
        for key in ("Voice command heard: ", "STT heard: ", "STT raw: "):
            if key in line:
                return line.split(key, 1)[1][:80].strip("'\"")
    return "?"


def scenario(title):
    print("\n== %s" % title)


def main():
    for f in glob.glob(R + "/*.log"):
        if not f.endswith("dbus.log"):
            os.remove(f)
    stop_player()
    rpc("GUI.ActivateWindow", {"window": "home"})
    time.sleep(1)

    scenario("1. BLE plumbing: press -> MIC_OPEN, release -> AUDIO_END, MIC_CLOSE")
    out, dt = hold_mic("pause")  # nothing playing: harmless command
    dbus = open(R + "/dbus.log").read()
    check("GET_CAPS handshake sent at startup", "0x0a,0x00,0x04" in dbus or
          "sent ATVV GET_CAPS handshake" in open(LOG, errors="replace").read())
    check("MIC_OPEN written on press", "0x0c,0x00,0x01" in dbus)
    check("release (AUDIO_END) stopped capture", "AUDIO_END received" in out)
    check("MIC_CLOSE written after release", "0x0d,0x00" in dbus)
    check("audio decoded + transcribed", heard(out).lower().startswith("pause"), "heard=%r" % heard(out))

    scenario("2. Dictation into Bingie search (window 1109) with LLM title matching")
    kodi_builtin("Skin.SetString(CustomSearchTerm,)")
    kodi_builtin("ActivateWindow(1109)", wait=0.5)
    for _ in range(20):
        w = window()
        if w["id"] in (1109, 11109):
            break
        time.sleep(0.25)
    check("Bingie search window is on top", w["id"] in (1109, 11109), str(w))
    out, dt = hold_mic("dictate")
    check("handled as dictation, not a command", "injecting text" in out)
    term = label("Skin.String(CustomSearchTerm)")
    check("search term filled", "six feet under" in term.lower(), "term=%r (%.1fs)" % (term, dt))
    rpc("GUI.ActivateWindow", {"window": "home"})

    scenario("3. 'Continue watching Lord of the Rings' -> resume S03E02 at 36:29")
    out, dt = hold_mic("resume")
    time.sleep(3)
    p = player()
    check("resume action chosen", "'action': 'resume'" in out)
    check("playing Rings of Power S03E02", p and "S03E02" in p["file"], p and os.path.basename(p["file"])[:60])
    # playback keeps running while the harness waits: compare against the
    # time since the resume action was logged
    started = [l for l in out.splitlines() if "'action': 'resume'" in l]
    check("resumed at 36:29 (+ elapsed)", p and 2180 <= p["secs"] <= 2189 + dt + 5,
          p and "at %ds, %.0fs after press" % (p["secs"], dt))

    scenario("4. 'Pause the show' while playing")
    out, dt = hold_mic("pause")
    time.sleep(1)
    p = player()
    check("player paused", p and p["speed"] == 0, "speed=%s" % (p and p["speed"]))

    scenario("5. 'Skip ahead two minutes'")
    before = player()["secs"]  # paused, so time only moves by the seek
    out, dt = hold_mic("seek")
    time.sleep(1.5)
    after = player()["secs"]
    check("seeked +120s", 115 <= after - before <= 125, "%ds -> %ds" % (before, after))

    scenario("6. 'Turn the volume down'")
    v0 = rpc("Application.GetProperties", {"properties": ["volume"]})["volume"]
    out, dt = hold_mic("volume")
    time.sleep(1)
    v1 = rpc("Application.GetProperties", {"properties": ["volume"]})["volume"]
    check("volume lowered", v1 < v0, "%s -> %s" % (v0, v1))

    scenario("7. 'Show me the SignalScope Atmos metadata'")
    out, dt = hold_mic("atmos")
    time.sleep(1)
    ss = stub_log("signalscope")
    check("SignalScope got toggle_atmos", any(e.get("notify") == "Other.toggle_atmos" for e in ss), str(ss[-1:]))

    scenario("8. 'Open the PPI' -> SignalScope overlay")
    out, dt = hold_mic("ppi")
    time.sleep(1.5)
    ss = stub_log("signalscope")
    check("SignalScope RunScript overlay", any(e.get("runscript") == ["overlay"] for e in ss), str(ss[-1:]))

    scenario("9. 'Play the next episode of Six Feet Under' (S03E07 not on disk -> nzbdav, no picker)")
    stop_player()
    out, dt = hold_mic("nextep", timeout=60)
    time.sleep(4)
    nz = stub_log("nzbdav")
    rec = nz[-1] if nz else {}
    argv = rec.get("argv", [])
    check("nzbdav tmdb_play called", argv[:1] == ["tmdb_play"], str(argv))
    check("asked for Six Feet Under S03E07", "season=3" in argv and "episode=7" in argv
          and any(a.startswith("title=Six") for a in argv))
    check("picker bypassed (auto_select_best=true on disk at launch)", rec.get("picker_shown") is False)
    p = player()
    check("first release playing", p and "S03E07" in p["file"], p and os.path.basename(p["file"])[:60])
    for _ in range(20):
        prof = open(os.path.expanduser(
            "~/.kodi/userdata/addon_data/plugin.video.nzbdav/settings.xml")).read()
        if 'auto_select_best">false' in prof or 'auto_select_best" default="true">false' in prof:
            break
        time.sleep(1)
    check("user's picker setting restored to false", ">false<" in prof.split("auto_select_best", 1)[1][:40])
    stop_player()

    scenario("10. 'Top ten highest rated sci-fi movies of the last three years' -> TMDb discover list")
    out, dt = hold_mic("discover")
    time.sleep(2)
    tm = stub_log("plugin.video.tmdb.bingie.helper")
    q = tm[-1]["query"] if tm else ""
    check("discover list opened", "info=discover" in q and "tmdb_type=movie" in q, q[:120])
    check("genre/date/sort/votes", "Science%20Fiction" in q and "primary_release_date.gte" in q
          and "vote_average.desc" in q and "vote_count.gte" in q)
    rpc("GUI.ActivateWindow", {"window": "home"})

    scenario("11. 'Search for The Bear' from home -> Bingie search opened and filled")
    kodi_builtin("Skin.SetString(CustomSearchTerm,)")
    out, dt = hold_mic("search")
    time.sleep(1.5)
    check("Bingie search window open", window()["id"] in (1109, 11109), str(window()))
    check("term is The Bear", "bear" in label("Skin.String(CustomSearchTerm)").lower(),
          label("Skin.String(CustomSearchTerm)"))

    scenario("12. 'Go home and open YouTube' (chained workflow)")
    rpc("GUI.ActivateWindow", {"window": "videos"})  # start somewhere that is not a text field
    time.sleep(1.5)
    out, dt = hold_mic("chain")
    time.sleep(2)
    check("both steps ran in order", out.find("'action': 'open'") < out.find("'action': 'open_addon'") and
          "'action': 'open_addon'" in out)
    check("YouTube launched", bool(stub_log("plugin.video.youtube")))

    scenario("13. 'Delete all my files and reboot the system' -> refused")
    rpc("GUI.ActivateWindow", {"window": "home"})
    out, dt = hold_mic("unsafe")
    acts = [l.split("Voice command: ", 1)[1] for l in out.splitlines() if "Voice command: " in l]
    check("only a notify ran", acts and all(a.startswith("notify") for a in acts), str(acts)[:120])
    check("Kodi still running", rpc("JSONRPC.Ping") == "pong")

    scenario("14. Accidental tap (button released instantly) -> no STT call")
    out, dt = hold_mic("tap", timeout=15)
    check("dropped as too short / no speech", ("too short" in out or "No speech" in out
          or "Heard nothing" in out) and "ElevenLabs HTTP" not in out, out.strip().splitlines()[-1][-90:] if out.strip() else "")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n%d/%d checks passed" % (passed, len(RESULTS)))
    for n, ok, d in RESULTS:
        if not ok:
            print("  FAILED: %s %s" % (n, d))
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
