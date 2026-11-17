# -*- coding: utf8 -*-
"""
Voice commands: spoken request -> LLM -> allowlisted Kodi actions.

Safety model: the LLM never produces Kodi builtins or JSON-RPC itself. It may
only pick action names from ACTIONS below with simple arguments; every action
and argument is validated here and mapped to a fixed, harmless builtin.
Nothing can install/remove add-ons, change system settings, delete files,
run scripts by path, or power off the device.
"""

import datetime
import json
import re

import xbmc  # type: ignore
import xbmcgui  # type: ignore

from lib import title_mapper
from lib import watch_history
from lib import nzbdav_play

# name -> Kodi window for "open"
WINDOWS = {
    "home": "home",
    "movies": "videos,videodb://movies/titles/,return",
    "tvshows": "videos,videodb://tvshows/titles/,return",
    "music": "music",
    "favourites": "favourites",
    "addons": "addonbrowser",
    "video_addons": "videos,addons://sources/video/,return",
    "program_addons": "programs,addons://sources/executable/,return",
    "settings": "settings",
    "weather": "weather",
    "files": "videos,sources://video/,return",
    "queue": "videoplaylist",
}

_SIMPLE = {
    # action -> builtin (no arguments)
    "play": None,  # handled specially (only resumes if paused)
    "pause": None,  # handled specially (only pauses if playing)
    "stop": "PlayerControl(Stop)",
    "next": "PlayerControl(Next)",
    "previous": "PlayerControl(Previous)",
    "back": "Action(Back)",
    "info": "Action(Info)",
    "context_menu": "Action(ContextMenu)",
    "fullscreen": "Action(FullScreen)",
    "mute_toggle": "Mute",
    "volume_up": "Action(VolumeUp)",
    "volume_down": "Action(VolumeDown)",
    "subtitles_toggle": "ShowSubtitles",
    "subtitles_next": "NextSubtitle",
    "audio_next": "AudioNextLanguage",
    "player_process_info": "Action(PlayerProcessInfo)",
    "player_debug": "Action(PlayerDebug)",
    # SignalScope (script.xbmc4lyfe.signalscope, TinyPPI fork) — exact
    # commands from its launcher.py / ui/views.py
    "signalscope_overlay": "RunScript(script.xbmc4lyfe.signalscope,overlay)",
    "signalscope_inspector": "RunScript(script.xbmc4lyfe.signalscope,inspector)",
    "signalscope_atmos": "NotifyAll(script.xbmc4lyfe.signalscope,toggle_atmos)",
    "signalscope_dv": "NotifyAll(script.xbmc4lyfe.signalscope,toggle_dv)",
}

MOVIE_GENRES = ['Action', 'Adventure', 'Animation', 'Comedy', 'Crime', 'Documentary', 'Drama', 'Family', 'Fantasy', 'History', 'Horror', 'Music', 'Mystery', 'Romance', 'Science Fiction', 'TV Movie', 'Thriller', 'War', 'Western']
TV_GENRES = ['Action & Adventure', 'Animation', 'Comedy', 'Crime', 'Documentary', 'Drama', 'Family', 'Kids', 'Mystery', 'News', 'Reality', 'Sci-Fi & Fantasy', 'Soap', 'Talk', 'War & Politics', 'Western']

ACTION_DOC = """\
- search {"query": str}            open the search screen and search for it
- play | pause | stop | next | previous
- seek {"seconds": int}             relative jump, negative = back (max 3600)
- volume_up | volume_down | mute_toggle
- subtitles_toggle | subtitles_next | audio_next
- back | info | context_menu | fullscreen
- player_process_info              toggle Kodi's built-in process info overlay
                                   (only when the user says "Kodi's" process info)
- player_debug                     toggle the detailed player debug overlay
- signalscope_overlay              SignalScope PPI overlay (codec, bitrate, HDR, audio)
- signalscope_atmos                toggle SignalScope's full-screen Atmos room
                                   (Atmos objects / speaker / audio metadata view)
- signalscope_dv                   toggle SignalScope's Dolby Vision / HDR overlay
- signalscope_inspector            SignalScope inspector (HDMI, EDID, room details)
- open {"window": one of %s}
- open_addon {"name": str}          launch an installed add-on by name
- open_addon_settings {"name": str} settings of an installed add-on ("this" = the voice add-on)
- discover {"type": "movie"|"tv", "genres": [str], "years_back": int,
            "year_from": int, "year_to": int, "sort": "rating"|"popularity"|"newest",
            "min_votes": int}   open a ranked TMDb list ("top rated sci-fi of the
            last 3 years", "popular comedies", "best 90s thrillers"). All args
            optional. Genres must be TMDb names:
            movie: {movie_genres}
            tv: {tv_genres}
- resume {"item": int}               continue a recently played item from where it stopped
- play_next_episode {"item": int}    play the episode after a recently played one
- play_from_start {"item": int}      replay a recently played item from the beginning
- get_episode {"title": str, "season": int, "episode": int}
                                   find + play a specific episode via NZB (auto-picks
                                   the first release, no picker)
- get_movie {"title": str, "year": int}   find + play a movie via NZB (no picker)
- wait {"seconds": int}             pause the workflow 1-10 s
- notify {"text": str}              say something / when nothing else fits
""" % ", ".join(sorted(WINDOWS))

_PROMPT = """You control a Kodi media center by voice. Turn the spoken request \
into a short workflow of actions from this list ONLY:
{actions}
Current state: {context}
Recently played (newest first):
{history}
Installed add-ons you can open: {addons}

Spoken request (speech-to-text, may contain mishearings and filler words): "{text}"

Rules:
- Actions run in order; each waits for the previous one to finish (screens to
  open, playback to start). Chain up to 6 to fulfil multi-part requests.
- "continue / keep watching / resume X": use resume on the matching recently
  played item if it was stopped part-way; if it was finished, use
  play_next_episode (it downloads the episode automatically if it is not on
  disk). Match loosely (e.g. "Lord of the Rings" = "The Lord of the
  Rings The Rings of Power"). Only use search if nothing recent matches.
- "PPI", "TinyPPI", "the PPI", "process info", "signal info", "SignalScope" =
  signalscope_overlay. Atmos / audio objects / speaker metadata =
  signalscope_atmos. Dolby Vision / HDR / DV metadata = signalscope_dv.
  HDMI / EDID / inspector = signalscope_inspector. Use the specific one.
- "play / download / get <show> season X episode Y" or "<movie>" that is not
  in recently played = get_episode / get_movie.
- Fix obvious title mishearings in search queries. Add-on names are often
  misheard (e.g. "you tube" for "YouTube"): match them to the
  installed list. The first word may be cut off, so a request that is only an
  add-on name means open_addon; one that is only a title means search.
- If the request is unclear or asks for anything not in the list, use a
  single notify explaining briefly.

Reply with ONLY a JSON object, no prose, no code fences:
{{"actions": [{{"action": "<name>", ...args}}], "say": "<very short confirmation>"}}"""


_last_history = []


def _clean(text, limit=100):
    """Strip characters that could break out of a quoted builtin argument."""
    text = re.sub(r'[\\"\r\n\t]', " ", str(text))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _context():
    bits = []
    if xbmc.getCondVisibility("Player.HasMedia"):
        bits.append(
            "media is {}".format(
                "paused" if xbmc.getCondVisibility("Player.Paused") else "playing"
            )
        )
        title = xbmc.getInfoLabel("VideoPlayer.TVShowTitle") or xbmc.getInfoLabel(
            "Player.Title"
        )
        if title:
            bits.append("now playing: {}".format(_clean(title, 80)))
    else:
        bits.append("nothing playing")
    bits.append("current window: {}".format(xbmc.getInfoLabel("System.CurrentWindow")))
    return "; ".join(bits)


def _installed_addons():
    """{lowercase name: addonid} for enabled, launchable add-ons."""
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "Addons.GetAddons",
        "params": {"enabled": True, "properties": ["name"]},
    }
    try:
        data = json.loads(xbmc.executeJSONRPC(json.dumps(req)))
        addons = data["result"]["addons"]
    except Exception:
        return {}
    out = {}
    for a in addons:
        aid = a.get("addonid", "")
        if aid.split(".")[0] in ("plugin", "script", "skin", "service", "repository"):
            out[a.get("name", aid).lower()] = (aid, a.get("type", ""))
    return out


def _addon_names():
    names = sorted(
        n for n, (aid, _t) in _installed_addons().items()
        if aid.split(".")[0] in ("plugin", "script")
        and not aid.startswith(("script.module.", "plugin.program.autocompletion"))
    )
    return ", ".join(names)[:2500] or "(none)"


def _find_addon(name):
    name = _clean(name, 60).lower()
    if name in ("this", "voice", "autocompletion", "voice keyboard"):
        return ("plugin.program.autocompletion", "xbmc.python.script")
    addons = _installed_addons()
    if name in addons:
        return addons[name]
    for n, v in addons.items():  # substring match either way
        if name and (name in n or n in name):
            return v
    for n, v in addons.items():  # match on add-on id
        if name.replace(" ", "") in v[0].lower():
            return v
    return None


def interpret(text):
    """Return a validated plan dict, or None if the LLM is unavailable."""
    global _last_history
    _last_history = watch_history.recent()
    raw = title_mapper.call_llm(
        _PROMPT.format(
            actions=ACTION_DOC,
            context=_context(),
            history=watch_history.describe(_last_history),
            addons=_addon_names(),
            text=_clean(text, 300),
        )
    )
    if raw is None:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        plan = json.loads(m.group(0)) if m else {}
    except ValueError:
        plan = {}
    actions = plan.get("actions") if isinstance(plan, dict) else None
    if not isinstance(actions, list):
        return {"actions": [{"action": "notify", "text": "Sorry, I didn't get that"}], "say": ""}
    return {"actions": [a for a in actions if isinstance(a, dict)][:6], "say": _clean(plan.get("say", ""), 80)}


def _search(query, set_search_term):
    query = _clean(query)
    if not query:
        return False
    if xbmc.getSkinDir() == "skin.bingie":
        if xbmcgui.getCurrentWindowId() not in (1109, 11109):
            # Leave fullscreen video first or the search opens behind it.
            if xbmc.getCondVisibility("Window.IsVisible(fullscreenvideo)"):
                xbmc.executebuiltin("Action(Back)")
                xbmc.sleep(300)
            xbmc.executebuiltin("ActivateWindow(1109)")
            for _ in range(30):
                xbmc.sleep(100)
                if xbmcgui.getCurrentWindowId() in (1109, 11109):
                    break
        set_search_term(query)
        return True
    # Other skins: library title search via the video window's filter.
    xbmc.executebuiltin(
        'ActivateWindow(videos,"videodb://movies/titles/?filter={}",return)'.format(
            query.replace(" ", "%20")
        )
    )
    return True


def _history_item(a):
    try:
        idx = int(a.get("item", 0))
    except (TypeError, ValueError):
        return None
    for it in _last_history:
        if it["id"] == idx:
            return it
    return None


def _play_file(path, resume):
    req = {
        "jsonrpc": "2.0", "id": 1, "method": "Player.Open",
        "params": {"item": {"file": path}, "options": {"resume": bool(resume)}},
    }
    xbmc.executeJSONRPC(json.dumps(req))
    for _ in range(100):  # wait up to 10 s for playback so later steps apply
        if xbmc.getCondVisibility("Player.HasMedia"):
            xbmc.sleep(1500)  # let the stream settle before seeking etc.
            return True
        xbmc.sleep(100)
    return False


def _wait_ui():
    """Let a just-opened window finish loading before the next step."""
    xbmc.sleep(400)
    for _ in range(50):
        if not xbmc.getCondVisibility("Window.IsActive(busydialog)") and not \
                xbmc.getCondVisibility("Window.IsActive(busydialognocancel)"):
            return
        xbmc.sleep(100)


_HELPERS = ("plugin.video.tmdb.bingie.helper", "plugin.video.themoviedb.helper")


def _discover(a):
    """Build a TMDb Helper discover list from validated args and open it."""
    from urllib.parse import quote

    tmdb_type = "tv" if str(a.get("type", "movie")).lower() in ("tv", "show", "shows") else "movie"
    allowed = TV_GENRES if tmdb_type == "tv" else MOVIE_GENRES
    lookup = {g.lower(): g for g in allowed}
    genres = [lookup[str(g).lower()] for g in (a.get("genres") or []) if str(g).lower() in lookup][:3]

    def _int(key, lo, hi):
        try:
            return max(lo, min(hi, int(a.get(key))))
        except (TypeError, ValueError):
            return None

    today = datetime.date.today()
    year_from, year_to = _int("year_from", 1900, today.year + 1), _int("year_to", 1900, today.year + 1)
    years_back = _int("years_back", 1, 100)
    date_field = "first_air_date" if tmdb_type == "tv" else "primary_release_date"
    params = [("info", "discover"), ("tmdb_type", tmdb_type)]
    if genres:
        params.append(("with_genres", " / ".join(genres)))
        params.append(("with_separator", "AND" if len(genres) > 1 else "OR"))
    if years_back:
        start = today.replace(year=today.year - years_back)
        params.append((date_field + ".gte", start.isoformat()))
    elif year_from:
        params.append((date_field + ".gte", "{}-01-01".format(year_from)))
    if year_to:
        params.append((date_field + ".lte", "{}-12-31".format(year_to)))
    sort = str(a.get("sort", "rating"))
    if sort == "popularity":
        params.append(("sort_by", "popularity.desc"))
    elif sort == "newest":
        params.append(("sort_by", date_field + ".desc"))
    else:
        params.append(("sort_by", "vote_average.desc"))
    min_votes = _int("min_votes", 0, 100000)
    if min_votes is None:
        min_votes = 300 if sort == "rating" else 0
    if min_votes:
        params.append(("vote_count.gte", str(min_votes)))
    helper = next((h for h in _HELPERS if xbmc.getCondVisibility("System.AddonIsEnabled({})".format(h))), None)
    if not helper:
        return False
    url = "plugin://{}/?{}".format(
        helper, "&".join("{}={}".format(k, quote(v, safe="")) for k, v in params)
    )
    xbmc.log("Voice command discover: {}".format(url), xbmc.LOGINFO)
    xbmc.executebuiltin('ActivateWindow(videos,"{}",return)'.format(url))
    _wait_ui()
    return True


def execute(plan, set_search_term):
    """Run a validated plan. Returns a short human summary of what was done."""
    done = []
    for a in plan.get("actions", []):
        name = str(a.get("action", ""))
        ok = False
        if name == "search":
            ok = _search(a.get("query", ""), set_search_term)
        elif name == "pause":
            if xbmc.getCondVisibility("Player.Playing"):
                xbmc.executebuiltin("PlayerControl(Play)")  # toggles to paused
            ok = True
        elif name == "play":
            if xbmc.getCondVisibility("Player.Paused"):
                xbmc.executebuiltin("PlayerControl(Play)")
            ok = True
        elif name in _SIMPLE:
            xbmc.executebuiltin(_SIMPLE[name])
            ok = True
        elif name == "seek":
            try:
                secs = max(-3600, min(3600, int(a.get("seconds", 0))))
            except (TypeError, ValueError):
                secs = 0
            if secs and xbmc.getCondVisibility("Player.HasMedia"):
                xbmc.executebuiltin("Seek({})".format(secs))
                xbmc.sleep(500)
                ok = True
        elif name == "open":
            win = WINDOWS.get(str(a.get("window", "")))
            if win:
                xbmc.executebuiltin("ActivateWindow({})".format(win))
                _wait_ui()
                ok = True
        elif name in ("open_addon", "open_addon_settings"):
            found = _find_addon(a.get("name", ""))
            if found:
                aid, atype = found
                if name == "open_addon_settings":
                    xbmc.executebuiltin("Addon.OpenSettings({})".format(aid))
                    ok = True
                elif aid.split(".")[0] in ("plugin", "script"):
                    xbmc.executebuiltin("RunAddon({})".format(aid))
                    _wait_ui()
                    ok = True
            else:
                xbmcgui.Dialog().notification(
                    "Voice", "No add-on named '{}'".format(_clean(a.get("name", ""), 40)),
                    xbmcgui.NOTIFICATION_WARNING, 3000,
                )
        elif name in ("resume", "play_from_start", "play_next_episode"):
            it = _history_item(a)
            if it:
                if name == "play_next_episode":
                    nxt = watch_history.next_episode(it["path"])
                    coords = watch_history.episode_coords(it["path"])
                    if nxt:
                        ok = _play_file(nxt, resume=True)
                    elif coords and nzbdav_play.available():
                        show, season, ep = coords
                        xbmcgui.Dialog().notification(
                            "Voice", "Getting {} S{:02d}E{:02d}".format(show, season, ep + 1),
                            xbmcgui.NOTIFICATION_INFO, 3000)
                        ok = nzbdav_play.play("episode", show, season=season, episode=ep + 1)
                    else:
                        xbmcgui.Dialog().notification(
                            "Voice", "Next episode of {} not found".format(_clean(it["name"], 40)),
                            xbmcgui.NOTIFICATION_WARNING, 3000)
                else:
                    ok = _play_file(it["path"], resume=(name == "resume"))
        elif name == "discover":
            ok = _discover(a)
        elif name in ("get_episode", "get_movie"):
            title = _clean(a.get("title", ""), 80)
            try:
                if name == "get_episode":
                    season = max(0, min(100, int(a.get("season"))))
                    ep = max(0, min(999, int(a.get("episode"))))
                year = a.get("year")
                year = int(year) if year and 1900 <= int(year) <= 2100 else ""
            except (TypeError, ValueError):
                title = ""
            if title and nzbdav_play.available():
                if name == "get_episode":
                    ok = nzbdav_play.play("episode", title, season=season, episode=ep)
                else:
                    ok = nzbdav_play.play("movie", title, year=year)
        elif name == "wait":
            try:
                secs = max(1, min(10, int(a.get("seconds", 1))))
            except (TypeError, ValueError):
                secs = 1
            xbmc.sleep(secs * 1000)
            ok = True
        elif name == "notify":
            xbmcgui.Dialog().notification("Voice", _clean(a.get("text", ""), 80) or "?",
                                          xbmcgui.NOTIFICATION_INFO, 3000)
            ok = True
        xbmc.log("Voice command: {} {} -> {}".format(name, a, "ok" if ok else "rejected"),
                 xbmc.LOGINFO)
        if ok and name != "notify":
            done.append(name)
    return done
