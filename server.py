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
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

BASE = Path(__file__).resolve().parent
INDEX_HTML = BASE / "index.html"
TOKEN_TTL = 600  # seconds

RAPIDAPI_HOST = "instagram-downloader-download-instagram-videos-stories1.p.rapidapi.com"


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

GENERIC_ERR = "Could not fetch that link — it may be private, deleted, or the platform is blocking it right now."
INSTAGRAM_ERR = "Instagram link nahi khula — private ya ghalat link ho sakta hai"


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
    if has("vimeo.com"):
        return "vimeo"
    if has("dailymotion.com", "dai.ly"):
        return "dailymotion"
    return None


PLATFORM_NAMES = {
    "tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube",
    "facebook": "Facebook", "soundcloud": "SoundCloud", "reddit": "Reddit",
    "pinterest": "Pinterest", "x": "X", "vimeo": "Vimeo",
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
    api = "https://www.tikwm.com/api/?url=" + urllib.parse.quote(url, safe="") + "&hd=1"
    r = HTTP.get(api, timeout=30)
    d = r.json()
    if d.get("code") != 0 or not d.get("data"):
        raise ValueError(GENERIC_ERR)
    data = d["data"]
    play = data.get("hdplay") or data.get("play")
    if not play:
        raise ValueError(GENERIC_ERR)
    return {"kind": "direct", "url": play, "referer": "https://www.tiktok.com/",
            "title": (data.get("title") or "TikTok video").strip(), "ext": "mp4",
            "thumbnail": data.get("cover") or data.get("origin_cover")}


def resolve_instagram(url):
    if not RAPIDAPI_KEY:
        raise ValueError("Instagram is not configured on this server yet.")
    api = f"https://{RAPIDAPI_HOST}/fetch?url=" + urllib.parse.quote(url, safe="")
    data = None
    for _attempt in range(2):  # the free API can be slow — one retry
        try:
            r = HTTP.get(api, headers={"x-rapidapi-host": RAPIDAPI_HOST,
                                        "x-rapidapi-key": RAPIDAPI_KEY}, timeout=90)
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


def ytdlp_extract(url):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 30, "retries": 2, "nocheckcertificate": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def resolve_ytdlp(platform, url):
    try:
        info = ytdlp_extract(url)
    except Exception:
        raise ValueError(GENERIC_ERR)
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
                       "invidious.0011.lt", "inv.nadeko.net", "yewtu.be"]


def resolve_youtube(url):
    """yt-dlp hits YouTube's bot wall from most server IPs, so resolve via a
    public Invidious instance's API (muxed formatStreams), yt-dlp fallback."""
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{6,})", url)
    if m:
        vid = m.group(1)
        for inst in INVIDIOUS_INSTANCES:
            try:
                r = HTTP.get(f"https://{inst}/api/v1/videos/{vid}", timeout=25)
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
    return resolve_ytdlp("youtube", url)


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
    "x": resolve_x,
    "reddit": resolve_reddit,
    "youtube": resolve_youtube,
    "vimeo": resolve_vimeo,
    "dailymotion": resolve_dailymotion,
}


def resolve_url(platform, url):
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
    return send_file(INDEX_HTML)


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


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
        return jsonify(ok=False, error=str(e)), 422
    job["platform"] = platform
    job["idtag"] = f"{platform}-{media_id(platform, url)}"
    tok = new_token(job)
    return jsonify(ok=True, platform=PLATFORM_NAMES[platform],
                   title=job.get("title") or "Video",
                   thumbnail=job.get("thumbnail"), token=tok)


@app.route("/api/download")
def api_download():
    tok = request.args.get("token", "")
    with TOKENS_LOCK:
        job = TOKENS.get(tok)
    if not job or time.time() - job["created"] > TOKEN_TTL:
        return jsonify(ok=False, error="That fetch expired — tap the circle again."), 410
    platform = job["platform"]
    ext = job.get("ext", "mp4")
    filename = f"{job['idtag']}.{ext}"
    mime = EXT_MIME.get(ext, "application/octet-stream")

    if job["kind"] == "direct":
        headers = {"Referer": job["referer"]} if job.get("referer") else {}
        try:
            r = HTTP.get(job["url"], stream=True, timeout=120, headers=headers)
            r.raise_for_status()
        except Exception:
            return jsonify(ok=False, error=GENERIC_ERR), 502
        resp_headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        if r.headers.get("Content-Length"):
            resp_headers["Content-Length"] = r.headers["Content-Length"]
        return Response(
            stream_with_context(r.iter_content(256 * 1024)),
            content_type=r.headers.get("Content-Type") or mime,
            headers=resp_headers)

    # kinds that need a temp file on disk (ytdlp / reddit merge)
    tmpdir = tempfile.mkdtemp(prefix="lona-")
    try:
        if job["kind"] == "ytdlp":
            path = ytdlp_download_to_dir(job["source"], job["format"], platform, tmpdir)
            filename = f"{job['idtag']}.{path.suffix.lstrip('.') or ext}"
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
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
