# -*- coding: utf8 -*-
"""Floating mic button overlay for the Kodi virtual keyboard.

Shows a microphone button in the bottom-right corner of the screen when the
virtual keyboard is visible. Pressing it triggers voice input via the same
window property mechanism used by the BLE remote button.
"""

import os
import threading

try:
    import xbmc
    import xbmcgui

    _KODI_AVAILABLE = True
except ImportError:
    _KODI_AVAILABLE = False

# Button placement — bottom-right, above the keyboard
BUTTON_WIDTH = 60
BUTTON_HEIGHT = 60
BUTTON_X = 1200  # right side of 1280-wide screen
BUTTON_Y = 520  # above typical keyboard position

ACTION_SELECT = 7
ACTION_PREVIOUS_MENU = 10
ACTION_NAV_BACK = 92


def _activate_voice():
    """Set the voice activation window property."""
    xbmcgui.Window(10000).setProperty("voice_keyboard_activate", "1")
    xbmc.log("Voice keyboard: mic button overlay activated", xbmc.LOGINFO)


def _handle_overlay_action(overlay, action):
    """Handle an action on the mic button overlay."""
    action_id = action.getId()
    if action_id == ACTION_SELECT:
        _activate_voice()
    elif action_id in (ACTION_PREVIOUS_MENU, ACTION_NAV_BACK):
        overlay.close()


def _handle_overlay_control(overlay, control):
    """Handle a control click on the mic button overlay."""
    if control == overlay._button:
        _activate_voice()


class MicButtonOverlay(xbmcgui.WindowDialog):
    """Transparent overlay with a mic button for voice activation."""

    def __init__(self):
        super(MicButtonOverlay, self).__init__()
        self._active = False

        # Use addon icon as mic button image, or fall back to built-in
        addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        icon_path = os.path.join(addon_dir, "resources", "mic.png")
        if not os.path.exists(icon_path):
            icon_path = os.path.join(addon_dir, "resources", "icon.png")

        self._button = xbmcgui.ControlButton(
            BUTTON_X,
            BUTTON_Y,
            BUTTON_WIDTH,
            BUTTON_HEIGHT,
            "",
            focusTexture=icon_path,
            noFocusTexture=icon_path,
        )
        self.addControl(self._button)
        self.setFocus(self._button)

    def onControl(self, control):
        _handle_overlay_control(self, control)

    def onAction(self, action):
        _handle_overlay_action(self, action)


class MicButtonMonitor(object):
    """Monitors keyboard visibility and shows/hides the mic button overlay."""

    def __init__(self):
        self._overlay = None
        self._showing = False
        self._lock = threading.Lock()

    def update(self):
        """Call periodically from the service loop to show/hide the overlay."""
        keyboard_visible = bool(
            xbmc.getCondVisibility("Window.IsVisible(virtualkeyboard)")
        )

        with self._lock:
            if keyboard_visible and not self._showing:
                self._show()
            elif not keyboard_visible and self._showing:
                self._hide()

    def _show(self):
        try:
            self._overlay = MicButtonOverlay()
            self._overlay.show()
            self._showing = True
            xbmc.log("Voice keyboard: mic button overlay shown", xbmc.LOGINFO)
        except Exception as exc:
            xbmc.log(
                "Voice keyboard: mic button overlay failed: {}".format(exc),
                xbmc.LOGWARNING,
            )

    def _hide(self):
        try:
            if self._overlay:
                self._overlay.close()
                self._overlay = None
            self._showing = False
        except Exception:
            self._showing = False

    def close(self):
        """Clean up the overlay on shutdown."""
        with self._lock:
            self._hide()
