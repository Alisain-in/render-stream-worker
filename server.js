const express = require('express');
const { spawn, exec } = require('child_process');
const fs = require('fs');
const path = require('path');
const cors = require('cors');

const app = express();
app.use(express.json());
app.use(cors());

let currentFfmpegProcess = null;

// Ping endpoint to keep Render awake
app.get('/ping', (req, res) => {
    res.status(200).send('PONG');
});

app.post('/start', async (req, res) => {
    const { streamUrl, videoUrl, secret } = req.body;
    
    if (secret !== process.env.API_SECRET) {
        return res.status(401).json({ error: 'Unauthorized' });
    }

    if (!streamUrl || !videoUrl) {
        return res.status(400).json({ error: 'Missing streamUrl or videoUrl' });
    }

    // Stop existing stream if running
    if (currentFfmpegProcess) {
        currentFfmpegProcess.kill('SIGKILL');
        currentFfmpegProcess = null;
    }

    const videoPath = path.join(__dirname, 'video.mp4');

    try {
        console.log(`Downloading video from ${videoUrl}...`);
        
        // Download using gdown
        exec(`gdown --fuzzy "${videoUrl}" -O ${videoPath}`, (err, stdout, stderr) => {
            if (err) {
                console.error("Gdown error:", err);
                return res.status(500).json({ error: 'Failed to download video' });
            }
            
            console.log("Download complete. Starting FFmpeg...");

            // Start FFmpeg loop
            const ffmpegArgs = [
                '-re',
                '-stream_loop', '-1', // Loop forever
                '-i', videoPath,
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-maxrate', '3000k',
                '-bufsize', '6000k',
                '-pix_fmt', 'yuv420p',
                '-g', '60',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ar', '44100',
                '-f', 'flv',
                streamUrl
            ];

            currentFfmpegProcess = spawn('ffmpeg', ffmpegArgs);

            currentFfmpegProcess.stdout.on('data', (data) => console.log(`FFmpeg: ${data}`));
            currentFfmpegProcess.stderr.on('data', (data) => console.log(`FFmpeg ERR: ${data}`));
            
            currentFfmpegProcess.on('close', (code) => {
                console.log(`FFmpeg exited with code ${code}`);
                currentFfmpegProcess = null;
            });

            res.status(200).json({ success: true, message: 'Stream started successfully' });
        });

    } catch (error) {
        console.error(error);
        res.status(500).json({ error: error.message });
    }
});

app.post('/stop', (req, res) => {
    const { secret } = req.body;
    if (secret !== process.env.API_SECRET) {
        return res.status(401).json({ error: 'Unauthorized' });
    }

    if (currentFfmpegProcess) {
        currentFfmpegProcess.kill('SIGKILL');
        currentFfmpegProcess = null;
        res.json({ success: true, message: 'Stream stopped' });
    } else {
        res.json({ success: true, message: 'No stream running' });
    }
});

const PORT = process.env.PORT || 10000;
app.listen(PORT, () => {
    console.log(`Render stream worker listening on port ${PORT}`);
});
