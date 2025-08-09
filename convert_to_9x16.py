#!/usr/bin/env python
"""
Convert any input video into a 9:16 (portrait) canvas and center it.
- Keeps full video visible (no crop) by scaling to fit, then pads the rest.
- Default output: 1080x1920 with black background, H.264 + AAC.

Usage:
  python convert_to_9x16.py input.mp4
  python convert_to_9x16.py input.mp4 -o output.mp4 --width 1080 --height 1920 --bg "#101010"
"""
import argparse
import os
from typing import Tuple, Optional

from moviepy.editor import VideoFileClip, ColorClip, CompositeVideoClip
import subprocess
import shlex
import re
import math
import tempfile
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

import imageio_ffmpeg as iio_ffmpeg


def parse_color(color: str) -> Tuple[int, int, int]:
    color = color.strip()
    if color.startswith("#") and len(color) == 7:
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        return (r, g, b)
    if "," in color:
        parts = color.split(",")
        if len(parts) == 3:
            r, g, b = (int(p.strip()) for p in parts)
            return (r, g, b)
    raise ValueError("Invalid color. Use '#RRGGBB' or 'R,G,B'.")


def _rgb_to_ffmpeg_hex(rgb: Tuple[int, int, int]) -> str:
    """Return ffmpeg-compatible hex color like 0xRRGGBB."""
    r, g, b = rgb
    return f"0x{r:02x}{g:02x}{b:02x}"


def _has_nvenc(ffmpeg_exe: str) -> bool:
    """Quick check if NVIDIA NVENC encoder is available."""
    try:
        proc = subprocess.run(
            [ffmpeg_exe, "-hide_banner", "-h", "encoder=h264_nvenc"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _run_ffmpeg(cmd):
    """Run a subprocess command and decode output as UTF-8 safely."""
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _probe_duration_seconds(input_path: str) -> float:
    """Probe duration using ffmpeg stderr parsing (works without ffprobe)."""
    ffmpeg = iio_ffmpeg.get_ffmpeg_exe()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", input_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", proc.stderr or "")
    if not m:
        raise RuntimeError("Could not determine input duration")
    hh, mm, ss = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return hh * 3600 + mm * 60 + ss


def _srt_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def transcribe_to_srt(
    input_path: str,
    srt_output: str,
    model_size: str = "base",
    language: Optional[str] = None,
    device: str = "cpu",
    compute_type: str = "int8",
) -> str:
    """
    Transcribe audio from input video to SRT using faster-whisper.
    Downloads the model on first use. Returns the SRT path.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper is not installed. Run: pip install faster-whisper"
        ) from e

    # Prefer CPU by default to avoid missing CUDA DLLs; if GPU requested, fallback to CPU
    try:
        model = WhisperModel(model_size, device=device, compute_type=compute_type, cpu_threads=os.cpu_count() or 4)
    except Exception as e:
        # If GPU requested and failed (e.g., missing cublas64_12.dll), retry on CPU
        if device != "cpu":
            model = WhisperModel(model_size, device="cpu", compute_type=compute_type, cpu_threads=os.cpu_count() or 4)
        else:
            raise

    segments, info = model.transcribe(input_path, language=language)

    lines = []
    idx = 1
    for seg in segments:
        start = float(seg.start)
        end = float(seg.end)
        text = (seg.text or "").strip()
        if not text:
            continue
        lines.append(str(idx))
        lines.append(f"{_srt_timestamp(start)} --> {_srt_timestamp(end)}")
        lines.append(text)
        lines.append("")
        idx += 1

    os.makedirs(os.path.dirname(srt_output) or ".", exist_ok=True)
    with open(srt_output, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")
    return srt_output


def _escape_subtitles_path_for_filter(path: str) -> str:
    """Escape Windows path for ffmpeg subtitles filter."""
    # Escape backslashes and colons; wrap in single quotes
    p = path.replace("\\", "\\\\").replace(":", "\\:")
    return f"'{p}'"


def _hex_to_ass_color(hex_color: str, alpha: int = 0) -> str:
    """Convert '#RRGGBB' to ASS color format &HAABBGGRR with given 0-255 alpha."""
    hx = hex_color.strip()
    if not (len(hx) == 7 and hx.startswith("#")):
        raise ValueError("Color must be '#RRGGBB'")
    r = int(hx[1:3], 16)
    g = int(hx[3:5], 16)
    b = int(hx[5:7], 16)
    a = max(0, min(255, alpha))
    # ASS is &HAABBGGRR (note BGR order)
    return f"&H{a:02X}{b:02X}{g:02X}{r:02X}"


def build_subtitle_force_style(
    *,
    font_name: str = "Arial",
    font_size: int = 24,
    primary_hex: str = "#FFFFFF",
    outline_hex: str = "#000000",
    border_style: int = 3,
    outline: int = 1,
    shadow: int = 0,
    alignment: str = "bottom",  # bottom | middle | top (centered)
    margin_v: int = 24,
) -> str:
    """
    Build an ASS 'force_style' string for ffmpeg subtitles filter from simple inputs.
    Only used for burn-in subtitles.
    """
    border_style = 3 if border_style not in (1, 3) else border_style
    primary = _hex_to_ass_color(primary_hex, alpha=0)
    outline_col = _hex_to_ass_color(outline_hex, alpha=128 if border_style == 3 else 0)
    align_map = {"bottom": 2, "middle": 5, "top": 8}
    align_val = align_map.get((alignment or "bottom").lower(), 2)

    parts = [
        f"FontName={font_name}",
        f"FontSize={int(font_size)}",
        f"PrimaryColour={primary}",
        f"OutlineColour={outline_col}",
        f"BorderStyle={border_style}",
        f"Outline={max(0, int(outline))}",
        f"Shadow={max(0, int(shadow))}",
        f"Alignment={align_val}",
        f"MarginV={max(0, int(margin_v))}",
    ]
    return "'" + ",".join(parts) + "'"


def convert_to_9x16_ffmpeg(
    input_path: str,
    output_path: str | None,
    width: int,
    height: int,
    bg_color: Tuple[int, int, int],
    *,
    encoder: str = "auto",  # 'auto' | 'cpu' | 'nvidia'
    crf: int = 20,
    audio_bitrate: str = "192k",
    subtitles_path: Optional[str] = None,
    force_style: Optional[str] = None,
    bg_mode: str = "color",  # 'color' | 'blur'
    blur_sigma: int = 20,
):
    """
    Fast path using FFmpeg directly. Preserves full frame (no crop) by scale+pad.

    encoder:
      - 'auto'   -> use NVENC if available, else CPU libx264
      - 'cpu'    -> libx264 with CRF
      - 'nvidia' -> h264_nvenc with fast preset
    """
    ffmpeg = iio_ffmpeg.get_ffmpeg_exe()

    # Build filters depending on background mode
    use_filter_complex = (bg_mode == "blur")
    if bg_mode == "color":
        color_hex = _rgb_to_ffmpeg_hex(bg_color)
        filters = [
            f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease",
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={color_hex}",
        ]
        if subtitles_path:
            subexpr = f"subtitles={_escape_subtitles_path_for_filter(subtitles_path)}"
            if force_style:
                subexpr += f":force_style={force_style}"
            filters.append(subexpr)
        filters.append("format=yuv420p")
        vf = ",".join(filters)
    else:
        # Blurred video background: split once to avoid double decode, fast blur via boxblur
        subexpr = ""
        if subtitles_path:
            subexpr = f",subtitles={_escape_subtitles_path_for_filter(subtitles_path)}"
            if force_style:
                subexpr += f":force_style={force_style}"
        radius = max(1, int(blur_sigma))
        filter_complex = (
            f"[0:v]split[base][fgsrc];"
            f"[fgsrc]scale=w={width}:h={height}:force_original_aspect_ratio=decrease,setsar=1[fg];"
            f"[base]scale=w={width}:h={height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,boxblur={radius}:1[bg];"
            f"[bg][fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2[v];"
            f"[v]format=yuv420p{subexpr}[vout]"
        )

    if not output_path:
        root, _ = os.path.splitext(input_path)
        output_path = f"{root}_9x16.mp4"

    # Choose encoder
    use_nvenc = False
    if encoder == "nvidia":
        use_nvenc = True
    elif encoder == "auto":
        use_nvenc = _has_nvenc(ffmpeg)
    else:
        use_nvenc = False

    if use_nvenc:
        vcodec = ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "23"]  # fastest preset
    else:
        vcodec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf)]

    if not use_filter_complex:
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            input_path,
            "-vf",
            vf,
            "-movflags",
            "+faststart",
            *vcodec,
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
            output_path,
        ]
    else:
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            input_path,
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-map",
            "0:a?",
            "-movflags",
            "+faststart",
            *vcodec,
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
            output_path,
        ]

    # Run ffmpeg
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg failed (code {proc.returncode}):\n{proc.stderr}")
    return output_path


def mux_soft_subtitles(
    input_video: str,
    srt_path: str,
    output_path: str,
    language: Optional[str] = None,
):
    ffmpeg = iio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        input_video,
        "-i",
        srt_path,
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-c:s",
        "mov_text",
        "-map",
        "0",
        "-map",
        "1:s:0",
    ]
    if language:
        cmd += ["-metadata:s:s:0", f"language={language}"]
    cmd.append(output_path)
    proc = _run_ffmpeg(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg subtitle mux failed: {proc.stderr}")
    return output_path


def convert_to_9x16_ffmpeg_parallel(
    input_path: str,
    output_path: str | None,
    width: int,
    height: int,
    bg_color: Tuple[int, int, int],
    *,
    encoder: str = "auto",
    crf: int = 20,
    audio_bitrate: str = "192k",
    segment_sec: int = 30,
    jobs: int = 2,
    bg_mode: str = "color",
    blur_sigma: int = 20,
):
    """
    Split the input into time chunks, encode chunks in parallel, and concat.
    Faster on CPU-only systems; requires identical settings across chunks.
    """
    ffmpeg = iio_ffmpeg.get_ffmpeg_exe()
    dur = _probe_duration_seconds(input_path)
    if segment_sec <= 0:
        segment_sec = 30
    if jobs <= 0:
        jobs = 2

    use_filter_complex = (bg_mode == "blur")
    if bg_mode == "color":
        color_hex = _rgb_to_ffmpeg_hex(bg_color)
        vf = (
            f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={color_hex},"
            f"format=yuv420p"
        )
    else:
        radius = max(1, int(blur_sigma))
        filter_complex_base = (
            f"[0:v]split[base][fgsrc];"
            f"[fgsrc]scale=w={width}:h={height}:force_original_aspect_ratio=decrease,setsar=1[fg];"
            f"[base]scale=w={width}:h={height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,boxblur={radius}:1[bg];"
            f"[bg][fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2,format=yuv420p[vout]"
        )

    # Choose encoder options
    use_nvenc = False
    if encoder == "nvidia":
        use_nvenc = True
    elif encoder == "auto":
        use_nvenc = _has_nvenc(ffmpeg)
    else:
        use_nvenc = False

    if use_nvenc:
        vcodec = ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "23"]
    else:
        vcodec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf)]

    if not output_path:
        root, _ = os.path.splitext(input_path)
        output_path = f"{root}_9x16.mp4"

    tmpdir = tempfile.mkdtemp(prefix="v916_")
    chunk_paths = []

    # Plan chunks
    n_chunks = int(math.ceil(dur / segment_sec))
    times = [(i * segment_sec, min((i + 1) * segment_sec, dur) - i * segment_sec) for i in range(n_chunks)]

    # Build commands
    jobs = min(jobs, max(1, os.cpu_count() or 2))
    futures = []
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        for idx, (start, length) in enumerate(times):
            chunk = os.path.join(tmpdir, f"chunk_{idx:04d}.mp4")
            chunk_paths.append(chunk)
            if not use_filter_complex:
                cmd = [
                    ffmpeg,
                    "-y",
                    "-ss",
                    str(max(0, start)),
                    "-t",
                    str(max(0.001, length)),
                    "-i",
                    input_path,
                    "-vf",
                    vf,
                    "-movflags",
                    "+faststart",
                    *vcodec,
                    "-c:a",
                    "aac",
                    "-b:a",
                    audio_bitrate,
                    chunk,
                ]
            else:
                cmd = [
                    ffmpeg,
                    "-y",
                    "-ss",
                    str(max(0, start)),
                    "-t",
                    str(max(0.001, length)),
                    "-i",
                    input_path,
                    "-filter_complex",
                    filter_complex_base,
                    "-map",
                    "[vout]",
                    "-map",
                    "0:a?",
                    "-movflags",
                    "+faststart",
                    *vcodec,
                    "-c:a",
                    "aac",
                    "-b:a",
                    audio_bitrate,
                    chunk,
                ]
            futures.append(ex.submit(_run_ffmpeg, cmd))

        for f in as_completed(futures):
            proc = f.result()
            if proc.returncode != 0:
                # Cleanup and raise
                err = proc.stderr
                try:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                finally:
                    raise RuntimeError(f"FFmpeg chunk failed: {err}")

    # Concat
    list_path = os.path.join(tmpdir, "list.txt")
    with open(list_path, "w", encoding="utf-8") as fh:
        for p in chunk_paths:
            fh.write(f"file '{p}'\n")

    concat_cmd = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_path,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        output_path,
    ]
    proc = subprocess.run(
        concat_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # Best-effort cleanup
    shutil.rmtree(tmpdir, ignore_errors=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg concat failed: {proc.stderr}")
    return output_path


def convert_to_9x16(input_path: str, output_path: str | None, width: int, height: int, bg_color: Tuple[int, int, int]):
    # Load source
    clip = VideoFileClip(input_path)

    # Scale to fit inside target while preserving aspect ratio (no cropping)
    scale = min(width / clip.w, height / clip.h)
    new_w = max(1, int(round(clip.w * scale)))
    new_h = max(1, int(round(clip.h * scale)))
    resized = clip.resize(newsize=(new_w, new_h))

    # Background (solid color) and center the resized video
    bg = ColorClip(size=(width, height), color=bg_color, duration=resized.duration)
    x = (width - new_w) // 2
    y = (height - new_h) // 2
    comp = CompositeVideoClip([bg, resized.set_position((x, y))])

    # Preserve audio if present
    if resized.audio is not None:
        comp = comp.set_audio(resized.audio)

    # Default output path
    if not output_path:
        root, _ = os.path.splitext(input_path)
        output_path = f"{root}_9x16.mp4"

    # Write output (H.264 + AAC). '+faststart' makes the file web-friendly.
    comp.write_videofile(
        output_path,
        fps=clip.fps or 30,
        codec="libx264",
        audio_codec="aac",
        preset="medium",
        threads=4,
        ffmpeg_params=["-movflags", "+faststart"],
    )

    # Cleanup
    clip.close()
    resized.close()
    comp.close()
    bg.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Center a video on a 9:16 canvas with padding (no crop).")
    parser.add_argument("input", help="Path to input video file")
    parser.add_argument("-o", "--output", help="Output file path (default: <input>_9x16.mp4)")
    parser.add_argument("--width", type=int, default=1080, help="Output width (default: 1080)")
    parser.add_argument("--height", type=int, default=1920, help="Output height (default: 1920)")
    parser.add_argument(
        "--bg",
        default="#000000",
        help="Background color as '#RRGGBB' or 'R,G,B' (default: #000000)",
    )
    parser.add_argument(
        "--engine",
        choices=["ffmpeg", "moviepy"],
        default="ffmpeg",
        help="Conversion engine: 'ffmpeg' (fast, default) or 'moviepy' (fallback)",
    )
    parser.add_argument(
        "--encoder",
        choices=["auto", "cpu", "nvidia"],
        default="auto",
        help="When using ffmpeg: choose encoder (auto tries NVENC then CPU)",
    )
    parser.add_argument("--crf", type=int, default=20, help="CRF for libx264 (lower=better quality, larger files)")
    parser.add_argument("--parallel", action="store_true", help="FFmpeg: split into chunks and encode in parallel")
    parser.add_argument("--seg", type=int, default=30, help="FFmpeg parallel: segment length in seconds (default 30)")
    parser.add_argument("--jobs", type=int, default=2, help="FFmpeg parallel: max concurrent encodes (default 2)")
    # Subtitles
    parser.add_argument("--auto-subs", action="store_true", help="Generate subtitles via faster-whisper and attach")
    parser.add_argument("--subs-burn", action="store_true", help="Burn subtitles into video instead of soft mux")
    parser.add_argument("--subs-model", default="base", help="faster-whisper model size (tiny, base, small, medium, large)")
    parser.add_argument("--subs-lang", default=None, help="Force language code (e.g., en, fr). Default: auto-detect")

    args = parser.parse_args()
    try:
        color = parse_color(args.bg)
    except Exception as e:
        raise SystemExit(f"Error: {e}")
    if args.engine == "ffmpeg":
        # Subtitles flow
        srt_path = None
        if args.auto_subs:
            root_out = os.path.splitext(args.output or os.path.splitext(args.input)[0] + "_9x16.mp4")[0]
            srt_path = root_out + ".srt"
            transcribe_to_srt(args.input, srt_path, model_size=args.subs_model, language=args.subs_lang)

        if args.auto_subs and args.subs_burn:
            # Burn in: single pass with subtitles filter; disable parallel
            convert_to_9x16_ffmpeg(
                input_path=args.input,
                output_path=args.output,
                width=args.width,
                height=args.height,
                bg_color=color,
                encoder=args.encoder,
                crf=args.crf,
                subtitles_path=srt_path,
                force_style="'FontSize=24,OutlineColour=&H80000000,BorderStyle=3,Outline=1,Shadow=0'",
            )
        else:
            # No burn (or no subs): convert first (parallel allowed), then mux soft subs if requested
            if args.parallel:
                convert_to_9x16_ffmpeg_parallel(
                    input_path=args.input,
                    output_path=args.output,
                    width=args.width,
                    height=args.height,
                    bg_color=color,
                    encoder=args.encoder,
                    crf=args.crf,
                    segment_sec=args.seg,
                    jobs=args.jobs,
                )
            else:
                convert_to_9x16_ffmpeg(
                    input_path=args.input,
                    output_path=args.output,
                    width=args.width,
                    height=args.height,
                    bg_color=color,
                    encoder=args.encoder,
                    crf=args.crf,
                )
            if args.auto_subs and srt_path:
                # Mux soft subs into final output (rewrite container quickly)
                final_out = args.output or os.path.splitext(args.input)[0] + "_9x16.mp4"
                tmp = final_out + ".tmp.mp4"
                os.replace(final_out, tmp)
                mux_soft_subtitles(tmp, srt_path, final_out, language=args.subs_lang)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    else:
        convert_to_9x16(
            input_path=args.input,
            output_path=args.output,
            width=args.width,
            height=args.height,
            bg_color=color,
        )
