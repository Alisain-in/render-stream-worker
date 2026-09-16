const express = require('express');
const { spawn, exec } = require('child_process');
const fs = require('fs');
const path = require('path');
const cors = require('cors');

const app = express();
app.use(express.json());
app.use(cors());

let currentFfmpegProcess = null;
const logHistory = [];

function addLog(msg) {
    const timestamp = new Date().toISOString().substring(11, 19);
    const line = `[${timestamp}] ${msg}`;
    console.log(line);
    logHistory.push(line);
    if (logHistory.length > 200) logHistory.shift();
}

// Health and Live Status Endpoints
app.get('/', (req, res) => {
    res.status(200).send('StreamerCore Worker is Online and Ready!');
});

app.get('/ping', (req, res) => {
    res.status(200).send('PONG');
});

// Real-time Debug Logs Viewable in Browser
app.get('/logs', (req, res) => {
    res.setHeader('Content-Type', 'text/plain');
    res.send(logHistory.join('\n') || 'No logs yet.');
});

// Helper: Download video using multiple resilient methods
function downloadVideo(videoUrl, videoPath) {
    return new Promise((resolve, reject) => {
        if (fs.existsSync(videoPath)) {
            try { fs.unlinkSync(videoPath); } catch (e) {}
        }

        const isYouTube = videoUrl.includes('youtube.com') || videoUrl.includes('youtu.be');
        const driveMatch = videoUrl.match(/\/d\/([a-zA-Z0-9_-]+)/) || videoUrl.match(/id=([a-zA-Z0-9_-]+)/);
        const driveId = driveMatch ? driveMatch[1] : null;

        if (isYouTube) {
            addLog(`Direct YouTube link detected: ${videoUrl}`);
            const ytCmd = `yt-dlp -f "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best" "${videoUrl}" -o "${videoPath}"`;
            addLog(`Running YouTube download: ${ytCmd}`);
            return exec(ytCmd, { timeout: 300000 }, (ytErr, ytOut, ytErrOut) => {
                if (fs.existsSync(videoPath) && fs.statSync(videoPath).size > 1024) {
                    addLog(`YouTube download succeeded. Size: ${(fs.statSync(videoPath).size / (1024*1024)).toFixed(2)} MB`);
                    return resolve(videoPath);
                }
                const errorMsg = `YouTube download failed. Stderr: ${ytErrOut || ytErr?.message}`;
                addLog(errorMsg);
                reject(new Error(errorMsg));
            });
        }

        let downloadCmd = '';
        if (driveId) {
            addLog(`Extracted Google Drive ID: ${driveId}`);
            downloadCmd = `python3 -m gdown --id "${driveId}" -O "${videoPath}"`;
        } else {
            downloadCmd = `python3 -m gdown --fuzzy "${videoUrl}" -O "${videoPath}"`;
        }

        addLog(`Running download command: ${downloadCmd}`);
        exec(downloadCmd, { timeout: 180000 }, (err, stdout, stderr) => {
            if (stdout) addLog(`Download: ${stdout.substring(0, 150)}`);
            if (stderr) addLog(`Download Stderr: ${stderr.substring(0, 150)}`);

            if (fs.existsSync(videoPath) && fs.statSync(videoPath).size > 1024) {
                addLog(`Download succeeded. File size: ${(fs.statSync(videoPath).size / (1024*1024)).toFixed(2)} MB`);
                return resolve(videoPath);
            }

            addLog("gdown failed or produced empty file. Trying yt-dlp fallback...");
            exec(`yt-dlp "${videoUrl}" -o "${videoPath}"`, { timeout: 180000 }, (ytErr, ytOut, ytErrOut) => {
                if (fs.existsSync(videoPath) && fs.statSync(videoPath).size > 1024) {
                    addLog(`yt-dlp fallback succeeded. File size: ${(fs.statSync(videoPath).size / (1024*1024)).toFixed(2)} MB`);
                    return resolve(videoPath);
                }

                addLog("yt-dlp failed. Trying direct curl...");
                exec(`curl -L "${videoUrl}" -o "${videoPath}"`, { timeout: 180000 }, (curlErr) => {
                    if (fs.existsSync(videoPath) && fs.statSync(videoPath).size > 1024) {
                        addLog(`curl fallback succeeded. File size: ${(fs.statSync(videoPath).size / (1024*1024)).toFixed(2)} MB`);
                        return resolve(videoPath);
                    }
                    const errorMsg = `All download methods failed. Stderr: ${stderr || ytErrOut || err?.message}`;
                    addLog(errorMsg);
                    reject(new Error(errorMsg));
                });
            });
        });
    });
}

// Background streaming orchestrator
async function startStreamProcess(streamUrl, videoUrl) {
    const videoPath = path.join(__dirname, 'video.mp4');

    try {
        addLog(`Initiating download for video from: ${videoUrl}`);
        await downloadVideo(videoUrl, videoPath);

        addLog(`Starting FFmpeg stream to YouTube RTMP: ${streamUrl.substring(0, 35)}...`);

        // YouTube requires strict H.264 + AAC + 2-second keyframes (GOP -g 60 at 30fps)
        // preset ultrafast keeps CPU usage extremely low while guaranteeing broadcast compatibility
        const ffmpegArgs = [
            '-re',
            '-stream_loop', '-1',
            '-i', videoPath,
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-tune', 'zerolatency',
            '-b:v', '2500k',
            '-maxrate', '2500k',
            '-bufsize', '5000k',
            '-pix_fmt', 'yuv420p',
            '-g', '60',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-flvflags', 'no_duration_filesize',
            '-f', 'flv',
            streamUrl
        ];

        currentFfmpegProcess = spawn('ffmpeg', ffmpegArgs);

        currentFfmpegProcess.stdout.on('data', (data) => addLog(`FFmpeg: ${data.toString().trim()}`));
        currentFfmpegProcess.stderr.on('data', (data) => {
            const str = data.toString().trim();
            // Only log meaningful progress/error lines to avoid flooding
            if (str.includes('frame=') || str.includes('Error') || str.includes('Opening') || str.includes('Stream #')) {
                addLog(`FFmpeg: ${str}`);
            }
        });

        currentFfmpegProcess.on('close', (code) => {
            addLog(`FFmpeg process exited with code: ${code}`);
            currentFfmpegProcess = null;
        });

    } catch (error) {
        addLog(`Stream Error: ${error.message}`);
    }
}

app.post('/start', (req, res) => {
    const { streamUrl, videoUrl, secret } = req.body;
    
    if (secret !== process.env.API_SECRET) {
        return res.status(401).json({ error: 'Unauthorized' });
    }

    if (!streamUrl || !videoUrl) {
        return res.status(400).json({ error: 'Missing streamUrl or videoUrl' });
    }

    if (currentFfmpegProcess) {
        addLog("Terminating previous stream process...");
        try { currentFfmpegProcess.kill('SIGKILL'); } catch (e) {}
        currentFfmpegProcess = null;
    }

    addLog("Received /start request from dashboard.");
    res.status(200).json({ success: true, message: 'Stream deployment initiated' });

    startStreamProcess(streamUrl, videoUrl);
});

app.post('/stop', (req, res) => {
    const { secret } = req.body;
    if (secret !== process.env.API_SECRET) {
        return res.status(401).json({ error: 'Unauthorized' });
    }

    if (currentFfmpegProcess) {
        addLog("Received /stop request. Killing FFmpeg process...");
        try { currentFfmpegProcess.kill('SIGKILL'); } catch (e) {}
        currentFfmpegProcess = null;
        res.json({ success: true, message: 'Stream stopped' });
    } else {
        res.json({ success: true, message: 'No stream running' });
    }
});

const PORT = process.env.PORT || 10000;
app.listen(PORT, () => {
    addLog(`StreamerCore Worker active on port ${PORT}`);
});
