import json, sys, time, xbmcgui, xbmcplugin
open("/tmp/remote/plugin.video.youtube.log", "a").write(json.dumps({"t": time.time(), "query": sys.argv[2] if len(sys.argv) > 2 else ""}) + "\n")
h = int(sys.argv[1])
for n in ("Project Hail Mary", "The Wild Robot", "Dune: Part Two"):
    xbmcplugin.addDirectoryItem(h, "", xbmcgui.ListItem(n), False)
xbmcplugin.endOfDirectory(h)
