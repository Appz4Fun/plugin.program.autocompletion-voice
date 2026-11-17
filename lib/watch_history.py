# -*- coding: utf8 -*-
"""
Read-only view of what was recently played, from Kodi's MyVideos database.

Covers library items AND plain files / plugin URLs played outside the library
(Kodi records lastPlayed, playCount and resume bookmarks for every file).
Opened with sqlite "mode=ro" — this module never writes to Kodi's database.
"""

import glob
import os
import re
import sqlite3

import xbmcvfs  # type: ignore

_EP_RE = re.compile(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})")
_VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv")
_SKIP = ("bingie_intro", "imdb.trailers", "special://", "/trailer")


def _db_path():
    files = glob.glob(xbmcvfs.translatePath("special://database/MyVideos*.db"))
    if not files:
        return None
    return sorted(files, key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0))[-1]


def display_name(path):
    """'The.Lord.of.the.Rings...S03E02.The.Gift...2160p...mkv' -> readable."""
    name = os.path.basename(path.rstrip("/")) or path
    name = re.sub(r"\.(mkv|mp4|avi|m4v|ts|mov|wmv)$", "", name, flags=re.I)
    m = _EP_RE.search(name)
    if m:
        show = name[: m.start()]
        rest = name[m.end():]
        ep_title = re.split(r"[. ](?:\d{3,4}p|WEB|AMZN|NF|DSNP|HMAX|ATVP|BluRay)", rest)[0]
        show = re.sub(r"[._]+", " ", show).strip(" -")
        show = re.sub(r"\b(19|20)\d\d$", "", show).strip()
        ep_title = re.sub(r"[._]+", " ", ep_title).strip(" -")
        out = "{} S{:02d}E{:02d}".format(show, int(m.group(1)), int(m.group(2)))
        return out + (" - " + ep_title if ep_title else "")
    name = re.split(r"[. ](?:\d{3,4}p|WEB|BluRay|x26[45]|H\.?26[45])", name)[0]
    return re.sub(r"[._]+", " ", name).strip()


def recent(limit=12):
    """Most recently played items, newest first.

    Each: {id, path, name, last_played, finished, resume_s, total_s}.
    """
    db = _db_path()
    if not db:
        return []
    try:
        con = sqlite3.connect("file:{}?mode=ro".format(db), uri=True, timeout=2)
        rows = con.execute(
            """SELECT p.strPath, f.strFilename, f.lastPlayed, f.playCount,
                      b.timeInSeconds, b.totalTimeInSeconds
               FROM files f JOIN path p ON p.idPath = f.idPath
               LEFT JOIN bookmark b ON b.idFile = f.idFile AND b.type = 1
               WHERE f.lastPlayed IS NOT NULL AND f.lastPlayed != ''
               ORDER BY f.lastPlayed DESC LIMIT ?""",
            (limit * 3,),
        ).fetchall()
        con.close()
    except Exception:
        return []
    out, seen = [], set()
    for spath, fname, last, plays, pos, total in rows:
        full = fname if "://" in fname else os.path.join(spath, fname)
        if any(s in full for s in _SKIP):
            continue
        name = display_name(fname)
        if name in seen:
            continue
        seen.add(name)
        out.append({
            "id": len(out) + 1,
            "path": full,
            "name": name,
            "last_played": last,
            "finished": bool(plays) and not pos,
            "resume_s": int(pos or 0),
            "total_s": int(total or 0),
        })
        if len(out) >= limit:
            break
    return out


def describe(items):
    lines = []
    for it in items:
        if it["resume_s"] and it["total_s"]:
            state = "stopped at {}:{:02d} of {}:{:02d}".format(
                it["resume_s"] // 60, it["resume_s"] % 60,
                it["total_s"] // 60, it["total_s"] % 60)
        elif it["finished"]:
            state = "finished"
        else:
            state = "started"
        lines.append("  [{}] {} ({}, {})".format(it["id"], it["name"], state, it["last_played"][:16]))
    return "\n".join(lines) or "  (none)"


def next_episode(path):
    """Find the following episode's file next to (or one folder up from) path."""
    if "://" in path:
        return None
    m = _EP_RE.search(os.path.basename(path)) or _EP_RE.search(path)
    if not m:
        return None
    season, ep = int(m.group(1)), int(m.group(2))
    base = os.path.basename(path)
    prefix = re.sub(r"[^a-z0-9]", "", base[: _EP_RE.search(base).start()].lower()) if _EP_RE.search(base) else ""
    if not prefix:
        return None
    folder = os.path.dirname(path)
    roots = [folder, os.path.dirname(folder)]
    targets = [(season, ep + 1), (season + 1, 1)]
    for want_s, want_e in targets:
        for root in roots:
            try:
                entries = os.listdir(root)
            except OSError:
                continue
            for entry in sorted(entries):
                em = _EP_RE.search(entry)
                if not em or (int(em.group(1)), int(em.group(2))) != (want_s, want_e):
                    continue
                if re.sub(r"[^a-z0-9]", "", entry[: em.start()].lower()) != prefix:
                    continue
                full = os.path.join(root, entry)
                if os.path.isfile(full) and full.lower().endswith(_VIDEO_EXT):
                    return full
                if os.path.isdir(full):
                    vids = [f for f in sorted(os.listdir(full)) if f.lower().endswith(_VIDEO_EXT)]
                    if vids:
                        return os.path.join(full, vids[0])
    return None


def episode_coords(path):
    """(show title, season, episode) parsed from a release file name, or None."""
    base = os.path.basename(path)
    m = _EP_RE.search(base)
    if not m:
        return None
    show = re.sub(r"[._]+", " ", base[: m.start()]).strip(" -")
    show = re.sub(r"\b(19|20)\d\d$", "", show).strip()
    return show, int(m.group(1)), int(m.group(2))
