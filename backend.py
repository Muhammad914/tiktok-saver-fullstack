import os
import re
import uuid
import time
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import yt_dlp


BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

MAX_FILE_AGE = 1800  # 30 minutes

app = Flask(__name__)
CORS(app, origins=["http://localhost:5000"])

limiter = Limiter(get_remote_address, app=app, default_limits=["10 per minute"])

jobs = {}

def clean_old_files_and_jobs():
    now = time.time()


    for f in DOWNLOAD_DIR.iterdir():
        if f.is_file() and (now - f.stat().st_mtime) > MAX_FILE_AGE:
            f.unlink(missing_ok=True)

    
    for j in list(jobs.keys()):
        if now - jobs[j].get("created_at", now) > MAX_FILE_AGE:
            jobs.pop(j, None)

def auto_cleanup():
    while True:
        clean_old_files_and_jobs()
        time.sleep(300)

threading.Thread(target=auto_cleanup, daemon=True).start()


def make_opts(quality, output, hook=None):
    qmap = {
        "1080p": "bestvideo[height<=1080]+bestaudio/best",
        "720p": "bestvideo[height<=720]+bestaudio/best",
        "480p": "bestvideo[height<=480]+bestaudio/best",
        "360p": "bestvideo[height<=360]+bestaudio/best",
        "mp3": "bestaudio",
    }

    opts = {
        "format": qmap.get(quality, qmap["1080p"]),
        "outtmpl": output,
        "quiet": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
    }

    if quality == "mp3":
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
        }]
        opts["outtmpl"] = output.replace(".mp4", ".mp3")

    if hook:
        opts["progress_hooks"] = [hook]

    return opts



@app.route("/api/info", methods=["POST"])
def info():
    data = request.get_json()
    url = (data or {}).get("url", "").strip()

    if not re.search(r"(tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)", url):
        return jsonify({"error": "Invalid TikTok URL"}), 400

    try:
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            info = ydl.extract_info(url, download=False)

        return jsonify({
            "title": info.get("title"),
            "author": info.get("uploader"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
        })

    except Exception:
        return jsonify({"error": "Failed to fetch video info"}), 500


@app.route("/api/download", methods=["POST"])
@limiter.limit("5 per minute")
def download():
    data = request.get_json()
    url = data.get("url")
    quality = data.get("quality", "1080p")

    if not url:
        return jsonify({"error": "No URL"}), 400

    job_id = str(uuid.uuid4())
    ext = "mp3" if quality == "mp3" else "mp4"
    output = str(DOWNLOAD_DIR / f"{job_id}.{ext}")

    jobs[job_id] = {
        "status": "pending",
        "progress": 0,
        "file": None,
        "error": None,
        "created_at": time.time()
    }

    def run():
        def hook(d):
            if d['status'] == 'downloading':
                total = d.get('total_bytes') or 1
                done = d.get('downloaded_bytes', 0)
                jobs[job_id]['progress'] = int(done / total * 100)
                jobs[job_id]['status'] = 'downloading'

        try:
            opts = make_opts(quality, output, hook)

            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

            file = next(DOWNLOAD_DIR.glob(f"{job_id}*"), None)

            if file:
                jobs[job_id]['file'] = str(file.resolve())
                jobs[job_id]['status'] = 'done'
                jobs[job_id]['progress'] = 100
            else:
                jobs[job_id]['status'] = 'error'
                jobs[job_id]['error'] = "File not found"

        except Exception as e:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['error'] = str(e)[:200]

    threading.Thread(target=run, daemon=True).start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)

    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "error": job["error"]
    })


@app.route("/api/file/<job_id>")
def file(job_id):
    job = jobs.get(job_id)

    if not job or job["status"] != "done":
        return jsonify({"error": "Not ready"}), 404

    return send_file(job["file"], as_attachment=True)



if __name__ == "__main__":
    print("🔥 Server running at http://localhost:5000")
    app.run(debug=True)