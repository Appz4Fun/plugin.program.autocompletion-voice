#!/bin/bash
# Build a headless Kodi 21 test rig inside an OrbStack/Debian VM.
# Env: EL_KEY (ElevenLabs), AN_KEY (Anthropic).  Run as the VM user.
set -euo pipefail
E2E="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$E2E/../.." && pwd)"
K="$HOME/.kodi"; A="$K/addons"; U="$K/userdata"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg sqlite3 rsync >/dev/null
pkill -f kodi.bin 2>/dev/null || true; pkill -f Xvfb 2>/dev/null || true; sleep 2
rm -rf "$K" /tmp/remote; mkdir -p "$A" "$U/addon_data/plugin.program.autocompletion" /tmp/remote/press

# --- media: tiny 70-minute videos named like the user's releases ------------
if [ ! -f /media/base.mkv ]; then
  sudo mkdir -p /media && sudo chown "$USER" /media
  ffmpeg -loglevel error -f lavfi -i testsrc=size=64x36:rate=1 -f lavfi -i anullsrc=r=8000:cl=mono \
    -t 4200 -c:v libx264 -preset ultrafast -crf 51 -g 5 -c:a aac -b:a 8k -shortest /media/base.mkv
fi
rm -rf /media/downloads /media/nzbdav; mkdir -p /media/downloads /media/nzbdav
rel() { d="/media/downloads/$1 [NZBDAV copy 0$2]"; mkdir -p "$d"; cp /media/base.mkv "$d/$1.mkv"; }
rel "The.Lord.of.the.Rings.The.Rings.of.Power.S03E02.The.Gift.of.Men.2160p.AMZN.WEB-DL.DD.5.1.Atmos.DoVi.H.265-playWEB" 3
rel "The.Lord.of.the.Rings.The.Rings.of.Power.S03E03.The.Secret.Fire.2160p.AMZN.WEB-DL.DD+5.1.Atmos.DoVi.H.265-playWEB" 2
rel "Six.Feet.Under.S03E06.Making.Love.Work.1080p.AMZN.WEB-DL.DDP5.1.H.264-KAIZEN" 2
rel "The.Wheel.of.Time.S03E08.He.Who.Comes.With.The.Dawn.2160p.AMZN.WEB-DL.DDP5.1.DoVi.H.265" 1
cp /media/base.mkv "/media/nzbdav/Six.Feet.Under.S03E07.Timing.and.Space.1080p.AMZN.WEB-DL.mkv"

# --- add-ons: the real voice add-on, a Bingie-id skin, and stubs -------------
rsync -a --exclude tests --exclude .git --exclude HANDOFF.md "$REPO/" "$A/plugin.program.autocompletion/"
cp -r /usr/share/kodi/addons/skin.estuary "$A/skin.bingie"
sed -i 's/id="skin.estuary"/id="skin.bingie"/; s/name="Estuary"/name="Bingie (e2e mock)"/' "$A/skin.bingie/addon.xml"
rm -f "$A/skin.bingie/xml/Custom_1109_TopBarOverlay.xml"  # Estuary's id 1109 would clash
cp "$E2E/Custom_1109_BingieSearch.xml" "$A/skin.bingie/xml/"
cp -r "$E2E/addons/"* "$A/"

cat > "$U/guisettings.xml" <<X
<settings version="2">
  <setting id="services.webserver">true</setting>
  <setting id="services.webserverport">8080</setting>
  <setting id="services.webserverauthentication">false</setting>
  <setting id="addons.unknownsources">true</setting>
</settings>
X
cat > "$U/addon_data/plugin.program.autocompletion/settings.xml" <<X
<settings version="2">
    <setting id="stt_provider">elevenlabs</setting>
    <setting id="elevenlabs_api_key">${EL_KEY}</setting>
    <setting id="llm_provider">anthropic</setting>
    <setting id="llm_key_anthropic">${AN_KEY}</setting>
    <setting id="voice_commands">true</setting>
    <setting id="audio_source">ble</setting>
</settings>
X
echo "rig built"
