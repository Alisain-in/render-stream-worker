#@title 🌐 Start StreamerCore 24/7 Streaming Engine { run: "auto" }
# =====================================================================
# MODE 1 — LOOP SEQUENCE:  Video1 -> Video2 -> Video1 -> Video2 ...
# MODE 2 — LOOP CURRENT:   Loops only the currently active video
# INSTANT SWITCH:          Kills FFmpeg, jumps to target video NOW
#                          then continues sequence from that point
# =====================================================================
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
print("DATABASE CONNECTED! Worker running with Switch + All Loop Modes!\n")

active_processes = {}  # { stream_id: Popen }


# ─────────────────────────────────────────────
# DOWNLOADER
# ─────────────────────────────────────────────
def download_video(video_url, output_path):
    if os.path.exists(output_path):
        try: os.remove(output_path)
        except: pass

    is_youtube = "youtube.com" in video_url or "youtu.be" in video_url
    if is_youtube:
        print(f"   Downloading YouTube: {video_url[:50]}")
        subprocess.run(["yt-dlp", "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                        video_url, "-o", output_path], check=False)
    else:
        drive_match = re.search(r"/d/([a-zA-Z0-9_-]+)", video_url) or re.search(r"id=([a-zA-Z0-9_-]+)", video_url)
        if drive_match:
            print(f"   Downloading Drive ID: {drive_match.group(1)}")
            subprocess.run(["gdown", "--id", drive_match.group(1), "-O", output_path], check=False)
        else:
            subprocess.run(["gdown", "--fuzzy", video_url, "-O", output_path], check=False)

    if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
        print("   Retrying with yt-dlp...")
        subprocess.run(["yt-dlp", video_url, "-o", output_path], check=False)

    return os.path.exists(output_path) and os.path.getsize(output_path) >= 1024


def ensure(url, path):
    if os.path.exists(path) and os.path.getsize(path) >= 1024:
        return True
    return download_video(url, path)


# ─────────────────────────────────────────────
# STREAM WORKER THREAD
# ─────────────────────────────────────────────
def stream_worker_thread(stream_id, stream_title, video_url, stream_url):
    print(f"\n[STREAM STARTING] {stream_title}")

    def get_doc(fields=None):
        try:
            return streams_collection.find_one({"_id": ObjectId(stream_id)}, fields)
        except:
            return None

    def build_ffmpeg_input(active_idx):
        """
        Builds the correct FFmpeg input args based on current DB state.

        LOOP SEQUENCE (playbackMode = 'loop_sequence'):
            - Downloads all videos in playlist
            - Builds a concat playlist .txt file
            - Rotates the sequence so it STARTS from active_idx
              e.g. switch to video 2 -> plays: 2->1->2->1...
              e.g. normal start (idx=0) -> plays: 1->2->1->2...

        LOOP CURRENT (playbackMode = 'loop_current'):
            - Downloads only the active_idx video
            - Loops that single video forever

        INSTANT SWITCH:
            - The polling loop detects switchSignal change
            - Kills FFmpeg, reads new active_idx from DB
            - Calls build_ffmpeg_input(new_idx) -> starts from new video
        """
        try:
            doc = get_doc()
            if not doc:
                v_file = f"video_{stream_id}.mp4"
                ensure(video_url, v_file)
                return ["-stream_loop", "-1", "-i", v_file]

            playlist      = doc.get("playlist", [])
            playback_mode = doc.get("playbackMode", "loop_sequence")

            # ════════════════════════════════════
            # MODE 1: LOOP SEQUENCE (multi-video)
            # ════════════════════════════════════
            if playlist and len(playlist) > 1 and playback_mode == "loop_sequence":
                sorted_items = sorted(playlist, key=lambda x: x.get("order", 0))

                # Pre-cache ALL videos in background
                for idx, item in enumerate(sorted_items):
                    v_url  = item.get("url", video_url)
                    v_file = f"vid_{stream_id}_{item.get('id', idx)}.mp4"
                    if not (os.path.exists(v_file) and os.path.getsize(v_file) >= 1024):
                        print(f"   Pre-caching #{idx+1} ({item.get('title','Video')})")
                        ensure(v_url, v_file)

                # ⚡ Rotate sequence to start from active_idx
                # This makes instant switch work:
                #   click Switch to Video 2 → sequence becomes: 2->1->2->1
                #   normal start (idx=0)   → sequence stays:   1->2->1->2
                if 0 < active_idx < len(sorted_items):
                    sorted_items = sorted_items[active_idx:] + sorted_items[:active_idx]

                lines = []
                for idx, item in enumerate(sorted_items):
                    v_file = f"vid_{stream_id}_{item.get('id', idx if active_idx == 0 else (idx + active_idx) % len(sorted_items))}.mp4"
                    # Resolve correct filename (items were reordered but filenames are based on original index)
                    original_items = sorted(playlist, key=lambda x: x.get("order", 0))
                    original_idx   = original_items.index(item) if item in original_items else idx
                    v_file = f"vid_{stream_id}_{item.get('id', original_idx)}.mp4"

                    if os.path.exists(v_file) and os.path.getsize(v_file) >= 1024:
                        for _ in range(max(1, item.get("targetPlayCount", 1))):
                            lines.append(f"file '{v_file}'")

                if lines:
                    txt_path = f"playlist_{stream_id}.txt"
                    with open(txt_path, "w", encoding="utf-8") as f:
                        f.write("\n".join(lines) + "\n")
                    return ["-f", "concat", "-safe", "0", "-stream_loop", "-1", "-i", txt_path]

            # ════════════════════════════════════
            # MODE 2: LOOP CURRENT VIDEO (or single video)
            # Also used after instant switch to specific video
            # ════════════════════════════════════
            if playlist and len(playlist) > active_idx:
                v_url = playlist[active_idx].get("url", video_url)
            else:
                v_url = video_url

            v_file = f"video_{stream_id}.mp4"
            ensure(v_url, v_file)
            return ["-stream_loop", "-1", "-i", v_file]

        except Exception as err:
            print(f"   build_ffmpeg_input error: {err}")
            v_file = f"video_{stream_id}.mp4"
            ensure(video_url, v_file)
            return ["-stream_loop", "-1", "-i", v_file]

    # Mark LIVE
    try:
        streams_collection.update_one({"_id": ObjectId(stream_id)}, {"$set": {"status": "LIVE"}})
    except: pass

    # ════════════════════════════════════════════
    # MAIN STREAMING LOOP
    # ════════════════════════════════════════════
    while stream_id in active_processes:
        try:
            # Read fresh state from DB before each FFmpeg launch
            doc        = get_doc()
            active_idx = doc.get("activeVideoIndex", 0) if doc else 0

            input_args = build_ffmpeg_input(active_idx)

            cmd = [
                "ffmpeg", "-re", *input_args,
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-b:v", "3000k", "-maxrate", "3000k", "-bufsize", "6000k",
                "-pix_fmt", "yuv420p", "-g", "60",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                "-flvflags", "no_duration_filesize", "-f", "flv",
                stream_url
            ]

            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            active_processes[stream_id] = proc

            mode_label = doc.get("playbackMode", "loop_sequence") if doc else "loop_sequence"
            print(f"   LIVE [{mode_label.upper()}] starting from video #{active_idx + 1} — {stream_title}")

            # Read current switchSignal baseline
            last_switch_signal = doc.get("switchSignal") if doc else None

            # ══════════════════════════════════════════════════════════
            # SWITCH DETECTION POLL LOOP
            # Replaces proc.wait() — checks DB every 3 seconds.
            # Detects: switchSignal, scheduledEndTime, external stop.
            # ══════════════════════════════════════════════════════════
            while proc.poll() is None:
                time.sleep(3)
                if stream_id not in active_processes:
                    break
                try:
                    doc = streams_collection.find_one(
                        {"_id": ObjectId(stream_id)},
                        {"switchSignal": 1, "activeVideoIndex": 1,
                         "status": 1, "scheduledEndTime": 1, "playbackMode": 1}
                    )
                    if not doc:
                        break

                    # 1. INSTANT SWITCH DETECTED
                    current_signal = doc.get("switchSignal")
                    if current_signal is not None and current_signal != last_switch_signal:
                        new_idx = doc.get("activeVideoIndex", 0)
                        mode    = doc.get("playbackMode", "loop_sequence")
                        print(f"\n   SWITCH! -> Video #{new_idx + 1} | Mode: {mode}")
                        try: proc.kill()
                        except: pass
                        last_switch_signal = current_signal
                        break  # Restart outer loop -> build_ffmpeg_input(new_idx)

                    # 2. PLAYBACK MODE CHANGED (e.g. loop_sequence -> loop_current)
                    current_mode = doc.get("playbackMode", "loop_sequence")
                    current_idx  = doc.get("activeVideoIndex", 0)
                    if (current_mode != mode_label) or (current_mode == "loop_current" and current_idx != active_idx):
                        print(f"\n   Mode changed to {current_mode}. Restarting...")
                        try: proc.kill()
                        except: pass
                        break

                    # 3. SCHEDULED END TIME REACHED
                    end_time = doc.get("scheduledEndTime")
                    if end_time:
                        now_utc = datetime.datetime.now(datetime.timezone.utc)
                        end_utc = end_time if getattr(end_time, "tzinfo", None) else end_time.replace(tzinfo=datetime.timezone.utc)
                        if now_utc >= end_utc:
                            print(f"\n   Scheduled End reached for: {stream_title}")
                            streams_collection.update_one(
                                {"_id": ObjectId(stream_id)}, {"$set": {"status": "COMPLETED"}}
                            )
                            try: proc.kill()
                            except: pass
                            active_processes.pop(stream_id, None)
                            return

                    # 4. EXTERNALLY STOPPED
                    if doc.get("status") not in ["LIVE", "STARTING"]:
                        try: proc.kill()
                        except: pass
                        break

                except Exception:
                    pass  # ignore transient DB errors

            if stream_id not in active_processes:
                break

            print(f"   Restarting: {stream_title}...")
            time.sleep(1)

        except Exception as e:
            print(f"   Stream loop error: {e}")
            time.sleep(3)

    print(f"Stream ended: {stream_title}")


# ═════════════════════════════════════════════════════════════
# MAIN DAEMON LOOP — polls DB every 3s
# ═════════════════════════════════════════════════════════════
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
                    print(f"\nScheduled time reached: {s.get('title')}. Launching...")
                    streams_collection.update_one({"_id": s["_id"]}, {"$set": {"status": "STARTING"}})
                t = threading.Thread(
                    target=stream_worker_thread,
                    args=(sid, s.get("title", "Stream"), s.get("driveLink"), s.get("youtubeStreamKey")),
                    daemon=True
                )
                t.start()

        all_ids = set(str(s["_id"]) for s in streams_collection.find(
            {"status": {"$in": ["SCHEDULED", "STARTING", "LIVE"]}}
        ))
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
