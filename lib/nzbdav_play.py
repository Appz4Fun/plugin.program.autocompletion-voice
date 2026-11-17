# -*- coding: utf8 -*-
"""
Fetch-and-play via NeNeTeePee-Stream (plugin.video.nzbdav) with no picker.

nzbdav's "auto_select_best" setting makes it skip its results dialog and play
filtered[0] — the first release (first duplicate group) the picker would have
listed. Voice requests should behave that way without changing the user's
normal picker experience, so we switch the setting on just for this request
and restore the previous value as soon as playback starts (or after a
timeout). nzbdav's script route reads its settings.xml from disk, so the write
is verified on disk before launching.
"""

import os
import re
import threading
import time
from urllib.parse import quote

import xbmc  # type: ignore
import xbmcaddon  # type: ignore
import xbmcvfs  # type: ignore

ADDON_ID = "plugin.video.nzbdav"
_SETTING = "auto_select_best"
_RESTORE_TIMEOUT = 600  # s; a fresh download can take a while to start
_lock = threading.Lock()


def available():
    return bool(xbmc.getCondVisibility("System.AddonIsEnabled({})".format(ADDON_ID)))


def _settings_file():
    return os.path.join(
        xbmcvfs.translatePath("special://profile/addon_data/{}/".format(ADDON_ID)),
        "settings.xml",
    )


def _on_disk():
    try:
        with open(_settings_file(), "r", encoding="utf-8") as fh:
            m = re.search(r'id="{}"[^>]*>([^<]*)<'.format(_SETTING), fh.read())
        return m.group(1).strip().lower() if m else None
    except OSError:
        return None


def _set(value):
    addon = xbmcaddon.Addon(ADDON_ID)
    addon.setSetting(_SETTING, value)
    for _ in range(20):  # Kodi persists asynchronously
        if _on_disk() == value:
            return True
        xbmc.sleep(100)
    return _on_disk() == value


def _restore_later(previous):
    def run():
        # Restore once nzbdav has started playing something NEW (the file
        # changes — works whether or not something was already playing).
        deadline = time.time() + _RESTORE_TIMEOUT
        before = xbmc.getInfoLabel("Player.FilenameAndPath")
        while time.time() < deadline:
            now = xbmc.getInfoLabel("Player.FilenameAndPath")
            if xbmc.getCondVisibility("Player.HasMedia") and now and now != before:
                xbmc.sleep(3000)
                break
            xbmc.sleep(1000)
        with _lock:
            _set(previous or "false")
        xbmc.log("Voice nzbdav: restored {}={}".format(_SETTING, previous or "false"), xbmc.LOGINFO)

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()


def _clean(text):
    return re.sub(r'[\\"\r\n\t,()]', " ", str(text)).strip()[:120]


def play(media_type, title, year="", season=None, episode=None):
    """Search nzbdav and auto-play its first release. Returns True if launched."""
    if not available() or not _clean(title):
        return False
    with _lock:
        previous = xbmcaddon.Addon(ADDON_ID).getSetting(_SETTING)
        if previous != "true" and not _set("true"):
            xbmc.log("Voice nzbdav: could not enable auto-select", xbmc.LOGWARNING)
            return False
    if previous != "true":
        _restore_later(previous)

    args = ["tmdb_play", "type={}".format("episode" if media_type == "episode" else "movie"),
            "title={}".format(quote(_clean(title)))]
    if year:
        args.append("year={}".format(int(year)))
    if media_type == "episode":
        s, e = int(season), int(episode)
        args += ["season={}".format(s), "episode={}".format(e),
                 "ep_season={}".format(s), "ep_episode={}".format(e)]
    builtin = "RunScript(special://home/addons/{}/addon.py,{})".format(ADDON_ID, ",".join(args))
    xbmc.log("Voice nzbdav: {}".format(builtin), xbmc.LOGINFO)
    xbmc.executebuiltin(builtin)
    return True
