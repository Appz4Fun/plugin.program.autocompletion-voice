# Live end-to-end test (headless Kodi 21 in an OrbStack VM)

Real Kodi + the real add-on; only the hardware and third-party add-ons are faked.

| Piece | Real or fake |
|---|---|
| Kodi 21.2, JSON-RPC, player, windows, skin strings | real (Debian, Xvfb) |
| This add-on (service, BLE parser, ADPCM decoder, STT, LLM, actions) | real |
| ElevenLabs STT, Anthropic Opus 5.5 | real API calls (keys via env) |
| UR02 remote | fake `bin/btmon` / `bin/busctl` / `bin/dbus-send` replaying speech encoded by `encode_utterance.py` (macOS `say` -> 8 kHz -> ATVV ADPCM frames) |
| Bingie search | Estuary copy with id `skin.bingie` + `Custom_1109_BingieSearch.xml` |
| nzbdav, SignalScope, TMDb Helper, YouTube | stub add-ons in `addons/` that log how they were called |
| Watch history / media | seeded MyVideos DB + tiny 70-min test videos |

```sh
orb create debian:trixie voice-e2e
orb -m voice-e2e sudo apt-get install -y kodi xvfb
orb -m voice-e2e bash -c "EL_KEY=... AN_KEY=... $PWD/tests/e2e/setup_vm.sh && $PWD/tests/e2e/kodi_ctl.sh start && $PWD/tests/e2e/seed.sh"
# then, per run:
orb -m voice-e2e bash -c "$PWD/tests/e2e/reset.sh && python3 $PWD/tests/e2e/run_e2e.py"
```
New phrases: `python3 tests/e2e/encode_utterance.py "text" tests/e2e/utterances/name.frames` (on macOS).
