# -*- coding: utf8 -*-
"""Minimal fake xbmc / xbmcgui / xbmcaddon / xbmcvfs modules for unit tests.

Every builtin, JSON-RPC call and notification is recorded on ``KODI`` so tests
can assert exactly what would have been sent to Kodi.
"""

import json
import sys
import types


class FakeKodi(object):
    def __init__(self):
        self.reset()

    def reset(self):
        self.builtins = []
        self.rpc = []
        self.notifications = []
        self.logs = []
        self.cond = {}
        self.labels = {}
        self.settings = {}  # addon_id -> {key: value}
        self.addons = []  # [{"addonid", "name", "type"}]
        self.skin = "skin.bingie"
        self.paths = {}
        self.on_builtin = None
        self.on_rpc = None
        self.window_id = 10000

    def setting(self, addon_id, key):
        return self.settings.get(addon_id, {}).get(key, "")


KODI = FakeKodi()


def install():
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG, xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.LOGERROR = 0, 1, 2, 3
    xbmc.log = lambda msg, level=0: KODI.logs.append(msg)

    def executebuiltin(b, *a):
        KODI.builtins.append(b)
        if KODI.on_builtin:
            KODI.on_builtin(b)

    xbmc.executebuiltin = executebuiltin

    def executeJSONRPC(q):
        req = json.loads(q)
        KODI.rpc.append(req)
        if KODI.on_rpc:
            out = KODI.on_rpc(req)
            if out is not None:
                return json.dumps(out)
        if req.get("method") == "Addons.GetAddons":
            return json.dumps({"result": {"addons": KODI.addons}})
        return json.dumps({"result": "OK"})

    xbmc.executeJSONRPC = executeJSONRPC
    xbmc.getCondVisibility = lambda c: bool(KODI.cond.get(c, False))
    xbmc.getInfoLabel = lambda l: KODI.labels.get(l, "")
    xbmc.getSkinDir = lambda: KODI.skin
    xbmc.sleep = lambda ms: None

    class Monitor(object):
        def abortRequested(self):
            return True

        def waitForAbort(self, t=0):
            return True

    xbmc.Monitor = Monitor

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.NOTIFICATION_INFO = "info"
    xbmcgui.NOTIFICATION_WARNING = "warning"
    xbmcgui.NOTIFICATION_ERROR = "error"

    class Dialog(object):
        def notification(self, heading, message, *a, **k):
            KODI.notifications.append(message)

        def select(self, *a, **k):
            return 0

        def ok(self, *a, **k):
            return True

    class DialogProgressBG(object):
        def create(self, *a):
            pass

        def update(self, *a, **k):
            pass

        def close(self):
            pass

    class Window(object):
        _props = {}

        def __init__(self, wid=10000):
            pass

        def getProperty(self, k):
            return Window._props.get(k, "")

        def setProperty(self, k, v):
            Window._props[k] = v

        def clearProperty(self, k):
            Window._props.pop(k, None)

    xbmcgui.getCurrentWindowId = lambda: KODI.window_id
    xbmcgui.getCurrentWindowDialogId = lambda: 9999
    xbmcgui.Dialog = Dialog
    xbmcgui.DialogProgressBG = DialogProgressBG
    xbmcgui.Window = Window

    xbmcaddon = types.ModuleType("xbmcaddon")

    class Addon(object):
        def __init__(self, addon_id="plugin.program.autocompletion"):
            self.id = addon_id

        def getSetting(self, k):
            return KODI.setting(self.id, k)

        def setSetting(self, k, v):
            KODI.settings.setdefault(self.id, {})[k] = v
            hook = KODI.settings.get("__on_set__")
            if hook:
                hook(self.id, k, v)

        def getAddonInfo(self, k):
            return {"id": self.id, "version": "test", "path": ".", "profile": "."}.get(k, "")

    xbmcaddon.Addon = Addon

    xbmcvfs = types.ModuleType("xbmcvfs")

    def translatePath(p):
        for prefix, real in KODI.paths.items():
            if p.startswith(prefix):
                return real + p[len(prefix):]
        return p

    xbmcvfs.translatePath = translatePath

    sys.modules.update(xbmc=xbmc, xbmcgui=xbmcgui, xbmcaddon=xbmcaddon, xbmcvfs=xbmcvfs)
    return KODI
