import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from yt_dlp.utils import download_range_func

BASE = Path(__file__).resolve().parent
TMP = BASE / "tmp"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(exist_ok=True)

MAX_LEN = 120
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
SIZES = {(720, 1280), (1080, 1920), (2160, 3840)}
SLOTS = threading.Semaphore(2)

app = FastAPI(title="Heiwa Clips Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def ok_url(u):
    p = urlparse(u)
    return p.scheme in ("http", "https") and (p.hostname or "").lower() in HOSTS


def base_opts():
    o = {"noplaylist": True, "quiet": True, "no_warnings": True}
    b = os.environ.get("HEIWA_COOKIES_BROWSER", "").strip()
    if b:
        o["cookiesfrombrowser"] = (b,)
    return o


def friendly(e):
    m = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    low = m.lower()
    if "not a bot" in low or "sign in to confirm" in low:
        return "YouTube ne bot samajh kar roka. run.bat mein HEIWA_COOKIES_BROWSER=chrome set karein (Chrome mein YouTube login ho) ya yt-dlp update karein."
    if "javascript runtime" in low or "deno" in low:
        return "Deno nahi mila. Chalao: winget install DenoLand.Deno, phir naya terminal kholkar run.bat chalao."
    if "private video" in low:
        return "Ye video private hai."
    if "not available" in low or "unavailable" in low:
        return "Ye video available nahi hai (region/age lock ho sakta hai)."
    return m[:300]


def run(cmd):
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=900)
    except FileNotFoundError:
        raise RuntimeError("FFmpeg nahi mila.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("FFmpeg bahut der laga raha hai.")
    except subprocess.CalledProcessError as e:
        raise RuntimeError("FFmpeg error: " + (e.stderr or "")[-300:])


@app.get("/")
def health():
    return {"ok": True}


@app.get("/info")
def info(url: str):
    if not ok_url(url):
        return JSONResponse({"error": "Sirf YouTube link chalega."}, status_code=400)
    try:
        with yt_dlp.YoutubeDL({**base_opts(), "skip_download": True}) as ydl:
            d = ydl.extract_info(url, download=False)
        if d.get("is_live"):
            return JSONResponse({"error": "Live stream abhi support nahi. Khatam hone ke baad try karein."}, status_code=400)
        heat = [{"start_time": h.get("start_time", 0), "end_time": h.get("end_time", 0), "value": h.get("value", 0)}
                for h in (d.get("heatmap") or [])]
        chs = [{"start_time": c.get("start_time", 0), "end_time": c.get("end_time", 0), "title": c.get("title", "")}
               for c in (d.get("chapters") or [])]
        return {"title": d.get("title", ""), "duration": d.get("duration") or 0, "heatmap": heat, "chapters": chs}
    except Exception as e:
        return JSONResponse({"error": friendly(e)}, status_code=500)


@app.post("/clip")
def clip(url: str = Form(...), start: float = Form(...), end: float = Form(...),
         mode: str = Form("crop"), w: int = Form(720), h: int = Form(1280),
         fps: int = Form(30), best: str = Form("0")):
    if not ok_url(url):
        return JSONResponse({"error": "Sirf YouTube link chalega."}, status_code=400)
    if not (0 <= start < end) or end - start > MAX_LEN:
        return JSONResponse({"error": "Start/End galat hai (max %d sec)." % MAX_LEN}, status_code=400)
    if mode not in ("crop", "fit", "copy") or (w, h) not in SIZES or fps not in (30, 60):
        return JSONResponse({"error": "Settings galat hain."}, status_code=400)
    if not SLOTS.acquire(blocking=False):
        return JSONResponse({"error": "Server busy hai, 1 minute baad try karo."}, status_code=429)

    folder = TMP / uuid.uuid4().hex[:12]
    folder.mkdir()

    def cleanup():
        shutil.rmtree(folder, ignore_errors=True)
        SLOTS.release()

    try:
        cap = 2160 if h >= 3840 else 1080
        opts = {
            **base_opts(),
            "format": f"bv*[height<={cap}][ext=mp4]+ba[ext=m4a]/bv*[height<={cap}]+ba/b[height<={cap}]",
            "outtmpl": str(folder / "src.%(ext)s"),
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 4,
            "download_ranges": download_range_func(None, [(start, end)]),
            "force_keyframes_at_cuts": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        srcs = [f for f in folder.glob("src.*") if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
        if not srcs:
            raise RuntimeError("YouTube se video nahi aaya.")
        src, out = str(srcs[0]), str(folder / "clip.mp4")
        hq = best == "1"

        def encode(vf_args):
            run(["ffmpeg", "-y", "-i", src, *vf_args, "-r", str(fps),
                 "-c:v", "libx264", "-preset", "medium" if hq else "veryfast",
                 "-crf", "18" if hq else "23", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out])

        cr = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
        if mode == "copy":
            try:
                run(["ffmpeg", "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", out])
            except RuntimeError:
                encode([])
        elif mode == "fit":
            ft = (f"split[a][b];[a]{cr},boxblur=20:5[bg];"
                  f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
                  f"[bg][fg]overlay=(W-w)/2:(H-h)/2[o]")
            encode(["-filter_complex", ft, "-map", "[o]", "-map", "0:a?"])
        else:
            encode(["-vf", cr])

        return FileResponse(out, media_type="video/mp4", filename="clip.mp4",
                            background=BackgroundTask(cleanup))
    except Exception as e:
        cleanup()
        return JSONResponse({"error": friendly(e)}, status_code=500)  
