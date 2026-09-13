const express = require('express');
const { spawn, exec } = require('child_process');
const fs = require('fs');
const path = require('path');
const cors = require('cors');

const app = express();
app.use(express.json());
app.use(cors());

let currentFfmpegProcess = null;

// Root and Ping endpoints for health checks
app.get('/', (req, res) => {
    res.status(200).send('StreamerCore Worker is Online and Ready!');
});

app.get('/ping', (req, res) => {
    res.status(200).send('PONG');
});

// Helper: Download video using multiple resilient methods
function downloadVideo(videoUrl, videoPath) {
    return new Promise((resolve, reject) => {
        // Clean up old file if exists
        if (fs.existsSync(videoPath)) {
            try { fs.unlinkSync(videoPath); } catch (e) {}
        }

        // Extract Google Drive ID if present
        const driveMatch = videoUrl.match(/\/d\/([a-zA-Z0-9_-]+)/) || videoUrl.match(/id=([a-zA-Z0-9_-]+)/);
        const driveId = driveMatch ? driveMatch[1] : null;

        let downloadCmd = '';
        if (driveId) {
            console.log(`Extracted Google Drive ID: ${driveId}`);
            downloadCmd = `python3 -m gdown --id "${driveId}" -O "${videoPath}"`;
        } else {
            downloadCmd = `python3 -m gdown --fuzzy "${videoUrl}" -O "${videoPath}"`;
        }

        console.log(`Executing download: ${downloadCmd}`);
        exec(downloadCmd, { timeout: 180000 }, (err, stdout, stderr) => {
            console.log('Download stdout:', stdout);
            if (stderr) console.log('Download stderr:', stderr);

            // Verify file exists and has size > 1KB
            if (fs.existsSync(videoPath) && fs.statSync(videoPath).size > 1024) {
                console.log(`Video downloaded successfully. Size: ${fs.statSync(videoPath).size} bytes`);
                return resolve(videoPath);
            }

            // Fallback 1: Try yt-dlp
            console.log("gdown failed or produced empty file. Trying fallback with yt-dlp...");
            exec(`yt-dlp "${videoUrl}" -o "${videoPath}"`, { timeout: 180000 }, (ytErr, ytOut, ytErrOut) => {
                if (fs.existsSync(videoPath) && fs.statSync(videoPath).size > 1024) {
                    console.log(`Fallback yt-dlp succeeded! Size: ${fs.statSync(videoPath).size} bytes`);
                    return resolve(videoPath);
                }

                // Fallback 2: Direct curl with redirect follow
                console.log("yt-dlp failed. Trying direct curl...");
                exec(`curl -L "${videoUrl}" -o "${videoPath}"`, { timeout: 180000 }, (curlErr) => {
                    if (fs.existsSync(videoPath) && fs.statSync(videoPath).size > 1024) {
                        return resolve(videoPath);
                    }
                    reject(new Error(`Failed to download video. stderr: ${stderr || ytErrOut || err?.message}`));
                });
            });
        });
    });
}

// Background streaming orchestrator
async function startStreamProcess(streamUrl, videoUrl) {
    const videoPath = path.join(__dirname, 'video.mp4');

    try {
        console.log(`Starting download for: ${videoUrl}`);
        await downloadVideo(videoUrl, videoPath);

        console.log("Video ready. Launching FFmpeg in Zero-CPU Copy Mode...");

        const ffmpegArgs = [
            '-re',
            '-stream_loop', '-1', // Loop endlessly
            '-i', videoPath,
            '-c:v', 'copy',       // Direct copy (near 0% CPU)
            '-c:a', 'aac',        // AAC audio
            '-b:a', '128k',
            '-ar', '44100',
            '-f', 'flv',
            streamUrl
        ];

        currentFfmpegProcess = spawn('ffmpeg', ffmpegArgs);

        currentFfmpegProcess.stdout.on('data', (data) => console.log(`FFmpeg: ${data}`));
        currentFfmpegProcess.stderr.on('data', (data) => console.log(`FFmpeg: ${data}`));

        currentFfmpegProcess.on('close', (code) => {
            console.log(`FFmpeg exited with code ${code}`);
            currentFfmpegProcess = null;
        });

    } catch (error) {
        console.error("Stream initialization failed:", error.message);
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

    // Stop existing stream if running
    if (currentFfmpegProcess) {
        try { currentFfmpegProcess.kill('SIGKILL'); } catch (e) {}
        currentFfmpegProcess = null;
    }

    // Respond immediately with success so Vercel doesn't timeout!
    res.status(200).json({ success: true, message: 'Stream deployment initiated' });

    // Trigger download and streaming in background
    startStreamProcess(streamUrl, videoUrl);
});

app.post('/stop', (req, res) => {
    const { secret } = req.body;
    if (secret !== process.env.API_SECRET) {
        return res.status(401).json({ error: 'Unauthorized' });
    }

    if (currentFfmpegProcess) {
        try { currentFfmpegProcess.kill('SIGKILL'); } catch (e) {}
        currentFfmpegProcess = null;
        res.json({ success: true, message: 'Stream stopped' });
    } else {
        res.json({ success: true, message: 'No stream running' });
    }
});

const PORT = process.env.PORT || 10000;
app.listen(PORT, () => {
    console.log(`StreamerCore Worker listening on port ${PORT}`);
});
