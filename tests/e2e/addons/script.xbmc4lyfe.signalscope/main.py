import json, sys, time
open("/tmp/remote/signalscope.log", "a").write(json.dumps({"t": time.time(), "runscript": sys.argv[1:]}) + "\n")
