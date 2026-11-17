import json, time, xbmc
class M(xbmc.Monitor):
    def onNotification(self, sender, method, data):
        if method.startswith("Other."):
            open("/tmp/remote/signalscope.log", "a").write(json.dumps(
                {"t": time.time(), "notify": method, "sender": sender}) + "\n")
m = M()
while not m.abortRequested():
    m.waitForAbort(1)
