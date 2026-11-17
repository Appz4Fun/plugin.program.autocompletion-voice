#!/bin/bash
# Enable the rig's add-ons, select the Bingie-id skin, seed watch history.
set -e
E2E="$(cd "$(dirname "$0")" && pwd)"
J() { curl -s -m 10 -H 'Content-Type: application/json' -d "$1" http://127.0.0.1:8080/jsonrpc; echo; }
for a in plugin.program.autocompletion skin.bingie plugin.video.nzbdav script.xbmc4lyfe.signalscope plugin.video.tmdb.bingie.helper plugin.video.youtube script.e2e.exec; do
  J "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"Addons.SetAddonEnabled\",\"params\":{\"addonid\":\"$a\",\"enabled\":true}}" >/dev/null
done
"$E2E/kodi_ctl.sh" stop
DB=$(ls ~/.kodi/userdata/Database/MyVideos*.db | sort -V | tail -1)
D=/media/downloads
seed() { # dir file lastPlayed playCount resume total
  sqlite3 "$DB" "INSERT INTO path (strPath) VALUES ('$D/$1/');
    INSERT INTO files (idPath,strFilename,playCount,lastPlayed) VALUES (last_insert_rowid(),'$2',$4,'$3');"
  [ "$5" != 0 ] && sqlite3 "$DB" "INSERT INTO bookmark (idFile,timeInSeconds,totalTimeInSeconds,thumbNailImage,player,playerState,type)
    VALUES ((SELECT max(idFile) FROM files),$5,$6,'','VideoPlayer','',1);" || true
}
for d in "$D"/*; do n=$(basename "$d"); f=$(ls "$d"); case "$n" in
  *Rings.of.Power.S03E02*) seed "$n" "$f" "2026-11-16 14:12:58" "NULL" 2189 4112 ;;
  *Six.Feet.Under.S03E06*) seed "$n" "$f" "2026-11-16 01:17:00" 1 0 0 ;;
  *Wheel.of.Time.S03E08*)  seed "$n" "$f" "2026-11-16 12:08:40" "NULL" 780 4228 ;;
esac; done
python3 - <<P
import re,os
p=os.path.expanduser("~/.kodi/userdata/guisettings.xml"); s=open(p).read()
s=re.sub(r'<setting id="lookandfeel.skin"[^>]*>[^<]*</setting>','<setting id="lookandfeel.skin">skin.bingie</setting>',s)
if 'lookandfeel.skin' not in s: s=s.replace('</settings>','    <setting id="lookandfeel.skin">skin.bingie</setting>\n</settings>')
open(p,'w').write(s)
P
sqlite3 "$DB" "select strFilename, lastPlayed from files order by lastPlayed desc" | cut -c1-70
"$E2E/kodi_ctl.sh" start
J '{"jsonrpc":"2.0","id":1,"method":"Settings.GetSettingValue","params":{"setting":"lookandfeel.skin"}}'
J '{"jsonrpc":"2.0","id":1,"method":"Addons.GetAddons","params":{"enabled":true,"properties":["name"]}}' | grep -o '"addonid":"[^"]*\(nzbdav\|signalscope\|bingie\|autocompletion\|youtube\)[^"]*"'
grep -n "Voice keyboard" ~/.kodi/temp/kodi.log | tail -5
