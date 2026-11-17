# e2e stub of NeNeTeePee-Stream: record how voice invoked us and whether the
# picker would have been skipped (auto_select_best read from disk, exactly like
# the real script route), then "play the first release".
import json, os, re, sys, time
import xbmc, xbmcvfs
prof = xbmcvfs.translatePath("special://profile/addon_data/plugin.video.nzbdav/settings.xml")
try:
    flag = re.search(r'id="auto_select_best"[^>]*>([^<]*)<', open(prof).read()).group(1)
except Exception:
    flag = "missing"
rec = {"t": time.time(), "argv": sys.argv[1:], "auto_select_best_on_disk": flag,
       "picker_shown": flag != "true"}
open("/tmp/remote/nzbdav.log", "a").write(json.dumps(rec) + "\n")
if flag == "true":
    xbmc.Player().play("/media/nzbdav/Six.Feet.Under.S03E07.Timing.and.Space.1080p.AMZN.WEB-DL.mkv")
