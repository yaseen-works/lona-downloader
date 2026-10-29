"""Lona Downloader — public web app.

One-tap video downloader with the BlackHole-style UI.
Engines (all free, verified 2026-10-20):
  TikTok     -> tikwm API (HD, no watermark)
  Instagram  -> RapidAPI "Download All in One!" (key in server config only)
  X          -> fxtwitter / vxtwitter public APIs
  Reddit     -> per-post RSS + v.redd.it DASH (merged server-side)
  YouTube, Facebook, Pinterest, Vimeo, Dailymotion, SoundCloud -> yt-dlp

Run:  PORT=8000 ./venv/bin/python server.py     (PORT defaults to 8000)
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

BASE = Path(__file__).resolve().parent
INDEX_HTML = BASE / "index.html"
TOKEN_TTL = 600  # seconds

RAPIDAPI_HOST = "instagram-downloader-download-instagram-videos-stories1.p.rapidapi.com"
YT_API_HOST = "yt-api.p.rapidapi.com"  # YT-API by ytjar — free Basic plan (2026-10-23)


def load_rapidapi_key():
    key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if key:
        return key
    cfg = BASE / "server_config.json"
    if cfg.exists():
        try:
            return json.loads(cfg.read_text()).get("rapidapi_key", "").strip()
        except Exception:
            return ""
    return ""


RAPIDAPI_KEY = load_rapidapi_key()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": UA})

app = Flask(__name__)

# token -> job dict {kind, platform, title, thumbnail, ext, created, ...}
TOKENS = {}
TOKENS_LOCK = threading.Lock()

GENERIC_ERR = "Could not fetch that link. It may be private or deleted — or the platform is busy right now. Check the link and try again in a minute."
INSTAGRAM_ERR = "Instagram link nahi khula — ye private ho sakta hai, ya link ghalat hai. Public link try karo."
INSTAGRAM_LIMIT_ERR = "Instagram ki aaj ki free limit khatam ho gayi hai — kal phir try karo. Baaki platforms chal rahe hain."
YOUTUBE_ERR = "YouTube is blocking our server right now — please try again in a little while."
SNAPCHAT_ERR = "That Snapchat link could not be fetched. Only public Spotlight videos and public stories work."


def prune_tokens():
    now = time.time()
    with TOKENS_LOCK:
        for tok in [t for t, j in TOKENS.items() if now - j["created"] > TOKEN_TTL]:
            TOKENS.pop(tok, None)


def new_token(job):
    prune_tokens()
    tok = secrets.token_urlsafe(18)
    job["created"] = time.time()
    with TOKENS_LOCK:
        TOKENS[tok] = job
    return tok


# --------------------------------------------------------------------------
# Stats — Render's disk is ephemeral, so counters live in the free Abacus
# counter API. All counting is fire-and-forget: it can never break or slow
# a download. Recent downloads are kept in memory only (since restart).
# --------------------------------------------------------------------------

ABACUS_BASE = "https://abacus.jasoncameron.dev"
ABACUS_NS = "lona-downloader-stats-yw"
PKT = timezone(timedelta(hours=5))  # Asia/Karachi

RECENT_DOWNLOADS = deque(maxlen=20)
RECENT_LOCK = threading.Lock()

ADMIN_PLATFORMS = ["tiktok", "youtube", "instagram", "pinterest",
                   "x", "facebook", "reddit", "soundcloud", "snapchat"]


def pkt_now():
    return datetime.now(PKT)


def _abacus_hit(counter):
    try:
        requests.get(f"{ABACUS_BASE}/hit/{ABACUS_NS}/{counter}", timeout=3)
    except Exception:
        pass


def _abacus_get(counter):
    """Counter value, 0 if it doesn't exist yet, None if Abacus is down."""
    try:
        r = requests.get(f"{ABACUS_BASE}/get/{ABACUS_NS}/{counter}", timeout=4)
        if r.status_code == 404:
            return 0
        r.raise_for_status()
        return int(r.json().get("value", 0))
    except Exception:
        return None


def _fire_and_forget(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def count_visit():
    _fire_and_forget(_abacus_hit, "visits")


def count_failure(platform):
    def _hits():
        _abacus_hit("fails")
        _abacus_hit(f"f-{platform}")
    _fire_and_forget(_hits)


def count_ig_call():
    month = pkt_now().strftime("%Y-%m")
    _fire_and_forget(_abacus_hit, f"ig-{month}")


def count_download(platform, title):
    now = pkt_now()
    with RECENT_LOCK:
        RECENT_DOWNLOADS.appendleft({
            "time": now.strftime("%Y-%m-%d %H:%M"),
            "platform": platform,
            "title": (title or "")[:120],
        })
    day = now.strftime("%Y-%m-%d")

    def _hits():
        _abacus_hit("total")
        _abacus_hit(f"p-{platform}")
        _abacus_hit(f"d-{day}")

    _fire_and_forget(_hits)


def _admin_ok():
    key = os.environ.get("ADMIN_KEY", "")
    if not key:
        return False
    return secrets.compare_digest(request.args.get("key", ""), key)


ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lona Downloader — Stats</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#000; color:#fff; font-family:"Helvetica Neue", Helvetica, Arial, sans-serif;
         padding:28px 18px 48px; }
  .wrap { max-width:560px; margin:0 auto; }
  h1 { font-family:Georgia, "Times New Roman", serif; font-weight:400; text-align:center;
       font-size:26px; letter-spacing:8px; color:#e9c766; }
  .sub { text-align:center; color:#9a9a9a; font-size:13px; letter-spacing:3px; margin:8px 0 26px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:26px; }
  .card { background:#0d0d0d; border:1px solid #2c2c2c; border-radius:16px; padding:20px 16px; }
  .card .num { font-size:42px; font-weight:200; color:#e9c766; line-height:1.1; }
  .card .lbl { color:#9a9a9a; font-size:12.5px; letter-spacing:1.5px; margin-top:6px; }
  h2 { font-size:14px; font-weight:400; letter-spacing:3px; color:#e9c766; margin:0 0 14px; }
  .prow { display:flex; align-items:center; gap:12px; margin-bottom:11px; }
  .prow .name { width:92px; flex:none; font-size:14px; color:#e8e8e8; }
  .prow .track { flex:1; height:10px; background:#1d1d1d; border-radius:5px; overflow:hidden; }
  .prow .bar { height:100%; background:linear-gradient(90deg, #a97f27, #e9c766); border-radius:5px; }
  .prow .val { width:52px; flex:none; text-align:right; font-size:14px; color:#e9c766; }
  .panel { background:#0d0d0d; border:1px solid #2c2c2c; border-radius:16px; padding:22px 18px;
           margin-bottom:26px; }
  .ritem { padding:9px 0; border-bottom:1px solid #1d1d1d; font-size:14px; }
  .ritem:last-child { border-bottom:none; }
  .ritem .when { color:#9a9a9a; font-size:12.5px; }
  .ritem .plat { color:#e9c766; }
  .muted { color:#9a9a9a; font-size:12.5px; }
  .err { color:#ff8a80; text-align:center; margin-bottom:18px; display:none; }
</style>
</head>
<body>
<div class="wrap">
  <h1>LONA</h1>
  <div class="sub">DOWNLOADER &mdash; OWNER STATISTICS</div>
  <div class="err" id="err">Could not load the counters &mdash; showing what we have.</div>
  <div class="grid">
    <div class="card"><div class="num" id="total">&mdash;</div><div class="lbl">TOTAL DOWNLOADS</div></div>
    <div class="card"><div class="num" id="today">&mdash;</div><div class="lbl">TODAY (PKT)</div></div>
    <div class="card"><div class="num" id="last7">&mdash;</div><div class="lbl">LAST 7 DAYS</div></div>
    <div class="card"><div class="num" id="visits">&mdash;</div><div class="lbl">VISITS</div></div>
  </div>
  <div class="panel">
    <h2>BY PLATFORM</h2>
    <div id="platforms"></div>
  </div>
  <div class="panel">
    <h2>RECENT DOWNLOADS</h2>
    <div class="muted" style="margin-bottom:8px">(since server restart)</div>
    <div id="recent"></div>
  </div>
</div>
<script>
  var NAMES = {tiktok:"TikTok", youtube:"YouTube", instagram:"Instagram", pinterest:"Pinterest",
               x:"X", facebook:"Facebook", reddit:"Reddit", soundcloud:"SoundCloud", snapchat:"Snapchat"};
  var ORDER = ["tiktok","youtube","instagram","pinterest","x","facebook","reddit","soundcloud","snapchat"];
  function esc(s) { var d = document.createElement("div"); d.textContent = s || ""; return d.innerHTML; }
  function show(id, v) { document.getElementById(id).textContent = (v === null || v === undefined) ? "\\u2014" : v; }
  var key = new URLSearchParams(location.search).get("key") || "";
  fetch("/admin/data?key=" + encodeURIComponent(key))
    .then(function (r) { return r.json(); })
    .then(function (d) {
      show("total", d.total); show("today", d.today); show("last7", d.last7); show("visits", d.visits);
      var max = 0;
      ORDER.forEach(function (p) { var v = (d.platforms || {})[p]; if (v && v > max) max = v; });
      var html = "";
      ORDER.forEach(function (p) {
        var v = (d.platforms || {})[p];
        var fv = (d.platform_fails || {})[p];
        var pct = (v && max) ? Math.max(2, Math.round(v / max * 100)) : 0;
        html += '<div class="prow"><div class="name">' + NAMES[p] + '</div>' +
                '<div class="track"><div class="bar" style="width:' + pct + '%"></div></div>' +
                '<div class="val">' + ((v === null || v === undefined) ? "\\u2014" : v) +
                '' + '</div></div>';
      });
      if (d.ig_month !== null && d.ig_month !== undefined) {
        var cap = d.ig_month_cap || 500;
        var ipct = Math.min(100, Math.round(d.ig_month / cap * 100));
        document.getElementById("platforms").innerHTML +=
          '<div style="margin-top:16px"><h2>INSTAGRAM QUOTA (THIS MONTH)</h2>' +
          '<div class="prow"><div class="name">Used</div>' +
          '<div class="track"><div class="bar" style="width:' + ipct + '%"></div></div>' +
          '<div class="val">' + d.ig_month + ' / ~' + cap + '</div></div>' +
          '<div class="muted">Approx. downloads served by the free Instagram API this month.</div></div>';
      }
      document.getElementById("platforms").innerHTML = html;
      var rec = d.recent || [];
      document.getElementById("recent").innerHTML = rec.length ? rec.map(function (e) {
        return '<div class="ritem"><span class="when">' + esc(e.time) + ' PKT</span><br>' +
               '<span class="plat">' + esc(NAMES[e.platform] || e.platform) + '</span> &mdash; ' + esc(e.title) + '</div>';
      }).join("") : '<div class="muted">No downloads yet since this server started.</div>';
    })
    .catch(function () { document.getElementById("err").style.display = "block"; });
</script>
</body>
</html>
"""


def detect_platform(url):
    u = url.lower()
    host = urllib.parse.urlparse(u).netloc
    def has(*parts):
        return any(p in host for p in parts)
    if has("tiktok.com"):
        return "tiktok"
    if has("instagram.com"):
        return "instagram"
    if has("youtube.com", "youtu.be"):
        return "youtube"
    if has("facebook.com", "fb.watch", "fb.com"):
        return "facebook"
    if has("soundcloud.com"):
        return "soundcloud"
    if has("reddit.com", "redd.it"):
        return "reddit"
    if has("pinterest.", "pin.it"):
        return "pinterest"
    if has("x.com", "twitter.com"):
        return "x"
    if has("snapchat.com"):
        return "snapchat"
    if has("vimeo.com"):
        return "vimeo"
    if has("dailymotion.com", "dai.ly"):
        return "dailymotion"
    return None


PLATFORM_NAMES = {
    "tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube",
    "facebook": "Facebook", "soundcloud": "SoundCloud", "reddit": "Reddit",
    "pinterest": "Pinterest", "x": "X", "vimeo": "Vimeo", "snapchat": "Snapchat",
    "dailymotion": "Dailymotion",
}

EXT_MIME = {
    "mp4": "video/mp4", "mp3": "audio/mpeg", "jpg": "image/jpeg",
    "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
    "m4a": "audio/mp4", "webm": "video/webm",
}


def media_id(platform, url):
    pats = {
        "youtube": r"(?:v=|youtu\.be/|shorts/)([\w-]{6,})",
        "instagram": r"(?:reel|p|reels|stories)/([\w-]+)",
        "x": r"status/(\d+)",
        "reddit": r"(?:comments|v\.redd\.it)/([a-z0-9]+)",
        "pinterest": r"pin/(\d+)",
        "tiktok": r"video/(\d+)",
        "vimeo": r"vimeo\.com/(\d+)",
        "dailymotion": r"(?:video|dai\.ly)/([a-z0-9]+)",
        "facebook": r"(?:v=|videos/|watch\?v=)(\d+)",
        "soundcloud": r"soundcloud\.com/[^/]+/([^/?#]+)",
        "snapchat": r"(?:spotlight|story|add)/([\w.-]+)",
    }
    pat = pats.get(platform)
    if pat:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return secrets.token_hex(4)


# --------------------------------------------------------------------------
# Resolvers — each returns a job dict or raises ValueError(friendly message)
# --------------------------------------------------------------------------

def resolve_tiktok(url):
    # Primary: tikwm API (HD, no watermark). Some datacenter IPs (like our
    # host's) intermittently get a non-JSON block page from tikwm — retry once
    # in case the block is transient, then fall back to yt-dlp extraction.
    for _attempt in range(2):
        if _attempt:
            time.sleep(3)
        try:
            api = "https://www.tikwm.com/api/?url=" + urllib.parse.quote(url, safe="") + "&hd=1"
            r = HTTP.get(api, timeout=30)
            d = r.json()
            if d.get("code") == 0 and d.get("data"):
                data = d["data"]
                play = data.get("hdplay") or data.get("play")
                if play:
                    return {"kind": "direct", "url": play, "referer": "https://www.tiktok.com/",
                            "title": (data.get("title") or "TikTok video").strip(), "ext": "mp4",
                            "thumbnail": data.get("cover") or data.get("origin_cover")}
                break  # valid response but no play URL — retrying won't help
        except Exception:
            continue
    return resolve_ytdlp("tiktok", url)


def resolve_instagram(url):
    try:
        return _resolve_instagram_api(url)
    except ValueError as e:
        if str(e) == INSTAGRAM_LIMIT_ERR:
            raise
        # spare route: yt-dlp's own Instagram extractor (works for some
        # public posts without any API quota)
        try:
            return resolve_ytdlp("instagram", url)
        except Exception:
            raise e


def _resolve_instagram_api(url):
    if not RAPIDAPI_KEY:
        raise ValueError("Instagram is not configured on this server yet.")
    api = f"https://{RAPIDAPI_HOST}/fetch?url=" + urllib.parse.quote(url, safe="")
    data = None
    for _attempt in range(2):  # the free API can be slow — one retry
        try:
            count_ig_call()
            r = HTTP.get(api, headers={"x-rapidapi-host": RAPIDAPI_HOST,
                                        "x-rapidapi-key": RAPIDAPI_KEY}, timeout=90)
            if r.status_code == 429:
                raise ValueError(INSTAGRAM_LIMIT_ERR)
            if r.status_code != 200:
                raise ValueError(INSTAGRAM_ERR)
            data = r.json()
            break
        except ValueError:
            raise
        except Exception:
            data = None
    if not data or not data.get("ok") or not data.get("download_url"):
        raise ValueError(INSTAGRAM_ERR)
    is_image = data.get("type") == "image"
    caption = (data.get("caption") or "").strip()
    title = caption.split("\n")[0][:200] if caption else ("Instagram photo" if is_image else "Instagram video")
    return {"kind": "direct", "url": data["download_url"],
            "title": title, "ext": "jpg" if is_image else "mp4",
            "thumbnail": data.get("thumbnail_url") or (data["download_url"] if is_image else None)}


def resolve_x(url):
    m = re.search(r"(?:x\.com|twitter\.com)/([^/]+)/status/(\d+)", url)
    if not m:
        raise ValueError(GENERIC_ERR)
    user, tid = m.group(1), m.group(2)
    # fxtwitter (verified route)
    try:
        r = HTTP.get(f"https://api.fxtwitter.com/{user}/status/{tid}", timeout=30)
        tweet = r.json().get("tweet") or {}
        media = tweet.get("media") or {}
        videos = media.get("videos") or []
        best, best_area = None, -1
        for v in videos:
            variants = v.get("variants") or [{"url": v.get("url"), "bitrate": 0}]
            for var in variants:
                vu = var.get("url") or ""
                if ".mp4" in vu:
                    area = (v.get("width") or 0) * (v.get("height") or 0) + (var.get("bitrate") or 0)
                    if area > best_area:
                        best, best_area = vu, area
        if best:
            text = (tweet.get("text") or "X video").strip()
            return {"kind": "direct", "url": best,
                    "title": text.split("\n")[0][:200] or "X video", "ext": "mp4",
                    "thumbnail": (videos[0].get("thumbnail_url") if videos else None)}
    except ValueError:
        raise
    except Exception:
        pass
    # vxtwitter (backup)
    try:
        r = HTTP.get(f"https://api.vxtwitter.com/{user}/status/{tid}", timeout=30)
        d = r.json()
        mp4s = [u for u in (d.get("mediaURLs") or []) if ".mp4" in u]
        if mp4s:
            return {"kind": "direct", "url": mp4s[0],
                    "title": (d.get("text") or "X video").split("\n")[0][:200], "ext": "mp4",
                    "thumbnail": d.get("media_extended", [{}])[0].get("thumbnail_url") if d.get("media_extended") else None}
    except Exception:
        pass
    raise ValueError(GENERIC_ERR)


def resolve_reddit(url):
    vid = None
    title = "Reddit video"
    thumbnail = None
    mv = re.search(r"v\.redd\.it/([a-z0-9]+)", url)
    if mv:
        vid = mv.group(1)
    else:
        m = re.search(r"reddit\.com/r/([^/]+)/comments/([a-z0-9]+)", url)
        if not m:
            raise ValueError(GENERIC_ERR)
        sub, pid = m.group(1), m.group(2)
        rss_url = f"https://www.reddit.com/r/{sub}/comments/{pid}/.rss"
        try:
            r = HTTP.get(rss_url, timeout=30)
            if r.status_code != 200:
                raise ValueError(GENERIC_ERR)
            text = r.text
        except ValueError:
            raise
        except Exception:
            raise ValueError(GENERIC_ERR)
        mv2 = re.search(r"v\.redd\.it/([a-z0-9]+)", text)
        if not mv2:
            raise ValueError("That Reddit post has no video.")
        vid = mv2.group(1)
        mt = re.search(r"<entry>.*?<title>(.*?)</title>", text, re.S)
        if mt:
            title = re.sub(r"<[^>]+>", "", mt.group(1)).strip()[:200] or title
        mth = re.search(r'media:thumbnail[^>]*url="([^"]+)"', text)
        if mth:
            thumbnail = mth.group(1).replace("&amp;", "&")
    # DASH playlist: pick best video <=1080p and best audio representation
    mpd_url = f"https://v.redd.it/{vid}/DASHPlaylist.mpd"
    try:
        r = HTTP.get(mpd_url, timeout=30)
        if r.status_code != 200:
            raise ValueError(GENERIC_ERR)
        root = ET.fromstring(r.content)
    except ValueError:
        raise
    except Exception:
        raise ValueError(GENERIC_ERR)
    ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
    best_v, best_a = None, None  # (score, url)
    for rep in root.iter("{urn:mpeg:dash:schema:mpd:2011}Representation"):
        base = rep.findtext("mpd:BaseURL", default="", namespaces=ns) or ""
        if "CMAF" not in base:
            m2 = re.search(r"(CMAF[^<]+)", base)
            base = m2.group(1) if m2 else base
        bw = int(rep.get("bandwidth") or 0)
        h = int(rep.get("height") or 0)
        full = f"https://v.redd.it/{vid}/{base}"
        if base.endswith(".mp4") and "AUDIO" not in base:
            score = (min(h, 1080), bw)
            if best_v is None or score > best_v[0]:
                best_v = (score, full)
        elif "AUDIO" in base:
            if best_a is None or bw > best_a[0]:
                best_a = (bw, full)
    if not best_v:
        raise ValueError(GENERIC_ERR)
    return {"kind": "merge", "video_url": best_v[1],
            "audio_url": best_a[1] if best_a else None,
            "title": title, "ext": "mp4", "thumbnail": thumbnail}


YTDLP_FORMATS = {
    "snapchat": "b/best",
    "instagram": "bv*[height<=720]+ba/b/best",
    "youtube": "bv*[height<=720]+ba/b[height<=720]/bv+ba/b",
    "facebook": "bv*[height<=720]+ba/b/bv+ba/b",
    "vimeo": "bv*[height<=720]+ba/b/bv+ba/b",
    "dailymotion": "bv*[height<=720]+ba/b/bv+ba/b",
    "pinterest": "b[ext=mp4]/bv*[height<=720]+ba/b",
    "soundcloud": "bestaudio/best",
    "x": "bv*[height<=720]+ba/b",
    "reddit": "bv+ba/b",
    "tiktok": "b",
}


YT_CLIENTS = ["android", "tv", "web_embedded", "ios", "web"]


def ytdlp_extract(url, platform=None):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 30, "retries": 2, "nocheckcertificate": True}
    if platform == "youtube":
        last = None
        for client in YT_CLIENTS:
            o = dict(opts)
            o["extractor_args"] = {"youtube": {"player_client": [client]}}
            try:
                with yt_dlp.YoutubeDL(o) as ydl:
                    info = ydl.extract_info(url, download=False)
                if info:
                    return info
            except Exception as e:
                last = e
        if last:
            raise last
        return None
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def resolve_ytdlp(platform, url):
    try:
        info = ytdlp_extract(url, platform=platform)
    except Exception:
        raise ValueError(YOUTUBE_ERR if platform == "youtube" else GENERIC_ERR)
    if not info:
        raise ValueError(GENERIC_ERR)
    title = (info.get("title") or PLATFORM_NAMES[platform] + " media").strip()[:200]
    thumbnail = info.get("thumbnail")
    # Pinterest image pins: extractor may return a lone image URL
    if platform == "pinterest" and not info.get("formats"):
        u = info.get("url") or ""
        ext = (u.split("?")[0].rsplit(".", 1)[-1] or "jpg").lower()
        if ext in ("jpg", "jpeg", "png", "webp"):
            return {"kind": "direct", "url": u, "title": title,
                    "ext": ext, "thumbnail": u}
    return {"kind": "ytdlp", "source": url, "format": YTDLP_FORMATS[platform],
            "title": title, "ext": "mp3" if platform == "soundcloud" else "mp4",
            "thumbnail": thumbnail}


INVIDIOUS_INSTANCES = ["invidious.f5.si", "invidious.nerdvpn.de",
                       "invidious.0011.lt"]


def resolve_youtube(url):
    """yt-dlp hits YouTube's bot wall from most server IPs, so resolve via a
    public Invidious instance's API (muxed formatStreams), yt-dlp fallback."""
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{6,})", url)
    if m:
        vid = m.group(1)
        for inst in INVIDIOUS_INSTANCES:
            try:
                r = HTTP.get(f"https://{inst}/api/v1/videos/{vid}", timeout=8)
                if r.status_code != 200:
                    continue
                d = r.json()
                streams = [s for s in (d.get("formatStreams") or []) if s.get("url")]
                if not streams:
                    continue

                def _h(s):
                    mm = re.search(r"(\d+)p", s.get("qualityLabel") or "")
                    return int(mm.group(1)) if mm else 0

                best = max(streams, key=_h)
                thumb = None
                thumbs = d.get("videoThumbnails") or []
                for q in ("maxres", "sddefault", "high", "medium", "default"):
                    for t in thumbs:
                        if t.get("quality") == q and t.get("url"):
                            thumb = t["url"]
                            break
                    if thumb:
                        break
                if not thumb and thumbs:
                    thumb = thumbs[0].get("url")
                if thumb and thumb.startswith("/"):
                    thumb = f"https://{inst}{thumb}"
                return {"kind": "direct", "url": best["url"],
                        "title": (d.get("title") or "YouTube video").strip()[:200],
                        "ext": "mp4", "thumbnail": thumb}
            except Exception:
                continue
    api_job = resolve_youtube_api(vid) if m else None
    if api_job:
        return api_job
    try:
        return resolve_ytdlp("youtube", url)
    except ValueError:
        raise ValueError(YOUTUBE_ERR)


def resolve_youtube_api(vid):
    """YT-API (RapidAPI, free plan, 300 req/mo, 2 units per /dl call).
    Only used when the free Invidious route fails, so the quota lasts.
    Returns a job dict or None."""
    if not RAPIDAPI_KEY or not vid:
        return None
    try:
        month = pkt_now().strftime("%Y-%m")
        _fire_and_forget(_abacus_hit, f"ytapi-{month}")
        r = HTTP.get(f"https://{YT_API_HOST}/dl",
                     params={"id": vid, "cgeo": "DE"},
                     headers={"x-rapidapi-host": YT_API_HOST,
                              "x-rapidapi-key": RAPIDAPI_KEY}, timeout=60)
        if r.status_code != 200:
            return None
        d = r.json()
        if (d.get("status") or "").upper() != "OK":
            return None
        formats = [f for f in (d.get("formats") or []) if f.get("url")]
        if not formats:
            return None
        # progressive MP4s (audio+video in one file): prefer itag 22 (720p),
        # then 18 (360p), then the highest listed progressive format
        def score(f):
            itag = f.get("itag")
            if itag == 22:
                return 3
            if itag == 18:
                return 2
            mt = f.get("mimeType") or ""
            return 1 if ("mp4a" in mt and "avc1" in mt) else 0
        best = max(formats, key=score)
        if score(best) == 0:
            return None
        return {"kind": "direct", "url": best["url"],
                "title": (d.get("title") or "YouTube video").strip()[:200],
                "ext": "mp4",
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"}
    except Exception:
        return None


def resolve_snapchat(url):
    # yt-dlp's SnapchatSpotlight extractor (public Spotlight / stories)
    try:
        info = ytdlp_extract(url, platform="snapchat")
    except Exception:
        raise ValueError(SNAPCHAT_ERR)
    if not info:
        raise ValueError(SNAPCHAT_ERR)
    title = (info.get("title") or "Snapchat video").strip()[:200]
    u = info.get("url") or ""
    if u and ".mp4" in u.split("?")[0]:
        return {"kind": "direct", "url": u, "title": title, "ext": "mp4",
                "thumbnail": info.get("thumbnail")}
    return {"kind": "ytdlp", "source": url, "format": "b/best",
            "title": title, "ext": "mp4", "thumbnail": info.get("thumbnail")}


def resolve_vimeo(url):
    """Player-config route first (progressive MP4s, no login for public
    embeddable videos); yt-dlp fallback. Note: Vimeo bot-walls some server
    IPs entirely — then the honest error stands."""
    m = re.search(r"vimeo\.com/(\d+)", url)
    if m:
        vid = m.group(1)
        try:
            r = HTTP.get(f"https://player.vimeo.com/video/{vid}/config",
                         headers={"Referer": f"https://vimeo.com/{vid}"}, timeout=30)
            if r.status_code == 200:
                d = r.json()
                files = ((d.get("request") or {}).get("files") or {})
                prog = [p for p in (files.get("progressive") or []) if p.get("url")]
                if prog:
                    def _score(p):
                        h = p.get("height") or 0
                        return (1 if h <= 720 else 0, h if h <= 720 else -h)
                    best = max(prog, key=_score)
                    video = d.get("video") or {}
                    thumbs = video.get("thumbs") or {}
                    thumb = None
                    if thumbs:
                        thumb = thumbs[sorted(thumbs, key=lambda k: int(k))[-1]]
                    return {"kind": "direct", "url": best["url"],
                            "title": (video.get("title") or "Vimeo video").strip()[:200],
                            "ext": "mp4", "thumbnail": thumb}
        except Exception:
            pass
    return resolve_ytdlp("vimeo", url)


def resolve_dailymotion(url):
    """Player metadata route: qualities.auto is an HLS manifest that yt-dlp
    downloads and remuxes to MP4. yt-dlp page extraction is the fallback."""
    m = re.search(r"(?:video|dai\.ly)/([a-z0-9]+)", url)
    if m:
        vid = m.group(1)
        try:
            r = HTTP.get(f"https://www.dailymotion.com/player/metadata/video/{vid}",
                         timeout=30)
            if r.status_code == 200:
                d = r.json()
                qualities = d.get("qualities") or {}
                m3u8 = None
                for key in ("auto", "1080", "720", "480", "380", "240"):
                    for item in qualities.get(key) or []:
                        if item.get("url"):
                            m3u8 = item["url"]
                            break
                    if m3u8:
                        break
                if m3u8:
                    return {"kind": "ytdlp", "source": m3u8, "format": "b/best",
                            "title": (d.get("title") or "Dailymotion video").strip()[:200],
                            "ext": "mp4",
                            "thumbnail": f"https://www.dailymotion.com/thumbnail/video/{vid}"}
        except Exception:
            pass
    return resolve_ytdlp("dailymotion", url)


RESOLVERS = {
    "tiktok": resolve_tiktok,
    "instagram": resolve_instagram,
    "snapchat": resolve_snapchat,
    "x": resolve_x,
    "reddit": resolve_reddit,
    "youtube": resolve_youtube,
    "vimeo": resolve_vimeo,
    "dailymotion": resolve_dailymotion,
}


RESOLVE_CACHE = {}
RESOLVE_CACHE_LOCK = threading.Lock()
RESOLVE_TTL = 3 * 3600  # direct CDN URLs rot; 3h is a safe working window
RESOLVE_CACHE_MAX = 500


def _cache_get(key):
    now = time.time()
    with RESOLVE_CACHE_LOCK:
        ent = RESOLVE_CACHE.get(key)
        if ent and now - ent[0] < RESOLVE_TTL:
            return dict(ent[1])
    return None


def _cache_put(key, job):
    with RESOLVE_CACHE_LOCK:
        if len(RESOLVE_CACHE) >= RESOLVE_CACHE_MAX:
            oldest = sorted(RESOLVE_CACHE.items(), key=lambda kv: kv[1][0])[:100]
            for k, _ in oldest:
                RESOLVE_CACHE.pop(k, None)
        RESOLVE_CACHE[key] = (time.time(), dict(job))


def resolve_url(platform, url):
    key = f"{platform}-{media_id(platform, url)}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    job = _resolve_url_fresh(platform, url)
    _cache_put(key, job)
    return job


def _resolve_url_fresh(platform, url):
    if platform in RESOLVERS:
        try:
            return RESOLVERS[platform](url)
        except ValueError:
            raise
        except Exception:
            raise ValueError(GENERIC_ERR)
    return resolve_ytdlp(platform, url)


# --------------------------------------------------------------------------
# Download workers
# --------------------------------------------------------------------------

def http_download_to_file(url, dest, referer=None):
    headers = {"Referer": referer} if referer else {}
    with HTTP.get(url, stream=True, timeout=120, headers=headers) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(256 * 1024):
                if chunk:
                    f.write(chunk)
    return dest.stat().st_size


def ytdlp_download_to_dir(source, fmt, platform, tmpdir):
    import yt_dlp
    outtmpl = str(Path(tmpdir) / "media.%(ext)s")
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 30, "retries": 3, "outtmpl": outtmpl,
            "format": fmt, "nocheckcertificate": True}
    if platform == "soundcloud":
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                   "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        opts["merge_output_format"] = "mp4"
    if platform == "youtube":
        last = None
        for client in YT_CLIENTS:
            o = dict(opts)
            o["extractor_args"] = {"youtube": {"player_client": [client]}}
            try:
                with yt_dlp.YoutubeDL(o) as ydl:
                    ydl.download([source])
                last = None
                break
            except Exception as e:
                last = e
        if last:
            raise last
    else:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([source])
    files = [p for p in Path(tmpdir).iterdir()
             if p.is_file() and not p.name.endswith((".part", ".ytdl", ".temp"))]
    files = [p for p in files if p.stat().st_size > 0]
    if not files:
        raise ValueError(GENERIC_ERR)
    return max(files, key=lambda p: p.stat().st_size)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    count_visit()
    return send_file(INDEX_HTML)


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


@app.route("/e869b3bb1b342ff19fdba9b349212ebc.txt")
def indexnow_key():
    return Response("e869b3bb1b342ff19fdba9b349212ebc", mimetype="text/plain")


@app.route("/robots.txt")
def robots_txt():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "\n"
        "Sitemap: https://lona-downloader.onrender.com/sitemap.xml\n"
    )
    return Response(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    pages = [("https://lona-downloader.onrender.com/", "2026-10-22", "1.0")]
    pages += [
        (f"https://lona-downloader.onrender.com/{s}", "2026-10-22", "0.9")
        for s in SEO_SLUGS
    ]
    pages += [
        (f"https://lona-downloader.onrender.com/{t}", "2026-10-23", "0.5")
        for t in ("about", "contact", "privacy-policy")
    ]
    pages += [
        ("https://lona-downloader.onrender.com/blog", "2026-10-26", "0.6")
    ]
    pages += [
        (f"https://lona-downloader.onrender.com/blog/{s}", "2026-10-26", "0.7")
        for s in ("whatsapp-deleted-messages", "tiktok-without-watermark", "whatsapp-status-saver")
    ]
    items = "".join(
        f"<url><loc>{u}</loc><lastmod>{d}</lastmod><priority>{p}</priority></url>"
        for u, d, p in pages
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + items
        + "</urlset>"
    )
    return Response(body, mimetype="application/xml")


SEO_SLUGS = [
    "pinterest-video-downloader",
    "tiktok-video-downloader",
    "instagram-reel-downloader",
    "instagram-photo-downloader",
    "facebook-video-downloader",
    "reddit-video-downloader",
    "twitter-video-downloader",
    "soundcloud-downloader",
    "youtube-video-downloader",
    "snapchat-video-downloader",
    "snapchat-spotlight-downloader",
    "snapchat-story-downloader",
]


def _seo_page(slug):
    p = BASE / f"{slug}.html"
    if p.exists():
        count_visit()
        return send_file(p)
    return ("", 404)


for _slug in SEO_SLUGS:
    app.add_url_rule(
        f"/{_slug}", endpoint=f"seo_{_slug}",
        view_func=(lambda s: lambda: _seo_page(s))(_slug),
    )


TRUST_PAGES = {"about": "about.html", "contact": "contact.html", "privacy-policy": "privacy.html"}


def _trust_page(slug):
    p = BASE / TRUST_PAGES[slug]
    if p.exists():
        return send_file(p)
    return ("", 404)


for _t in TRUST_PAGES:
    app.add_url_rule(f"/{_t}", endpoint=f"trust_{_t}", view_func=(lambda s: lambda: _trust_page(s))(_t))


def _blog_page(slug):
    if slug in ("", "/"):
        p = BASE / "blog" / "index.html"
    else:
        if "/" in slug or "\\" in slug or slug.startswith("."):
            return ("", 404)
        p = BASE / "blog" / f"{slug}.html"
    if p.exists():
        return send_file(p)
    return ("", 404)


app.add_url_rule("/blog", endpoint="blog_index", view_func=lambda: _blog_page(""))
app.add_url_rule("/blog/", endpoint="blog_index_slash", view_func=lambda: _blog_page(""))
app.add_url_rule("/blog/<slug>", endpoint="blog_article", view_func=lambda slug: _blog_page(slug))


@app.route("/download-app")
def download_app():
    p = BASE / "LonaDownloader.apk"
    if p.exists():
        return send_file(p, as_attachment=True,
                         download_name="LonaDownloader.apk",
                         mimetype="application/vnd.android.package-archive")
    return ("", 404)

@app.route("/download-plus")
def download_plus():
    p = BASE / "LonaPlus.apk"
    if p.exists():
        return send_file(p, as_attachment=True,
                         download_name="LonaPlus.apk",
                         mimetype="application/vnd.android.package-archive")
    return ("Lona Plus is not available right now.", 404)

@app.route("/og-image.png")
def og_image():
    p = BASE / "og-image.png"
    if p.exists():
        return send_file(p, mimetype="image/png")
    return ("", 404)


@app.route("/api/resolve", methods=["POST"])
def api_resolve():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    m = re.search(r"https?://[^\s<>\"']+", url)
    if not m:
        return jsonify(ok=False, error="No link found. Copy a video link and try again."), 400
    url = m.group(0).rstrip("),.;!?")
    platform = detect_platform(url)
    if not platform:
        return jsonify(ok=False, error="That link isn't from a supported platform."), 400
    try:
        job = resolve_url(platform, url)
    except ValueError as e:
        count_failure(platform)
        return jsonify(ok=False, error=str(e)), 422
    job["platform"] = platform
    job["idtag"] = f"{platform}-{media_id(platform, url)}"
    tok = new_token(job)
    safe_title = re.sub(r"[^\w\- ]+", "", (job.get("title") or "").replace("/", " ")).strip()
    safe_title = re.sub(r"\s+", " ", safe_title)[:60].strip()
    filename = (f"{safe_title} [{job['idtag']}].{job.get('ext', 'mp4')}" if safe_title
                else f"{job['idtag']}.{job.get('ext', 'mp4')}")
    resp = {"ok": True, "platform": PLATFORM_NAMES[platform],
            "title": job.get("title") or "Video",
            "thumbnail": job.get("thumbnail"), "token": tok,
            "filename": filename}
    # The Android app can pull googlevideo files itself (its connection is
    # accepted); the server's IP is refused, so hand the app the raw URL.
    if job.get("kind") == "direct" and "googlevideo.com" in (job.get("url") or ""):
        resp["direct_url"] = job["url"]
    return jsonify(resp)


@app.route("/api/download")
def api_download():
    tok = request.args.get("token", "")
    with TOKENS_LOCK:
        job = TOKENS.get(tok)
    if not job or time.time() - job["created"] > TOKEN_TTL:
        return jsonify(ok=False, error="That fetch expired — tap the circle again."), 410
    platform = job["platform"]
    ext = job.get("ext", "mp4")
    safe_title = re.sub(r"[^\w\- ]+", "", (job.get("title") or "").replace("/", " ")).strip()
    safe_title = re.sub(r"\s+", " ", safe_title)[:60].strip()
    filename = (f"{safe_title} [{job['idtag']}].{ext}" if safe_title
                else f"{job['idtag']}.{ext}")
    mime = EXT_MIME.get(ext, "application/octet-stream")

    if job["kind"] == "direct":
        headers = {"Referer": job["referer"]} if job.get("referer") else {}
        r = None
        try:
            if "googlevideo.com" in job["url"]:
                # YouTube's CDN rejects plain datacenter fetches from some
                # server IPs; a real Chrome TLS fingerprint gets through.
                from curl_cffi import requests as creq
                r = creq.get(job["url"], stream=True, timeout=120,
                             headers=headers, impersonate="chrome124")
            else:
                r = HTTP.get(job["url"], stream=True, timeout=120, headers=headers)
            r.raise_for_status()
        except Exception:
            return jsonify(ok=False, error=GENERIC_ERR), 502
        resp_headers = {"Content-Disposition":
                        "attachment; filename*=UTF-8''" + urllib.parse.quote(filename)}
        if r.headers.get("Content-Length"):
            resp_headers["Content-Length"] = r.headers["Content-Length"]
        count_download(platform, job.get("title") or filename)
        return Response(
            stream_with_context(r.iter_content(256 * 1024)),
            content_type=r.headers.get("Content-Type") or mime,
            headers=resp_headers)

    # kinds that need a temp file on disk (ytdlp / reddit merge)
    tmpdir = tempfile.mkdtemp(prefix="lona-")
    try:
        if job["kind"] == "ytdlp":
            path = ytdlp_download_to_dir(job["source"], job["format"], platform, tmpdir)
            realext = path.suffix.lstrip(".") or ext
            filename = (f"{safe_title} [{job['idtag']}].{realext}" if safe_title
                        else f"{job['idtag']}.{realext}")
        elif job["kind"] == "merge":
            vpath = Path(tmpdir) / "v.mp4"
            http_download_to_file(job["video_url"], vpath)
            if job.get("audio_url"):
                apath = Path(tmpdir) / "a.mp4"
                http_download_to_file(job["audio_url"], apath)
                path = Path(tmpdir) / "out.mp4"
                proc = subprocess.run(
                    ["ffmpeg", "-y", "-i", str(vpath), "-i", str(apath),
                     "-c", "copy", "-movflags", "+faststart", str(path)],
                    capture_output=True, timeout=300)
                if proc.returncode != 0 or not path.exists():
                    path = vpath  # fall back to video-only file
            else:
                path = vpath
        else:
            raise ValueError(GENERIC_ERR)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify(ok=False, error=GENERIC_ERR), 502

    resp = send_file(path, as_attachment=True, download_name=filename,
                     mimetype=EXT_MIME.get(path.suffix.lstrip(".").lower(), mime))
    resp.call_on_close(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
    count_download(platform, job.get("title") or filename)
    return resp


@app.route("/admin")
def admin_page():
    if not _admin_ok():
        return ("", 404)
    return Response(ADMIN_HTML, mimetype="text/html")


@app.route("/admin/data")
def admin_data():
    if not _admin_ok():
        return jsonify(ok=False), 404
    days = [(pkt_now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    ig_month_key = f"ig-{pkt_now().strftime('%Y-%m')}"
    wanted = (["visits", "total", "fails", ig_month_key]
              + [f"p-{p}" for p in ADMIN_PLATFORMS]
              + [f"f-{p}" for p in ADMIN_PLATFORMS]
              + [f"d-{d}" for d in days])
    values = {}

    def _fetch(counter):
        values[counter] = _abacus_get(counter)

    threads = [threading.Thread(target=_fetch, args=(c,), daemon=True)
               for c in wanted]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=6)

    day_vals = [values.get(f"d-{d}") for d in days]
    last7 = None if all(v is None for v in day_vals) else sum(v or 0 for v in day_vals)
    with RECENT_LOCK:
        recent = list(RECENT_DOWNLOADS)
    return jsonify(
        ok=True,
        visits=values.get("visits"),
        total=values.get("total"),
        today=values.get(f"d-{days[0]}"),
        last7=last7,
        platforms={p: values.get(f"p-{p}") for p in ADMIN_PLATFORMS},
        fails_total=values.get("fails"),
        platform_fails={p: values.get(f"f-{p}") for p in ADMIN_PLATFORMS},
        ig_month=values.get(ig_month_key),
        ig_month_cap=500,
        recent=recent,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
