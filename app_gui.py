#!/usr/bin/env python
import os
import glob
import tempfile
import shutil
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser

import imageio_ffmpeg as iio_ffmpeg

# Reuse the existing converter
from convert_to_9x16 import (
    convert_to_9x16,
    convert_to_9x16_ffmpeg,
    convert_to_9x16_ffmpeg_parallel,
    parse_color,
    transcribe_to_srt,
    mux_soft_subtitles,
    build_subtitle_force_style,
    _escape_subtitles_path_for_filter,
)
from tiktok_api import oauth_connect, get_user_info, upload_video

APP_TITLE = "Clippy"
DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("780x720")
        self.minsize(720, 560)

        self.input_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.width_var = tk.IntVar(value=DEFAULT_WIDTH)
        self.height_var = tk.IntVar(value=DEFAULT_HEIGHT)
        self.bg_hex = tk.StringVar(value="#000000")
        self.bg_mode_var = tk.StringVar(value="color")  # color | blur
        self.blur_sigma_var = tk.IntVar(value=20)
        self.status_text = tk.StringVar(value="Idle")
        self.engine_var = tk.StringVar(value="FFmpeg (auto)")
        self.crf_var = tk.IntVar(value=20)
        self.parallel_var = tk.BooleanVar(value=False)
        self.seg_var = tk.IntVar(value=30)
        self.jobs_var = tk.IntVar(value=2)
        # TikTok mode
        self.tiktok_var = tk.BooleanVar(value=False)
        self.tiktok_min_var = tk.IntVar(value=1)
        # Subtitles
        self.auto_subs_var = tk.BooleanVar(value=False)
        self.burn_subs_var = tk.BooleanVar(value=False)
        self.subs_model_var = tk.StringVar(value="base")
        self.subs_lang_var = tk.StringVar(value="")
        # Subtitle style
        self.subs_font_var = tk.StringVar(value="Arial")
        self.subs_size_var = tk.IntVar(value=24)
        self.subs_color_hex = tk.StringVar(value="#FFFFFF")
        self.subs_outline_hex = tk.StringVar(value="#000000")
        self.subs_outline_var = tk.IntVar(value=1)
        self.subs_shadow_var = tk.IntVar(value=0)
        self.subs_align_var = tk.StringVar(value="bottom")
        self.subs_margin_var = tk.IntVar(value=24)
        self.subs_box_bg_var = tk.BooleanVar(value=True)  # BorderStyle=3

        self._build_ui()

    def _build_ui(self):
        pad = {'padx': 10, 'pady': 6}

        container = ttk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True)
        self._canvas = tk.Canvas(container, highlightthickness=0)
        vscroll = ttk.Scrollbar(container, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vscroll.set)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vscroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.body = ttk.Frame(self._canvas)
        body_window = self._canvas.create_window((0, 0), window=self.body, anchor="nw")

        def _on_body_config(_e=None):
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        def _on_canvas_config(e=None):
            try:
                self._canvas.itemconfigure(body_window, width=self._canvas.winfo_width())
            except Exception:
                pass
        self.body.bind("<Configure>", _on_body_config)
        self._canvas.bind("<Configure>", _on_canvas_config)

        # Input
        frm_in = ttk.LabelFrame(self.body, text="Input video")
        frm_in.pack(fill=tk.X, **pad)
        ttk.Entry(frm_in, textvariable=self.input_path).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 6), pady=8)
        ttk.Button(frm_in, text="Browse...", command=self.browse_input).pack(side=tk.LEFT, padx=(0, 10), pady=8)

        # Output
        frm_out = ttk.LabelFrame(self.body, text="Output file (.mp4)")
        frm_out.pack(fill=tk.X, **pad)
        ttk.Entry(frm_out, textvariable=self.output_path).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 6), pady=8)
        ttk.Button(frm_out, text="Save As...", command=self.browse_output).pack(side=tk.LEFT, padx=(0, 10), pady=8)

        # Options
        frm_opts = ttk.LabelFrame(self.body, text="Options (9:16)")
        frm_opts.pack(fill=tk.X, **pad)

        # Presets
        presets = [
            ("1080 x 1920 (FullHD)", 1080, 1920),
            ("720 x 1280 (HD)", 720, 1280),
            ("540 x 960 (qHD)", 540, 960),
        ]
        ttk.Label(frm_opts, text="Preset:").grid(row=0, column=0, sticky=tk.W, padx=(10, 6), pady=(10, 4))
        self.preset_cb = ttk.Combobox(frm_opts, state="readonly", values=[p[0] for p in presets])
        self.preset_cb.grid(row=0, column=1, sticky=tk.W, pady=(10, 4))
        self.preset_cb.current(0)

        def on_preset(_=None):
            idx = self.preset_cb.current()
            _, w, h = presets[idx]
            self.width_var.set(w)
            self.height_var.set(h)
        self.preset_cb.bind("<<ComboboxSelected>>", on_preset)
        on_preset()

        # Size
        ttk.Label(frm_opts, text="Width:").grid(row=1, column=0, sticky=tk.W, padx=(10, 6))
        ttk.Spinbox(frm_opts, from_=240, to=2160, increment=10, textvariable=self.width_var, width=10).grid(row=1, column=1, sticky=tk.W)
        ttk.Label(frm_opts, text="Height:").grid(row=1, column=2, sticky=tk.W, padx=(10, 6))
        ttk.Spinbox(frm_opts, from_=426, to=3840, increment=10, textvariable=self.height_var, width=10).grid(row=1, column=3, sticky=tk.W)

        # Background options
        ttk.Label(frm_opts, text="Background:").grid(row=2, column=0, sticky=tk.W, padx=(10, 6), pady=(6, 2))
        self.bg_mode_cb = ttk.Combobox(
            frm_opts,
            state="readonly",
            textvariable=self.bg_mode_var,
            values=["color", "blur"],
            width=10,
        )
        self.bg_mode_cb.grid(row=2, column=1, sticky=tk.W, pady=(6, 2))
        self.bg_mode_cb.current(0)

        # Color controls (visible for color mode)
        self.color_preview = tk.Label(frm_opts, textvariable=self.bg_hex, width=12, relief=tk.SUNKEN, bg=self.bg_hex.get(), fg="#ffffff")
        self.color_preview.grid(row=2, column=2, sticky=tk.W, pady=(6, 2))
        self.pick_color_btn = ttk.Button(frm_opts, text="Pick...", command=self.pick_color)
        self.pick_color_btn.grid(row=2, column=3, sticky=tk.W, pady=(6, 2))

        # Blur controls (visible for blur mode)
        ttk.Label(frm_opts, text="Blur sigma:").grid(row=3, column=0, sticky=tk.W, padx=(10, 6))
        self.blur_spin = ttk.Spinbox(frm_opts, from_=1, to=100, increment=1, textvariable=self.blur_sigma_var, width=10)
        self.blur_spin.grid(row=3, column=1, sticky=tk.W)

        def on_bg_mode(_=None):
            mode = self.bg_mode_var.get()
            is_color = (mode == "color")
            # Enable/disable color widgets
            state_color = tk.NORMAL if is_color else tk.DISABLED
            self.color_preview.configure(state=state_color)
            self.pick_color_btn.configure(state=state_color)
            # Enable/disable blur widgets
            state_blur = tk.NORMAL if not is_color else tk.DISABLED
            self.blur_spin.configure(state=state_blur)
        self.bg_mode_cb.bind("<<ComboboxSelected>>", on_bg_mode)
        on_bg_mode()

        # Engine / Speed
        ttk.Label(frm_opts, text="Engine:").grid(row=4, column=0, sticky=tk.W, padx=(10, 6))
        self.engine_cb = ttk.Combobox(
            frm_opts,
            state="readonly",
            textvariable=self.engine_var,
            values=[
                "FFmpeg (auto)",
                "FFmpeg (CPU)",
                "FFmpeg (NVIDIA)",
                "MoviePy",
            ],
            width=18,
        )
        self.engine_cb.grid(row=4, column=1, sticky=tk.W)

        ttk.Label(frm_opts, text="CRF (CPU):").grid(row=4, column=2, sticky=tk.W, padx=(10, 6))
        ttk.Spinbox(frm_opts, from_=14, to=30, increment=1, textvariable=self.crf_var, width=6).grid(row=4, column=3, sticky=tk.W)

        # Parallel chunk options
        self.parallel_chk = ttk.Checkbutton(frm_opts, text="Parallel chunks", variable=self.parallel_var)
        self.parallel_chk.grid(row=5, column=0, sticky=tk.W, padx=(10, 6), pady=(6, 10))
        ttk.Label(frm_opts, text="Segment (s):").grid(row=5, column=1, sticky=tk.W, padx=(10, 6), pady=(6, 10))
        ttk.Spinbox(frm_opts, from_=5, to=300, increment=5, textvariable=self.seg_var, width=8).grid(row=5, column=1, sticky=tk.E, pady=(6, 10))
        ttk.Label(frm_opts, text="Jobs:").grid(row=5, column=2, sticky=tk.W, padx=(10, 6), pady=(6, 10))
        ttk.Spinbox(frm_opts, from_=1, to=8, increment=1, textvariable=self.jobs_var, width=6).grid(row=5, column=3, sticky=tk.W, pady=(6, 10))

        # Subtitles
        frm_subs = ttk.LabelFrame(self.body, text="Subtitles")
        frm_subs.pack(fill=tk.X, **pad)
        self.auto_chk = ttk.Checkbutton(frm_subs, text="Auto subtitles (generate .srt)", variable=self.auto_subs_var)
        self.auto_chk.grid(row=0, column=0, sticky=tk.W, padx=(10, 6), pady=(8, 4))
        self.burn_chk = ttk.Checkbutton(frm_subs, text="Burn into video (not removable)", variable=self.burn_subs_var)
        self.burn_chk.grid(row=0, column=1, sticky=tk.W, padx=(10, 6), pady=(8, 4))

        ttk.Label(frm_subs, text="Model:").grid(row=1, column=0, sticky=tk.W, padx=(10, 6), pady=(0, 10))
        self.model_cb = ttk.Combobox(
            frm_subs,
            state="readonly",
            textvariable=self.subs_model_var,
            values=["tiny", "base", "small", "medium", "large-v3"],
            width=12,
        )
        self.model_cb.grid(row=1, column=1, sticky=tk.W, pady=(0, 10))
        self.model_cb.current(1)

        ttk.Label(frm_subs, text="Language (opt):").grid(row=1, column=2, sticky=tk.W, padx=(10, 6), pady=(0, 10))
        ttk.Entry(frm_subs, textvariable=self.subs_lang_var, width=10).grid(row=1, column=3, sticky=tk.W, pady=(0, 10))

        # Style controls (apply when Burn-in enabled)
        ttk.Label(frm_subs, text="Font:").grid(row=2, column=0, sticky=tk.W, padx=(10, 6))
        ttk.Entry(frm_subs, textvariable=self.subs_font_var, width=14).grid(row=2, column=1, sticky=tk.W)
        ttk.Label(frm_subs, text="Size:").grid(row=2, column=2, sticky=tk.W, padx=(10, 6))
        ttk.Spinbox(frm_subs, from_=10, to=96, increment=1, textvariable=self.subs_size_var, width=6).grid(row=2, column=3, sticky=tk.W)

        # Colors
        ttk.Label(frm_subs, text="Text color:").grid(row=3, column=0, sticky=tk.W, padx=(10, 6), pady=(4, 0))
        self.subs_color_preview = tk.Label(frm_subs, textvariable=self.subs_color_hex, width=10, relief=tk.SUNKEN, bg=self.subs_color_hex.get())
        self.subs_color_preview.grid(row=3, column=1, sticky=tk.W, pady=(4, 0))
        ttk.Button(frm_subs, text="Pick", command=self.pick_subs_color).grid(row=3, column=1, sticky=tk.E, pady=(4, 0))

        ttk.Label(frm_subs, text="Outline color:").grid(row=3, column=2, sticky=tk.W, padx=(10, 6), pady=(4, 0))
        self.subs_outline_preview = tk.Label(frm_subs, textvariable=self.subs_outline_hex, width=10, relief=tk.SUNKEN, bg=self.subs_outline_hex.get())
        self.subs_outline_preview.grid(row=3, column=3, sticky=tk.W, pady=(4, 0))
        ttk.Button(frm_subs, text="Pick", command=self.pick_subs_outline_color).grid(row=3, column=3, sticky=tk.E, pady=(4, 0))

        # Outline / Shadow
        ttk.Label(frm_subs, text="Outline:").grid(row=4, column=0, sticky=tk.W, padx=(10, 6))
        ttk.Spinbox(frm_subs, from_=0, to=10, increment=1, textvariable=self.subs_outline_var, width=6).grid(row=4, column=1, sticky=tk.W)
        ttk.Label(frm_subs, text="Shadow:").grid(row=4, column=2, sticky=tk.W, padx=(10, 6))
        ttk.Spinbox(frm_subs, from_=0, to=10, increment=1, textvariable=self.subs_shadow_var, width=6).grid(row=4, column=3, sticky=tk.W)

        # Alignment / Margin / Box
        ttk.Label(frm_subs, text="Align:").grid(row=5, column=0, sticky=tk.W, padx=(10, 6), pady=(0, 10))
        ttk.Combobox(frm_subs, state="readonly", values=["bottom", "middle", "top"], textvariable=self.subs_align_var, width=10).grid(row=5, column=1, sticky=tk.W, pady=(0, 10))
        ttk.Label(frm_subs, text="MarginV:").grid(row=5, column=2, sticky=tk.W, padx=(10, 6), pady=(0, 10))
        ttk.Spinbox(frm_subs, from_=0, to=200, increment=2, textvariable=self.subs_margin_var, width=6).grid(row=5, column=3, sticky=tk.W, pady=(0, 10))
        ttk.Checkbutton(frm_subs, text="Box background", variable=self.subs_box_bg_var).grid(row=5, column=4, sticky=tk.W, padx=(10, 0), pady=(0, 10))

        # TikTok mode
        frm_tt = ttk.LabelFrame(self.body, text="TikTok")
        frm_tt.pack(fill=tk.X, **pad)
        self.tiktok_chk = ttk.Checkbutton(frm_tt, text="Enable TikTok mode", variable=self.tiktok_var)
        self.tiktok_chk.grid(row=0, column=0, sticky=tk.W, padx=(10, 6), pady=(8, 4))
        ttk.Label(frm_tt, text="Segment length (min):").grid(row=0, column=1, sticky=tk.W, padx=(10, 6))
        ttk.Spinbox(frm_tt, from_=1, to=30, increment=1, textvariable=self.tiktok_min_var, width=6).grid(row=0, column=2, sticky=tk.W)
        # TikTok connect + client key
        ttk.Label(frm_tt, text="Client key:").grid(row=1, column=0, sticky=tk.W, padx=(10, 6))
        self.tt_client_key = tk.StringVar()
        ttk.Entry(frm_tt, textvariable=self.tt_client_key, width=28).grid(row=1, column=1, sticky=tk.W)
        self.tt_connected_lbl = ttk.Label(frm_tt, text="Not connected")
        self.tt_connected_lbl.grid(row=1, column=2, sticky=tk.W)
        ttk.Button(frm_tt, text="Connect TikTok", command=self.connect_tiktok).grid(row=1, column=3, sticky=tk.W, padx=(6, 0))
        self.tt_autopost = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm_tt, text="Auto-post segments", variable=self.tt_autopost).grid(row=2, column=0, columnspan=2, sticky=tk.W, padx=(10, 6), pady=(0, 6))

        # Action row
        frm_act = ttk.Frame(self.body)
        frm_act.pack(fill=tk.X, **pad)
        self.convert_btn = ttk.Button(frm_act, text="Convert", command=self.start_convert)
        self.convert_btn.pack(side=tk.LEFT, padx=(10, 6))

        self.fastest_btn = ttk.Button(frm_act, text="Fastest (Auto)", command=self.fastest_convert)
        self.fastest_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.progress = ttk.Progressbar(frm_act, mode="indeterminate")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 10))

        self.status_lbl = ttk.Label(self.body, textvariable=self.status_text)
        self.status_lbl.pack(fill=tk.X, padx=10)

    def connect_tiktok(self):
        key = (self.tt_client_key.get() or "").strip()
        if not key:
            messagebox.showwarning("TikTok", "Enter your TikTok client key first.")
            return
        def worker():
            self.after(0, lambda: (self._set_busy(True), self.status_text.set("Connecting to TikTok...")))
            ok = True
            err = None
            info = None
            try:
                oauth_connect(key, None)
                info = get_user_info(key)
            except Exception as e:
                ok = False
                err = e
            finally:
                def done():
                    self._set_busy(False)
                    if ok:
                        self.status_text.set("TikTok connected")
                        self.tt_connected_lbl.configure(text="Connected")
                        if info:
                            try:
                                name = info.get("data", {}).get("user", {}).get("display_name") or ""
                                if name:
                                    self.tt_connected_lbl.configure(text=f"Connected: {name}")
                            except Exception:
                                pass
                    else:
                        self.status_text.set("TikTok connect failed")
                        messagebox.showerror("TikTok", f"Connect failed:\n{err}")
                self.after(0, done)
        threading.Thread(target=worker, daemon=True).start()

    def browse_input(self):
        path = filedialog.askopenfilename(
            title="Choose a video",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.mkv *.avi *.m4v *.webm"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self.input_path.set(path)
        # Auto-derive output path
        root, _ = os.path.splitext(path)
        self.output_path.set(root + "_9x16.mp4")

    def browse_output(self):
        initialfile = os.path.basename(self.output_path.get()) if self.output_path.get() else "output_9x16.mp4"
        path = filedialog.asksaveasfilename(
            title="Save output as",
            defaultextension=".mp4",
            filetypes=[("MP4 Video", ".mp4")],
            initialfile=initialfile,
        )
        if path:
            self.output_path.set(path)

    def pick_color(self):
        try:
            rgb, hx = colorchooser.askcolor(color=self.bg_hex.get(), title="Choose background color")
            if hx:
                self.bg_hex.set(hx)
                self.color_preview.configure(bg=hx)
        except Exception as e:
            messagebox.showerror("Color Picker", f"Failed to pick color: {e}")

    def pick_subs_color(self):
        try:
            rgb, hx = colorchooser.askcolor(color=self.subs_color_hex.get(), title="Choose subtitle text color")
            if hx:
                self.subs_color_hex.set(hx)
                self.subs_color_preview.configure(bg=hx)
        except Exception as e:
            messagebox.showerror("Color Picker", f"Failed to pick subtitle color: {e}")

    def pick_subs_outline_color(self):
        try:
            rgb, hx = colorchooser.askcolor(color=self.subs_outline_hex.get(), title="Choose subtitle outline color")
            if hx:
                self.subs_outline_hex.set(hx)
                self.subs_outline_preview.configure(bg=hx)
        except Exception as e:
            messagebox.showerror("Color Picker", f"Failed to pick outline color: {e}")

    def _set_busy(self, busy: bool):
        state = tk.DISABLED if busy else tk.NORMAL
        for child in self.winfo_children():
            try:
                child.configure(state=state)
            except tk.TclError:
                pass
        # Re-enable status label even when disabled loop above
        self.status_lbl.configure(state=tk.NORMAL)
        self.convert_btn.configure(state=tk.DISABLED if busy else tk.NORMAL)
        if busy:
            self.progress.start(10)
        else:
            self.progress.stop()

    def start_convert(self):
        src = self.input_path.get().strip()
        dst = self.output_path.get().strip()
        if not src or not os.path.isfile(src):
            messagebox.showwarning("Input", "Please choose a valid input video file.")
            return
        if not dst:
            root, _ = os.path.splitext(src)
            dst = root + "_9x16.mp4"
            self.output_path.set(dst)
        # Ensure directory exists
        out_dir = os.path.dirname(dst) or "."
        os.makedirs(out_dir, exist_ok=True)

        try:
            w = int(self.width_var.get())
            h = int(self.height_var.get())
            if w <= 0 or h <= 0:
                raise ValueError
        except Exception:
            messagebox.showwarning("Dimensions", "Please enter valid positive width and height.")
            return

        hx = self.bg_hex.get() or "#000000"
        engine_label = self.engine_var.get()
        if engine_label == "FFmpeg (auto)":
            engine = "ffmpeg"; encoder = "auto"
        elif engine_label == "FFmpeg (CPU)":
            engine = "ffmpeg"; encoder = "cpu"
        elif engine_label == "FFmpeg (NVIDIA)":
            engine = "ffmpeg"; encoder = "nvidia"
        else:
            engine = "moviepy"; encoder = "cpu"
        bg_mode = self.bg_mode_var.get()
        blur_sigma = int(self.blur_sigma_var.get())

        def worker():
            self.after(0, lambda: (self._set_busy(True), self.status_text.set("Converting...")))
            ok = True
            err = None
            done_msg = None
            try:
                rgb = parse_color(hx)
                # Work on local copies to avoid scope issues
                engine_local = engine
                encoder_local = encoder
                # If user selected MoviePy but needs blurred background, switch to FFmpeg
                if engine_local != "ffmpeg" and bg_mode == "blur":
                    self.after(0, lambda: self.status_text.set("Using FFmpeg for blurred background..."))
                    engine_local = "ffmpeg"
                    if encoder_local == "cpu":
                        encoder_local = "auto"
                # Subtitles: optionally transcribe first (skip in TikTok mode; handled per-segment later)
                tiktok_mode = bool(self.tiktok_var.get())
                srt_path = None
                if self.auto_subs_var.get() and not tiktok_mode:
                    self.after(0, lambda: self.status_text.set("Transcribing (first run downloads the model)..."))
                    root_out = os.path.splitext(dst)[0]
                    srt_path = root_out + ".srt"
                    lang = self.subs_lang_var.get().strip() or None
                    transcribe_to_srt(src, srt_path, model_size=self.subs_model_var.get(), language=lang)

                # Convert and embed subtitles
                if engine_local == "ffmpeg":
                    if srt_path and self.burn_subs_var.get() and not tiktok_mode:
                        # Burn-in requires a single pass (disable chunking)
                        force_style = build_subtitle_force_style(
                            font_name=self.subs_font_var.get().strip() or "Arial",
                            font_size=int(self.subs_size_var.get()),
                            primary_hex=self.subs_color_hex.get().strip() or "#FFFFFF",
                            outline_hex=self.subs_outline_hex.get().strip() or "#000000",
                            border_style=3 if self.subs_box_bg_var.get() else 1,
                            outline=int(self.subs_outline_var.get()),
                            shadow=int(self.subs_shadow_var.get()),
                            alignment=self.subs_align_var.get(),
                            margin_v=int(self.subs_margin_var.get()),
                        )
                        convert_to_9x16_ffmpeg(
                            src,
                            dst,
                            w,
                            h,
                            rgb,
                            encoder=encoder_local,
                            crf=int(self.crf_var.get()),
                            subtitles_path=srt_path,
                            force_style=force_style,
                            bg_mode=bg_mode,
                            blur_sigma=blur_sigma,
                        )
                    else:
                        # Normal convert (optionally parallel)
                        if self.parallel_var.get():
                            convert_to_9x16_ffmpeg_parallel(
                                src,
                                dst,
                                w,
                                h,
                                rgb,
                                encoder=encoder_local,
                                crf=int(self.crf_var.get()),
                                segment_sec=int(self.seg_var.get()),
                                jobs=int(self.jobs_var.get()),
                                bg_mode=bg_mode,
                                blur_sigma=blur_sigma,
                            )
                        else:
                            convert_to_9x16_ffmpeg(
                                src,
                                dst,
                                w,
                                h,
                                rgb,
                                encoder=encoder_local,
                                crf=int(self.crf_var.get()),
                                bg_mode=bg_mode,
                                blur_sigma=blur_sigma,
                            )

                        # Soft-mux subtitles after conversion (skip in TikTok mode)
                        if srt_path and not self.burn_subs_var.get() and not tiktok_mode:
                            tmp = dst + ".tmp.mp4"
                            try:
                                os.replace(dst, tmp)
                                lang = self.subs_lang_var.get().strip() or None
                                mux_soft_subtitles(tmp, srt_path, dst, language=lang)
                            finally:
                                try:
                                    os.remove(tmp)
                                except OSError:
                                    pass
                else:
                    # MoviePy path (no burn capability here); convert then (optionally) soft-mux
                    # MoviePy path supports only color background
                    if bg_mode == "blur":
                        raise RuntimeError("Blurred background requires FFmpeg engine.")
                    convert_to_9x16(src, dst, w, h, rgb)
                    if srt_path and not self.burn_subs_var.get() and not tiktok_mode:
                        tmp = dst + ".tmp.mp4"
                        try:
                            os.replace(dst, tmp)
                            lang = self.subs_lang_var.get().strip() or None
                            mux_soft_subtitles(tmp, srt_path, dst, language=lang)
                        finally:
                            try:
                                os.remove(tmp)
                            except OSError:
                                pass

                # TikTok mode: split exported 9:16, then subtitle per segment
                if tiktok_mode:
                    self.after(0, lambda: self.status_text.set("TikTok: splitting into segments..."))
                    ffmpeg = iio_ffmpeg.get_ffmpeg_exe()
                    seg_minutes = max(1, int(self.tiktok_min_var.get() or 1))
                    seg_seconds = seg_minutes * 60
                    tmpdir = tempfile.mkdtemp(prefix="ttkseg_")
                    try:
                        seg_pattern = os.path.join(tmpdir, "seg_%03d.mp4")
                        cmd = [
                            ffmpeg,
                            "-hide_banner",
                            "-y",
                            "-i",
                            dst,
                            "-c",
                            "copy",
                            "-map",
                            "0",
                            "-f",
                            "segment",
                            "-segment_time",
                            str(seg_seconds),
                            "-reset_timestamps",
                            "1",
                            seg_pattern,
                        ]
                        proc = subprocess.run(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                        )
                        if proc.returncode != 0:
                            raise RuntimeError(f"FFmpeg segment failed: {proc.stderr}")

                        seg_files = sorted(glob.glob(os.path.join(tmpdir, "seg_*.mp4")))
                        if not seg_files:
                            raise RuntimeError("No segments were produced")

                        base_dir = os.path.dirname(dst) or "."
                        base_root = os.path.splitext(os.path.basename(src))[0]
                        lang = self.subs_lang_var.get().strip() or None
                        outputs = []
                        total = len(seg_files)

                        for i, seg in enumerate(seg_files, start=1):
                            self.after(0, lambda i=i, total=total: self.status_text.set(f"TikTok: subtitles {i}/{total}"))
                            srt_seg = os.path.join(tmpdir, f"seg_{i:03d}.srt")
                            transcribe_to_srt(seg, srt_seg, model_size=self.subs_model_var.get(), language=lang)

                            final_out = os.path.join(base_dir, f"{base_root}_tiktok_{i}.mp4")
                            if self.burn_subs_var.get():
                                force_style = build_subtitle_force_style(
                                    font_name=self.subs_font_var.get().strip() or "Arial",
                                    font_size=int(self.subs_size_var.get()),
                                    primary_hex=self.subs_color_hex.get().strip() or "#FFFFFF",
                                    outline_hex=self.subs_outline_hex.get().strip() or "#000000",
                                    border_style=3 if self.subs_box_bg_var.get() else 1,
                                    outline=int(self.subs_outline_var.get()),
                                    shadow=int(self.subs_shadow_var.get()),
                                    alignment=self.subs_align_var.get(),
                                    margin_v=int(self.subs_margin_var.get()),
                                )
                                subexpr = f"subtitles={_escape_subtitles_path_for_filter(srt_seg)}:force_style={force_style}"
                                # Choose codec
                                use_nvenc = False
                                eng_label = self.engine_var.get()
                                if eng_label == "FFmpeg (NVIDIA)":
                                    use_nvenc = True
                                elif eng_label == "FFmpeg (auto)" and self._has_nvenc():
                                    use_nvenc = True
                                if use_nvenc:
                                    vcodec = ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "23"]
                                else:
                                    vcodec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(self.crf_var.get()))]
                                cmd_burn = [
                                    ffmpeg,
                                    "-y",
                                    "-i",
                                    seg,
                                    "-vf",
                                    subexpr,
                                    "-movflags",
                                    "+faststart",
                                    *vcodec,
                                    "-c:a",
                                    "aac",
                                    "-b:a",
                                    "192k",
                                    final_out,
                                ]
                                p2 = subprocess.run(
                                    cmd_burn,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    text=True,
                                    encoding="utf-8",
                                    errors="replace",
                                )
                                if p2.returncode != 0:
                                    raise RuntimeError(f"FFmpeg burn subtitles failed: {p2.stderr}")
                            else:
                                mux_soft_subtitles(seg, srt_seg, final_out, language=lang)
                            outputs.append(final_out)

                            if self.tt_autopost.get():
                                try:
                                    self.after(0, lambda i=i: self.status_text.set(f"TikTok: uploading {i}/{total}"))
                                    ck = (self.tt_client_key.get() or "").strip()
                                    if not ck:
                                        raise RuntimeError("Missing TikTok client key")
                                    upload_video(ck, final_out, caption=os.path.basename(final_out))
                                except Exception as e:
                                    # Continue queue but notify
                                    messagebox.showwarning("TikTok", f"Upload failed for segment {i}: {e}")

                        done_msg = f"Created {len(outputs)} TikTok files in:\n{base_dir}"
                    finally:
                        shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception as e:
                ok = False
                err = e
            finally:
                def done():
                    self._set_busy(False)
                    if ok:
                        self.status_text.set("Done ✔")
                        if done_msg:
                            messagebox.showinfo("Done", done_msg)
                        else:
                            messagebox.showinfo("Done", f"Saved to\n{dst}")
                    else:
                        self.status_text.set("Failed ✖")
                        messagebox.showerror("Error", f"Conversion failed:\n{err}")
                self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    # --- Fastest auto mode ---
    def _has_nvenc(self) -> bool:
        try:
            ffmpeg = iio_ffmpeg.get_ffmpeg_exe()
            proc = subprocess.run([ffmpeg, "-hide_banner", "-h", "encoder=h264_nvenc"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return proc.returncode == 0
        except Exception:
            return False

    def fastest_convert(self):
        # Validate input first
        src = self.input_path.get().strip()
        if not src or not os.path.isfile(src):
            messagebox.showwarning("Input", "Please choose a valid input video file.")
            return
        # Determine output default if empty
        if not self.output_path.get().strip():
            root, _ = os.path.splitext(src)
            self.output_path.set(root + "_9x16.mp4")

        # Pick fastest settings
        if self._has_nvenc():
            # GPU path: usually fastest without chunking
            self.engine_var.set("FFmpeg (NVIDIA)")
            self.parallel_var.set(False)
        else:
            # CPU path: enable chunking with moderate CRF and jobs
            self.engine_var.set("FFmpeg (CPU)")
            self.crf_var.set(23)
            self.parallel_var.set(True)
            self.seg_var.set(30)
            jobs = min(4, max(2, (os.cpu_count() or 2) // 2))
            self.jobs_var.set(jobs)

        # Start conversion with these settings
        self.start_convert()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
