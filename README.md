<div align="center">

# Clippy (Python Desktop App)

Convert videos to 9:16, split into TikTok‑ready segments, generate subtitles, and optionally upload to TikTok.

</div>

---

## Quick start

Requirements: Python 3.10+, Windows/macOS/Linux

```bash
pip install -r requirements.txt
python app_gui.py
```

## What it does

- 9:16 export without cropping (color or blur background)
- Optional parallel encoding (CPU/GPU aware)
- Subtitles: generate SRT locally (faster‑whisper); burn‑in or soft‑mux
- TikTok mode: export, split into 1–30 min clips, subtitle each, name sequentially
- Optional auto‑upload to TikTok (official API; you authorize with your account)

## Using the app

1. Pick input and output
2. Choose preset (1080×1920, 720×1280, …) or set custom size
3. Background: color or blurred video
4. Engine: FFmpeg (auto/CPU/NVIDIA) or MoviePy
5. Subtitles: toggle auto, choose model/language; choose burn‑in or soft
6. TikTok: enable, set segment length (minutes); optional auto‑post
7. Convert

Tip: “Fastest (Auto)” picks sensible settings based on your hardware.

## TikTok connect (optional)

The app supports posting via TikTok’s Content Posting API.

- Create a TikTok Developer app and add redirect URI: `http://127.0.0.1:8765/callback`
- Request scopes: `user.info.basic`, `video.upload` (optional `video.publish`)
- In the app, enter your Client key, click Connect TikTok

Tokens are stored locally on your device. No creator servers are used.

## CLI (optional)

```bash
python convert_to_9x16.py input.mp4 -o output.mp4 --width 1080 --height 1920 --bg "#000000"
```

## Troubleshooting

- FFmpeg missing/codec errors: `pip install imageio-ffmpeg`
- Subtitle model download on first run can take a minute

## Privacy

All processing happens locally on your device. No analytics or external data storage by the creator. Uploads occur only if you connect your TikTok account and choose to post.

