# Video to 9:16 Converter

Convert any video into a 9:16 (portrait) canvas and center it without cropping.

## Setup

1. Install Python 3.10+
2. Install dependencies:

```bash
pip install -r requirements.txt
```

## GUI Usage

```bash
python app_gui.py
```

- Choose an input video.
- Pick a 9:16 preset (1080x1920, 720x1280, etc.) or enter custom size.
- Choose a background color (default: black).
- Click Convert. The output MP4 is saved next to your input unless you choose another path.

## CLI Usage (optional)

```bash
python convert_to_9x16.py input.mp4 -o output.mp4 --width 1080 --height 1920 --bg "#000000"
```

## Notes

- Encoding: H.264 video + AAC audio, with faststart for web players.
- If you see an error about ffmpeg, run:
  ```bash
  pip install imageio-ffmpeg
  ```
  or reinstall requirements.
