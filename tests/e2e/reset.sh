#!/bin/bash
# Restore the seeded watch history + stub settings, then (re)start Kodi.
E2E="$(cd "$(dirname "$0")" && pwd)"
"$E2E/kodi_ctl.sh" stop >/dev/null 2>&1
DB=$(ls ~/.kodi/userdata/Database/MyVideos*.db | sort -V | tail -1)
if [ ! -f ~/.e2e_db_snapshot ]; then
  sqlite3 "$DB" "DELETE FROM bookmark; DELETE FROM files; DELETE FROM path;"
  D=/media/downloads
  seed() { sqlite3 "$DB" "INSERT INTO path (strPath) VALUES ('$D/$1/');
      INSERT INTO files (idPath,strFilename,playCount,lastPlayed) VALUES (last_insert_rowid(),'$2',$4,'$3');"
    [ "$5" != 0 ] && sqlite3 "$DB" "INSERT INTO bookmark (idFile,timeInSeconds,totalTimeInSeconds,thumbNailImage,player,playerState,type)
      VALUES ((SELECT max(idFile) FROM files),$5,$6,'','VideoPlayer','',1);" || true; }
  for d in "$D"/*; do n=$(basename "$d"); f=$(ls "$d"); case "$n" in
    *Rings.of.Power.S03E02*) seed "$n" "$f" "2026-11-16 14:12:58" "NULL" 2189 4112 ;;
    *Six.Feet.Under.S03E06*) seed "$n" "$f" "2026-11-16 01:17:00" 1 0 0 ;;
    *Wheel.of.Time.S03E08*)  seed "$n" "$f" "2026-11-16 12:08:40" "NULL" 780 4228 ;;
  esac; done
  cp "$DB" ~/.e2e_db_snapshot
else
  cp ~/.e2e_db_snapshot "$DB"
fi
rm -f ~/.kodi/userdata/addon_data/plugin.video.nzbdav/settings.xml
rm -rf /tmp/remote/press/*; rm -f /tmp/remote/*.log /tmp/remote/log /tmp/remote/mic_*
"$E2E/kodi_ctl.sh" start
