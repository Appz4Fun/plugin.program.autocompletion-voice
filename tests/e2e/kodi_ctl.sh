#!/bin/bash
# start|stop headless Kodi with the fake bluetooth tools first on PATH
E2E="$(cd "$(dirname "$0")" && pwd)"
case "$1" in
  stop) curl -s -m 5 -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"Application.Quit"}' http://127.0.0.1:8080/jsonrpc >/dev/null
        sleep 4; pkill -f kodi.bin; pkill -f Xvfb; pkill -f "e2e/bin/btmon"; true ;;
  start) export PATH="$E2E/bin:$PATH" KODI_AE_SINK=NULL
        nohup xvfb-run -a -s "-screen 0 1280x720x24" kodi --standalone > /tmp/kodi-run.log 2>&1 &
        for i in $(seq 60); do curl -s -m 2 http://127.0.0.1:8080/jsonrpc -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"JSONRPC.Ping"}' | grep -q pong && { echo "kodi up"; exit 0; }; sleep 2; done
        echo "kodi failed to start"; tail -30 ~/.kodi/temp/kodi.log; exit 1 ;;
esac
