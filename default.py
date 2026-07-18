# -*- coding: utf8 -*-

# Copyright (C) 2015 - Philipp Temminghoff <phil65@kodi.tv>
# This program is Free Software see LICENSE file for details

import sys
import xbmc
import xbmcaddon
import xbmcgui

ADDON = xbmcaddon.Addon()
ADDON_VERSION = ADDON.getAddonInfo("version")


xbmc.log("version %s started" % ADDON_VERSION)

if len(sys.argv) > 1 and sys.argv[1] == "voice":
    xbmcgui.Window(10000).setProperty("voice_keyboard_activate", "1")
    xbmc.log("voice activation triggered")
else:
    ADDON.openSettings()
    xbmc.log("finished")
