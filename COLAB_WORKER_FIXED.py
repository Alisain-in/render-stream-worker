#@title 🌐 Start StreamerCore 24/7 Streaming Engine (FIXED: Switch Support) { run: "auto" }
MONGODB_URI = "mongodb+srv://sosaad804_db_user:F6phHyiDCifr0azL@cluster0.hm1xeji.mongodb.net/StreamCoreDB?retryWrites=true&w=majority" #@param {type:"string"}

import os
import re
import time
import datetime
import subprocess
import threading

print("Installing Cloud Streaming Dependencies...")
subprocess.run(["apt-get", "update", "-qq"], check=False)
subprocess.run(["apt-get", "install", "-y", "-qq", "ffmpeg"], check=False)
subprocess.run(["pip", "install", "-q", "pymongo", "dnspython", "gdown", "yt-dlp"], check=False)

from pymongo import MongoClient
from bson import ObjectId

client = MongoClient(MONGODB_URI)
db = client["StreamCoreDB"]
streams_collection = db["streams"]

print("DATABASE CONNECTED! Worker running with Live Switch Support!")

active_processes = {}

def download_video(video_url, output_path):
    if os.path.exists(output_path):
        try: os.remove(output_path)
        except: pass

    is_youtube = "youtube.com" in video_url or "youtu.be" in video_url
    if is_youtube:
        subprocess.run(["yt-dlp", "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best", video_url, "-o", output_path], check=False)
    else:
        drive_match = re.search(r'/d/([a-zA-Z0-9_-]+)', video_url) or re.search(r'id=([a-zA-Z0-9_-]+)', video_url)
        if drive_match:
            subprocess.run(["gdown", "--id", drive_match.group(1), "-O", output_path], check=False)
        else:
            subprocess.run(["gdown", "--fuzzy", video_url, "-O", output_path], check=False)

    if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
        subprocess.run(["yt-dlp", video_url, "-o", output_path], check=False)

    return os.path.exists(output_path) and os.path.getsize(output_path) >= 1024


def stream_worker_thread(stream_id, stream_title, video_url, stream_url):
    print(f"\nSTREAM STARTING: {stream_title}")

    def get_doc():
        try: return streams_collection.find_one({"_id": ObjectId(stream_id)})
        except: return None

    def ensure(url, path):
        if os.path.exists(path) and os.path.getsize(path) >= 1024: return True
        return download_video(url, path)

    def build_ffmpeg_input():
        try:
            doc = get_doc()
            if not doc:
                v_file = f"video_{stream_id}.mp4"
                ensure(video_url, v_file)
                return ["-stream_loop", "-1", "-i", v_file]

            playlist = doc.get("playlist", [])
            playback_mode = doc.get("playbackMode", "loop_sequence")
            active_idx = doc.get("activeVideoIndex", 0)

            if playlist and len(playlist) > 1 and playback_mode == "loop_sequence":
                sorted_items = sorted(playlist, key=lambda x: x.get("order", 0))
                lines = []
                for idx, item in enumerate(sorted_items):
                    v_url = item.get("url", video_url)
                    v_file = f"vid_{stream_id}_{item.get('id', idx)}.mp4"
                    if not (os.path.exists(v_file) and os.path.getsize(v_file) >= 1024):
                        print(f"   Pre-caching video #{idx+1}: {item.get('title','Video')}")
                        ensure(v_url, v_file)
                    if os.path.exists(v_file) and os.path.getsize(v_file) >= 1024:
                        for _ in range(max(1, item.get("targetPlayCount", 1))):
                            lines.append(f"file '{v_file}'")
                if lines:
                    txt = f"playlist_{stream_id}.txt"
                    with open(txt, "w", encoding="utf-8") as f:
                        f.write("\n".join(lines) + "\n")
                    return ["-f", "concat", "-safe", "0", "-stream_loop", "-1", "-i", txt]

            v_url = playlist[active_idx].get("url", video_url) if playlist and len(playlist) > active_idx else video_url
            v_file = f"video_{stream_id}.mp4"
            ensure(v_url, v_file)
            return ["-stream_loop", "-1", "-i", v_file]

        except Exception as err:
            print(f"   build_ffmpeg_input error: {err}")
            v_file = f"video_{stream_id}.mp4"
            ensure(video_url, v_file)
            return ["-stream_loop", "-1", "-i", v_file]

    try:
        streams_collection.update_one({"_id": ObjectId(stream_id)}, {"$set": {"status": "LIVE"}})
    except: pass

    while stream_id in active_processes:
        try:
            input_args = build_ffmpeg_input()
            cmd = ["ffmpeg", "-re", *input_args,
                   "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                   "-b:v", "3000k", "-maxrate", "3000k", "-bufsize", "6000k",
                   "-pix_fmt", "yuv420p", "-g", "60",
                   "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                   "-flvflags", "no_duration_filesize", "-f", "flv", stream_url]

            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            active_processes[stream_id] = proc
            print(f"   STREAM IS LIVE ON YOUTUBE! ({stream_title})")

            last_switch_signal = None
            try:
                doc = get_doc()
                last_switch_signal = doc.get("switchSignal") if doc else None
            except: pass

            # ══ SWITCH DETECTION LOOP (replaces proc.wait()) ══
            while proc.poll() is None:
                time.sleep(3)
                if stream_id not in active_processes: break
                try:
                    doc = streams_collection.find_one(
                        {"_id": ObjectId(stream_id)},
                        {"switchSignal": 1, "activeVideoIndex": 1, "status": 1, "scheduledEndTime": 1}
                    )
                    if not doc: break

                    # Check switch signal
                    current_signal = doc.get("switchSignal")
                    if current_signal is not None and current_signal != last_switch_signal:
                        new_idx = doc.get("activeVideoIndex", 0)
                        print(f"\n   SWITCH DETECTED! -> Video #{new_idx + 1}  Restarting FFmpeg...")
                        try: proc.kill()
                        except: pass
                        last_switch_signal = current_signal
                        break

                    # Check scheduled end time
                    end_time = doc.get("scheduledEndTime")
                    if end_time:
                        now_utc = datetime.datetime.now(datetime.timezone.utc)
                        end_utc = end_time if getattr(end_time, "tzinfo", None) else end_time.replace(tzinfo=datetime.timezone.utc)
                        if now_utc >= end_utc:
                            print(f"\n   Scheduled End Time reached for: {stream_title}. Stopping...")
                            streams_collection.update_one({"_id": ObjectId(stream_id)}, {"$set": {"status": "COMPLETED"}})
                            try: proc.kill()
                            except: pass
                            active_processes.pop(stream_id, None)
                            break

                    # Check if externally stopped
                    if doc.get("status") not in ["LIVE", "STARTING"]:
                        try: proc.kill()
                        except: pass
                        break
                except: pass

            if stream_id not in active_processes: break
            print(f"   Restarting stream: {stream_title}...")
            time.sleep(1)

        except Exception as e:
            print(f"   Stream loop error: {e}")
            time.sleep(3)

    print(f"Stream ended: {stream_title}")


while True:
    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        query = {"$or": [
            {"status": "STARTING"},
            {"status": "SCHEDULED", "scheduledStartTime": {"$lte": now_utc}},
            {"status": "LIVE", "$or": [
                {"scheduledEndTime": None},
                {"scheduledEndTime": {"$exists": False}},
                {"scheduledEndTime": {"$gt": now_utc}}
            ]}
        ]}

        for s in list(streams_collection.find(query)):
            sid = str(s["_id"])
            if sid not in active_processes:
                active_processes[sid] = True
                if s.get("status") == "SCHEDULED":
                    print(f"\nScheduled time reached: {s.get('title')}. Launching now...")
                    streams_collection.update_one({"_id": s["_id"]}, {"$set": {"status": "STARTING"}})
                t = threading.Thread(target=stream_worker_thread, args=(
                    sid, s.get("title", "Stream"), s.get("driveLink"), s.get("youtubeStreamKey")
                ), daemon=True)
                t.start()

        all_ids = set(str(s["_id"]) for s in streams_collection.find({"status": {"$in": ["SCHEDULED", "STARTING", "LIVE"]}}))
        for sid in [s for s in list(active_processes.keys()) if s not in all_ids]:
            proc = active_processes.pop(sid, None)
            if proc and hasattr(proc, "kill"):
                try: proc.kill()
                except: pass
            print(f"Stopped stream {sid} (deleted from dashboard).")

        time.sleep(3)
    except KeyboardInterrupt:
        print("\nWorker stopped.")
        break
    except Exception:
        time.sleep(3)
