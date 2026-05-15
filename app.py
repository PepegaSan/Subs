#!/usr/bin/env python3
"""Desktop-Tool: Text und Untertitel ins Video einbetten, Vorschau, FFmpeg- und DaVinci-Export."""

from __future__ import annotations

import io
import json
import queue
import shutil
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from overlay_core import (
    FFmpegNotFoundError,
    ProbeError,
    TimedTextOverlay,
    build_export_command,
    extract_frame_png_bytes,
    extract_preview_frame_with_overlay,
    find_ffmpeg,
    map_codec_to_lib,
    overlay_segment_has_visible_text,
    parse_bitrate,
    parse_time_seconds,
    probe_video,
    suggest_ffmpeg_bitrate_from_bps,
)
from os_fonts import list_windows_font_choices, preferred_font_dialog_initialdir

try:
    from PIL import Image, ImageTk
except ImportError as exc:  # pragma: no cover - startup guard
    raise SystemExit(
        "Pillow fehlt. Bitte installieren: pip install Pillow\n" + str(exc)
    ) from exc


_FONT_PREVIEW_SAMPLE = (
    "Aa Bb Çç 12345\n"
    "Der braune Fuchs springt.\n"
    "ÄÖÜ äöü ß «Untertitel»"
)


def _tk_family_from_registry_label(registry_display_name: str) -> str:
    """Aus Registry-Anzeigenamen einen Tk-/Windows-Schriftfamiliennamen machen."""
    name = (registry_display_name or "").strip()
    for marker in (" (TrueType)", " (OpenType)", " (All Fonts)", " (ALL)"):
        if name.endswith(marker):
            name = name[: -len(marker)].strip()
            break
    if name.startswith("@"):
        name = name[1:].strip()
    return name


def _sanitize_tk_font_family(name: str) -> str:
    """Tk/Charmap kann bei Sonderzeichen in Schriftnamen abstürzen oder fehlschlagen."""
    s = (name or "").strip().replace("\x00", "")
    if not s:
        return "Segoe UI"
    for ch in "{}[]\"\\":
        s = s.replace(ch, " ")
    s = s.replace("&", " ")
    s = " ".join(s.split())
    if len(s) > 96:
        s = s[:96].rstrip()
    return s or "Segoe UI"


class _WindowsFontPickerDialog(tk.Toplevel):
    """Modal: installierte Windows-Schriften (Registry + Fonts-Ordner), mit Filter."""

    def __init__(
        self,
        master: tk.Misc,
        pairs: list[tuple[str, str]],
        *,
        title: str,
    ) -> None:
        super().__init__(master)
        self.title(title)
        self.transient(master)
        self.lift(master)
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self._pairs = pairs
        self._filtered = list(pairs)
        self._result: str | None = None

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Teilname eingeben zum Filtern:").pack(anchor="w")
        self.var_filter = tk.StringVar()
        ent = ttk.Entry(outer, textvariable=self.var_filter)
        ent.pack(fill="x", pady=(4, 6))
        ent.bind("<KeyRelease>", self._on_filter)
        ent.focus_set()

        mid = ttk.Frame(outer)
        mid.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(mid)
        scroll.pack(side="right", fill="y")
        self.lb = tk.Listbox(mid, height=12, yscrollcommand=scroll.set, exportselection=False)
        self.lb.pack(side="left", fill="both", expand=True)
        scroll.config(command=self.lb.yview)
        self.lb.bind("<Double-Button-1>", lambda _e: self._ok())
        self.lb.bind("<<ListboxSelect>>", self._on_list_select)

        pv_fr = ttk.LabelFrame(outer, text="Vorschau (markierte Schrift)", padding=(8, 6))
        pv_fr.pack(fill="x", pady=(10, 0))
        self._preview_pt = 14
        self.preview_lbl = tk.Label(
            pv_fr,
            text=_FONT_PREVIEW_SAMPLE,
            font=("Segoe UI", self._preview_pt),
            justify="left",
            anchor="w",
            bg="#FAFAFA",
            fg="#111111",
            padx=10,
            pady=10,
        )
        self.preview_lbl.pack(fill="x")

        self._refill_list()

        bf = ttk.Frame(outer)
        bf.pack(fill="x", pady=(10, 0))
        ttk.Button(bf, text="OK", command=self._ok).pack(side="right")
        ttk.Button(bf, text="Abbrechen", command=self._cancel).pack(side="right", padx=(0, 10))

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _on_list_select(self, _evt: object | None = None) -> None:
        try:
            sel = self.lb.curselection()
            if not sel:
                self.preview_lbl.configure(
                    text=_FONT_PREVIEW_SAMPLE + "\n\n— bitte eine Zeile wählen —",
                    fg="#666666",
                )
                return
            idx = int(sel[0])
            if idx < 0 or idx >= len(self._filtered):
                return
            label = self._filtered[idx][0]
            fam = _sanitize_tk_font_family(_tk_family_from_registry_label(label))
            try:
                self.preview_lbl.configure(
                    text=_FONT_PREVIEW_SAMPLE,
                    fg="#111111",
                    font=(fam, self._preview_pt),
                )
            except tk.TclError:
                self.preview_lbl.configure(
                    text=_FONT_PREVIEW_SAMPLE
                    + "\n\n(Vorschau in Tk nicht möglich — FFmpeg nutzt die gewählte Datei.)",
                    fg="#555555",
                    font=("Segoe UI", self._preview_pt),
                )
        except tk.TclError:
            pass

    def _on_filter(self, _evt: object | None = None) -> None:
        q = self.var_filter.get().strip().lower()
        if not q:
            self._filtered = list(self._pairs)
        else:
            self._filtered = [p for p in self._pairs if q in p[0].lower()]
        self._refill_list()

    def _refill_list(self) -> None:
        self.lb.delete(0, tk.END)
        for label, _p in self._filtered:
            self.lb.insert(tk.END, label)
        if self._filtered:
            self.lb.selection_clear(0, tk.END)
            self.lb.selection_set(0)
            self.lb.activate(0)
            self.lb.see(0)
            self.after_idle(self._on_list_select)
        else:
            self.preview_lbl.configure(
                text="— keine Treffer —",
                fg="#666666",
                font=("Segoe UI", self._preview_pt),
            )

    def _ok(self) -> None:
        sel = self.lb.curselection()
        if not sel:
            messagebox.showinfo("Schrift", "Bitte eine Zeile markieren.", parent=self)
            return
        idx = int(sel[0])
        if 0 <= idx < len(self._filtered):
            self._result = self._filtered[idx][1]
        self._release_grab_safe()
        self.destroy()

    def _cancel(self) -> None:
        self._result = None
        self._release_grab_safe()
        self.destroy()

    def _release_grab_safe(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass

    def chosen_path(self) -> str | None:
        try:
            self.update_idletasks()
        except tk.TclError:
            pass
        self.wait_window()
        return self._result


try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _TkBase = TkinterDnD.Tk
except ImportError:
    _TkBase = tk.Tk
    DND_FILES = None  # type: ignore[misc, assignment]


SETTINGS_NAME = "video_text_tool_settings.json"

VIDEO_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".mov",
        ".mkv",
        ".webm",
        ".avi",
        ".mxf",
        ".m4v",
        ".wmv",
        ".gif",  # animiert oder Standbild — FFmpeg/ffprobe wie Video
    }
)

EXPORT_FMT_MP4 = "MP4 (Video)"
EXPORT_FMT_GIF = "GIF (animiert)"

# Hex ohne # für var_color / FFmpeg
BASIC_COLOR_PALETTE = (
    ("FFFFFF", "Weiß"),
    ("000000", "Schwarz"),
    ("FF0000", "Rot"),
    ("00CC00", "Grün"),
    ("0066FF", "Blau"),
    ("FFFF00", "Gelb"),
    ("FF00CC", "Magenta"),
    ("00DDDD", "Cyan"),
    ("FF8800", "Orange"),
    ("888888", "Grau"),
)


def parse_optional_seconds(text: str) -> float | None:
    s = (text or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def settings_path() -> Path:
    return Path(__file__).resolve().parent / SETTINGS_NAME


def parse_dnd_file_list(data: str) -> list[str]:
    """Wandelt TkDND *event.data* (Tcl-Listen mit `{…}` für Leerzeichen) in Pfadstrings."""
    data = (data or "").strip()
    if not data:
        return []
    paths: list[str] = []
    i = 0
    n = len(data)
    while i < n:
        while i < n and data[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        if data[i] == "{":
            depth = 0
            j = i
            while j < n:
                if data[j] == "{":
                    depth += 1
                elif data[j] == "}":
                    depth -= 1
                    if depth == 0:
                        paths.append(data[i + 1 : j])
                        i = j + 1
                        break
                j += 1
            else:
                break
        else:
            j = i
            while j < n and data[j] not in " \t\r\n":
                j += 1
            chunk = data[i:j].strip()
            if chunk:
                paths.append(chunk)
            i = j
    return paths


def _segment_dict_normalize(raw: dict) -> dict:
    """JSON-Eintrag → konsistentes Dict für die UI."""
    def _i(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(int(raw.get(key, default)), hi))
        except (TypeError, ValueError):
            return default

    col = str(raw.get("color") or "FFFFFF").strip().lstrip("#").upper()
    if len(col) != 6:
        col = "FFFFFF"
    box_en = True
    if "box_enabled" in raw:
        box_en = _segment_bool(raw.get("box_enabled"))
    return {
        "text": str(raw.get("text") or ""),
        "from": str(raw.get("from") or "").strip(),
        "to": str(raw.get("to") or "").strip(),
        "fontsize": _i("fontsize", 42, 12, 200),
        "color": col,
        "px": _i("px", 80, 0, 20000),
        "py": _i("py", 80, 0, 20000),
        "line_spacing": _i("line_spacing", -12, -120, 120),
        "box_border": _i("box_border", 3, 0, 40),
        "box_enabled": box_en,
        "font_path": str(raw.get("font_path") or "").strip(),
        "italic_font_path": str(raw.get("italic_font_path") or "").strip(),
        "bold": _segment_bool(raw.get("bold")),
        "italic": _segment_bool(raw.get("italic")),
        "strike": _segment_bool(raw.get("strike")),
    }


def _segment_bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    try:
        return bool(int(raw))
    except (TypeError, ValueError):
        return False


def clamp_geometry(geo: str, screen_w: int, screen_h: int, min_w: int, min_h: int) -> str:
    try:
        parts = geo.replace("+", " ").split()
        wh = parts[0].split("x")
        w, h = int(wh[0]), int(wh[1])
        x = int(parts[1])
        y = int(parts[2])
    except (IndexError, ValueError):
        return f"{min_w}x{min_h}+80+80"
    w = max(min_w, min(w, screen_w))
    h = max(min_h, min(h, screen_h))
    x = max(0, min(x, max(0, screen_w - w)))
    y = max(0, min(y, max(0, screen_h - h)))
    return f"{w}x{h}+{x}+{y}"


class VideoTextApp(_TkBase):
    def __init__(self) -> None:
        super().__init__()
        self.title("Video-Text & Untertitel")
        self.minsize(960, 640)

        self.settings: dict = {}
        self._load_settings()

        self.video_path: str | None = None
        self.video_info = None
        self.current_time = 0.0
        self.playing = False
        self._play_after_id: str | None = None

        self.overlay_segments: list[dict] = []
        self._segment_loading = False

        self._configure_save_job: str | None = None
        self._photo_ref = None
        self._last_disp: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._preview_src_im: Image.Image | None = None
        self._preview_src_size: tuple[int, int] | None = None
        self._pz_zoom = 1.0
        self._pz_ox: int | None = None
        self._pz_oy: int | None = None
        self._pan_r_mark: tuple[int, int, int | None, int | None] | None = None
        self._preview_configure_job: str | None = None

        self.export_queue: queue.Queue = queue.Queue()
        self.last_ffmpeg_output: str | None = None
        self._text_refresh_job: str | None = None
        self._timing_preview_job: str | None = None

        self._preview_gen = 0
        self._scrub_preview_job: str | None = None
        self._last_play_preview_at = 0.0
        self._font_path_job: str | None = None

        self._build_ui()
        self.after(250, self._setup_drag_drop)
        self.after(200, self._apply_startup_geometry)
        self.after(100, self._poll_export_queue)

        self.bind("<Configure>", self._on_root_configure)

    # --- Settings ---

    def _load_settings(self) -> None:
        p = settings_path()
        if p.is_file():
            try:
                self.settings = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.settings = {}
        else:
            self.settings = {}

    def _save_settings(self) -> None:
        try:
            self.settings["geometry"] = self.geometry()
            try:
                self.settings["box_border_w"] = int(self.var_box_border.get())
                self.settings["line_spacing_px"] = int(self.var_line_spacing.get())
                self.settings["text_visible_from"] = self.var_text_from.get().strip()
                self.settings["text_visible_to"] = self.var_text_to.get().strip()
                self.settings["overlay_segments"] = list(self.overlay_segments)
                self.settings["export_container"] = (
                    "gif" if self.var_export_fmt.get() == EXPORT_FMT_GIF else "mp4"
                )
                self.settings["gif_fps"] = max(1, min(int(self.var_gif_fps.get()), 60))
                self.settings["gif_max_width"] = max(160, min(int(self.var_gif_max_w.get()), 1920))
                self.settings["gif_palette_colors"] = max(
                    8, min(int(self.var_gif_colors.get()), 256)
                )
            except (tk.TclError, AttributeError, ValueError):
                pass
            settings_path().write_text(
                json.dumps(self.settings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _apply_startup_geometry(self) -> None:
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        geo = self.settings.get("geometry") or "1100x760+60+40"
        self.geometry(clamp_geometry(str(geo), sw, sh, 960, 640))

    def _on_root_configure(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        if self._configure_save_job:
            self.after_cancel(self._configure_save_job)
        self._configure_save_job = self.after(500, self._debounced_save_geometry)

    def _debounced_save_geometry(self) -> None:
        self._configure_save_job = None
        self._save_settings()

    # --- Drag & Drop (tkinterdnd2 / TkDND — kein ctypes-WndProc, stabil mit Python 3.12) ---

    def _setup_drag_drop(self) -> None:
        if DND_FILES is None:
            self.lbl_drop.configure(
                text="Drag & Drop: pip install tkinterdnd2 — sonst „Video laden …“ nutzen."
            )
            return
        self.update_idletasks()
        try:
            # Nur das Root-Fenster — sonst feuern Canvas + Root oft doppelt <<Drop>>.
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_tkdnd_drop)
            self.lbl_drop.configure(
                text="Video oder SRT hierher ziehen (Fenster oder graue Vorschau)"
            )
        except Exception as exc:  # pragma: no cover
            traceback.print_exc()
            self.lbl_drop.configure(text=f"Drag & Drop nicht verfügbar: {exc}")

    def _on_tkdnd_drop(self, event: tk.Event) -> None:
        raw = getattr(event, "data", "") or ""
        paths = parse_dnd_file_list(raw)
        if not paths and raw:
            paths = [raw.strip()]
        self._apply_dropped_path_strings(paths)

    def _apply_dropped_path_strings(self, paths: list[str]) -> None:
        try:
            self._handle_dropped_paths([Path(p) for p in paths])
        except Exception as exc:
            traceback.print_exc()
            messagebox.showerror(
                "Drag & Drop",
                f"Die abgelegten Dateien konnten nicht verarbeitet werden:\n{exc}",
                parent=self,
            )

    def _handle_dropped_paths(self, paths: list[Path]) -> None:
        video: Path | None = None
        srt_file: Path | None = None
        for p in paths:
            if not p.is_file():
                continue
            suf = p.suffix.lower()
            if suf in VIDEO_EXTENSIONS and video is None:
                video = p
            elif suf == ".srt" and srt_file is None:
                srt_file = p
        if video is not None:
            self.settings["last_video_dir"] = str(video.parent)
            self._save_settings()
            self._load_video(str(video))
        if srt_file is not None:
            self.var_srt_path.set(str(srt_file))
            self.lbl_srt.configure(text=srt_file.name)
            self.refresh_preview()
        if video is None and srt_file is None:
            messagebox.showwarning(
                "Drag & Drop",
                "Keine unterstützte Datei erkannt. Unterstützt werden u. a.\n"
                + ", ".join(sorted(VIDEO_EXTENSIONS))
                + " sowie .srt",
                parent=self,
            )

    # --- UI ---

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)

        ttk.Button(top, text="Video laden …", command=self._pick_video).grid(
            row=0, column=0, sticky="w"
        )
        self.lbl_video = ttk.Label(top, text="Keine Datei geladen")
        self.lbl_video.grid(row=0, column=1, sticky="w", padx=8)
        ttk.Button(top, text="Einstellungen …", command=self._open_settings).grid(
            row=0, column=2, sticky="e"
        )

        preview = ttk.LabelFrame(top, text="Vorschau", padding=6)
        preview.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        preview.columnconfigure(0, weight=1)
        top.rowconfigure(1, weight=2)

        self.lbl_drop = ttk.Label(
            preview,
            text="Video oder SRT ins Fenster ziehen (Drag & Drop)",
            foreground="#555555",
        )
        self.lbl_drop.grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.canvas = tk.Canvas(
            preview,
            width=960,
            height=540,
            background="#2b2b2b",
            highlightthickness=0,
        )
        self.canvas.grid(row=1, column=0, sticky="nsew")
        preview.rowconfigure(1, weight=2)
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_click)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<MouseWheel>", self._on_preview_wheel)
        self.canvas.bind("<Button-4>", lambda e: self._preview_wheel_common(e.x, e.y, True))
        self.canvas.bind("<Button-5>", lambda e: self._preview_wheel_common(e.x, e.y, False))
        self.canvas.bind("<Enter>", lambda _e: self.canvas.focus_set())
        self.canvas.bind("<ButtonPress-3>", self._on_preview_pan_press)
        self.canvas.bind("<B3-Motion>", self._on_preview_pan_move)
        self.canvas.bind("<ButtonRelease-3>", self._on_preview_pan_release)
        self.canvas.bind("<Configure>", self._on_preview_canvas_configure)

        ctl = ttk.Frame(preview)
        ctl.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.scale_time = tk.DoubleVar(value=0.0)
        self.slider = ttk.Scale(
            ctl,
            from_=0.0,
            to=1.0,
            variable=self.scale_time,
            command=self._on_scrub,
        )
        self.slider.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctl.columnconfigure(0, weight=1)
        ttk.Button(ctl, text="Abspielen", command=self._toggle_play).grid(
            row=0, column=1
        )
        self.lbl_time = ttk.Label(ctl, text="0:00 / 0:00")
        self.lbl_time.grid(row=0, column=2, padx=8)

        zf = ttk.Frame(preview)
        zf.grid(row=3, column=0, sticky="ew", pady=(2, 0))
        ttk.Label(zf, text="Vorschau:", foreground="#555555").pack(side="left")
        ttk.Button(zf, text="−", width=2, command=lambda: self._preview_zoom_step(False)).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(zf, text="+", width=2, command=lambda: self._preview_zoom_step(True)).pack(
            side="left", padx=(4, 0)
        )
        ttk.Button(zf, text="Zoom 100%", command=self._preview_zoom_reset).pack(
            side="left", padx=(10, 0)
        )
        ttk.Label(
            zf,
            text="Mausrad zoomt zur Maus · Rechts klicken und ziehen schwenkt",
            foreground="#777777",
        ).pack(side="left", padx=(14, 0))

        seg_fr = ttk.LabelFrame(
            top,
            text="Text-Abschnitte — Zeitfenster & Darstellung pro Eintrag",
            padding=6,
        )
        seg_fr.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        seg_inner = ttk.Frame(seg_fr)
        seg_inner.pack(fill="both", expand=True)
        seg_inner.columnconfigure(0, weight=1)
        self.lbox_segments = tk.Listbox(
            seg_inner,
            height=5,
            width=82,
            exportselection=False,
            activestyle="dotbox",
        )
        seg_sb = ttk.Scrollbar(seg_inner, orient="vertical", command=self.lbox_segments.yview)
        self.lbox_segments.configure(yscrollcommand=seg_sb.set)
        self.lbox_segments.grid(row=0, column=0, sticky="nsew")
        seg_sb.grid(row=0, column=1, sticky="ns")
        seg_btns = ttk.Frame(seg_fr)
        seg_btns.pack(fill="x", pady=(6, 0))
        ttk.Button(seg_btns, text="Neuer Abschnitt", command=self._segment_new).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(seg_btns, text="Übernehmen", command=self._segment_commit).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(seg_btns, text="Löschen", command=self._segment_delete).pack(side="left")
        self.lbox_segments.bind("<<ListboxSelect>>", self._on_segment_select)

        text_fr = ttk.LabelFrame(top, text="Aktueller Abschnitt (bearbeiten)", padding=6)
        text_fr.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        r = 0
        ttk.Label(text_fr, text="Text").grid(row=r, column=0, sticky="nw")
        text_holder = ttk.Frame(text_fr)
        text_holder.grid(row=r, column=1, rowspan=2, sticky="ew", padx=6)
        text_holder.columnconfigure(0, weight=1)
        self.txt_overlay = tk.Text(text_holder, height=4, width=60, wrap="none")
        sx = ttk.Scrollbar(text_holder, orient="horizontal", command=self.txt_overlay.xview)
        self.txt_overlay.configure(xscrollcommand=sx.set)
        self.txt_overlay.grid(row=0, column=0, sticky="ew")
        sx.grid(row=1, column=0, sticky="ew")
        try:
            self.txt_overlay.configure(spacing1=0, spacing2=0, spacing3=0)
        except tk.TclError:
            pass
        self.txt_overlay.bind("<KeyRelease>", self._on_text_key)
        self.txt_overlay.bind("<Return>", self._overlay_insert_newline)
        self.txt_overlay.bind("<KP_Enter>", self._overlay_insert_newline)
        ttk.Button(text_fr, text="(i)", width=3, command=self._info_text).grid(
            row=r, column=2, sticky="ne"
        )
        text_fr.columnconfigure(1, weight=1)

        r = 2
        ttk.Label(text_fr, text="Schriftdatei").grid(row=r, column=0, sticky="nw")
        fon_fr = ttk.Frame(text_fr)
        fon_fr.grid(row=r, column=1, columnspan=2, sticky="ew", padx=6)
        fon_fr.columnconfigure(0, weight=1)
        self.var_font_path = tk.StringVar(value="")
        self.ent_font_path = ttk.Entry(fon_fr, textvariable=self.var_font_path, width=48)
        self.ent_font_path.grid(row=0, column=0, columnspan=2, sticky="ew")
        fbtn = ttk.Frame(fon_fr)
        fbtn.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(fbtn, text="Datei …", command=self._pick_overlay_font, width=11).pack(
            side="left"
        )
        ttk.Button(
            fbtn,
            text="Aus Windows …",
            command=self._pick_overlay_font_windows,
            width=14,
        ).pack(side="left", padx=(8, 0))
        ttk.Label(
            fon_fr,
            text="(.ttf / .otf — leer = FFmpeg-Standard; „Aus Windows“ = installierte Schriften)",
            foreground="#666666",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.var_font_path.trace_add("write", lambda *_: self._schedule_font_path_preview())

        r = 3
        ttk.Label(text_fr, text="Kursiv-Schrift (optional)").grid(row=r, column=0, sticky="nw")
        it_fr = ttk.Frame(text_fr)
        it_fr.grid(row=r, column=1, columnspan=2, sticky="ew", padx=6)
        it_fr.columnconfigure(0, weight=1)
        self.var_italic_font_path = tk.StringVar(value="")
        ttk.Entry(it_fr, textvariable=self.var_italic_font_path, width=48).grid(
            row=0, column=0, columnspan=2, sticky="ew"
        )
        ibtn = ttk.Frame(it_fr)
        ibtn.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(ibtn, text="Datei …", command=self._pick_italic_overlay_font, width=11).pack(
            side="left"
        )
        ttk.Button(
            ibtn,
            text="Aus Windows …",
            command=self._pick_italic_font_windows,
            width=14,
        ).pack(side="left", padx=(8, 0))
        self.var_italic_font_path.trace_add("write", lambda *_: self._schedule_font_path_preview())

        r = 4
        ttk.Label(text_fr, text="Stil").grid(row=r, column=0, sticky="w")
        st_fr = ttk.Frame(text_fr)
        st_fr.grid(row=r, column=1, columnspan=2, sticky="w", padx=6)
        self.var_font_bold = tk.BooleanVar(value=False)
        self.var_font_italic = tk.BooleanVar(value=False)
        self.var_font_strike = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            st_fr, text="Fett", variable=self.var_font_bold, command=self.refresh_preview
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(
            st_fr, text="Kursiv", variable=self.var_font_italic, command=self.refresh_preview
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(
            st_fr,
            text="Durchgestrichen",
            variable=self.var_font_strike,
            command=self.refresh_preview,
        ).pack(side=tk.LEFT)

        r = 5
        ttk.Label(text_fr, text="Schriftgröße").grid(row=r, column=0, sticky="w")
        self.var_fontsize = tk.IntVar(value=42)
        ttk.Spinbox(
            text_fr,
            from_=12,
            to=200,
            textvariable=self.var_fontsize,
            width=6,
            command=self.refresh_preview,
        ).grid(row=r, column=1, sticky="w", padx=6)

        r = 6
        ttk.Label(text_fr, text="Zeilenabstand (Pixel)").grid(row=r, column=0, sticky="w")
        try:
            ls_default = int(self.settings.get("line_spacing_px", -12))
        except (TypeError, ValueError):
            ls_default = -12
        self.var_line_spacing = tk.IntVar(value=max(-120, min(ls_default, 120)))
        ttk.Spinbox(
            text_fr,
            from_=-120,
            to=120,
            textvariable=self.var_line_spacing,
            width=6,
            command=self.refresh_preview,
        ).grid(row=r, column=1, sticky="w", padx=6)
        ttk.Label(
            text_fr,
            text="(negativ = enger im Video)",
            foreground="#666666",
        ).grid(row=r, column=2, sticky="w")

        r = 7
        ttk.Label(text_fr, text="Farbe (#RRGGBB)").grid(row=r, column=0, sticky="w")
        self.var_color = tk.StringVar(value="FFFFFF")
        ttk.Entry(text_fr, textvariable=self.var_color, width=12).grid(
            row=r, column=1, sticky="w", padx=6
        )

        r = 8
        ttk.Label(text_fr, text="Farbpalette").grid(row=r, column=0, sticky="nw")
        pal_fr = ttk.Frame(text_fr)
        pal_fr.grid(row=r, column=1, sticky="w", padx=6)
        for hx, _name in BASIC_COLOR_PALETTE:
            bg = f"#{hx}"
            btn = tk.Button(
                pal_fr,
                width=2,
                height=1,
                bg=bg,
                activebackground=bg,
                relief=tk.RIDGE,
                bd=1,
                highlightthickness=1 if hx.upper() == "FFFFFF" else 0,
                highlightbackground="#999999",
                command=lambda h=hx: self._pick_palette_color(h),
            )
            if hx.upper() == "000000":
                btn.configure(fg="#CCCCCC")
            btn.pack(side=tk.LEFT, padx=(0, 5), pady=2)

        r = 9
        ttk.Label(text_fr, text="Position (Pixel im Videobild)").grid(row=r, column=0, sticky="w")
        pos = ttk.Frame(text_fr)
        pos.grid(row=r, column=1, sticky="w", padx=6)
        self.var_px = tk.IntVar(value=80)
        self.var_py = tk.IntVar(value=80)
        ttk.Label(pos, text="X").pack(side="left")
        ttk.Spinbox(
            pos,
            from_=0,
            to=20000,
            textvariable=self.var_px,
            width=7,
            command=self.refresh_preview,
        ).pack(side="left", padx=(4, 12))
        ttk.Label(pos, text="Y").pack(side="left")
        ttk.Spinbox(
            pos,
            from_=0,
            to=20000,
            textvariable=self.var_py,
            width=7,
            command=self.refresh_preview,
        ).pack(side="left", padx=(4, 0))

        r = 10
        ttk.Label(text_fr, text="Texthintergrund").grid(row=r, column=0, sticky="w")
        self.var_box_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            text_fr,
            text="Abgeschatteter Kasten",
            variable=self.var_box_enabled,
            command=self.refresh_preview,
        ).grid(row=r, column=1, sticky="w", padx=6)
        ttk.Label(
            text_fr,
            text="(aus = nur Schrift auf Video)",
            foreground="#666666",
        ).grid(row=r, column=2, sticky="w")

        r = 11
        ttk.Label(text_fr, text="Rahmenabstand (Pixel)").grid(row=r, column=0, sticky="w")
        try:
            bb_default = int(self.settings.get("box_border_w", 3))
        except (TypeError, ValueError):
            bb_default = 3
        self.var_box_border = tk.IntVar(value=max(0, min(bb_default, 40)))
        ttk.Spinbox(
            text_fr,
            from_=0,
            to=40,
            textvariable=self.var_box_border,
            width=6,
            command=self.refresh_preview,
        ).grid(row=r, column=1, sticky="w", padx=6)
        ttk.Label(
            text_fr,
            text="(nur mit Kasten)",
            foreground="#666666",
        ).grid(row=r, column=2, sticky="w")

        r = 12
        ttk.Label(text_fr, text="Text nur zwischen (Sekunden)").grid(
            row=r, column=0, sticky="nw"
        )
        twrap = ttk.Frame(text_fr)
        twrap.grid(row=r, column=1, sticky="w", padx=6)
        self.var_text_from = tk.StringVar(
            value=str(self.settings.get("text_visible_from", "") or "")
        )
        self.var_text_to = tk.StringVar(
            value=str(self.settings.get("text_visible_to", "") or "")
        )
        ttk.Label(twrap, text="von").pack(side="left")
        ttk.Entry(twrap, textvariable=self.var_text_from, width=9).pack(
            side="left", padx=(4, 4)
        )
        ttk.Button(
            twrap,
            text="Einfügen",
            width=10,
            command=self._apply_playhead_to_text_from,
        ).pack(side="left", padx=(0, 10))
        ttk.Label(twrap, text="bis").pack(side="left")
        ttk.Entry(twrap, textvariable=self.var_text_to, width=9).pack(
            side="left", padx=(4, 4)
        )
        ttk.Button(
            twrap,
            text="Einfügen",
            width=10,
            command=self._apply_playhead_to_text_to,
        ).pack(side="left")
        self.var_text_from.trace_add("write", lambda *_: self._schedule_timing_preview())
        self.var_text_to.trace_add("write", lambda *_: self._schedule_timing_preview())

        sub_fr = ttk.LabelFrame(top, text="Untertitel (optional)", padding=6)
        sub_fr.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Button(sub_fr, text="SRT wählen …", command=self._pick_srt).pack(side="left")
        self.lbl_srt = ttk.Label(sub_fr, text="Keine SRT-Datei")
        self.lbl_srt.pack(side="left", padx=8)
        ttk.Button(sub_fr, text="(i)", width=3, command=self._info_subs).pack(side="right")

        self.var_srt_path = tk.StringVar(value="")

        ff = ttk.LabelFrame(top, text="Export mit FFmpeg", padding=6)
        ff.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        _saved_fmt = str(self.settings.get("export_container", "mp4")).lower()
        _fmt_init = EXPORT_FMT_GIF if _saved_fmt == "gif" else EXPORT_FMT_MP4

        ttk.Label(ff, text="Ausgabe").grid(row=0, column=0, sticky="w")
        self.var_export_fmt = tk.StringVar(value=_fmt_init)
        self.cmb_export_fmt = ttk.Combobox(
            ff,
            values=[EXPORT_FMT_MP4, EXPORT_FMT_GIF],
            textvariable=self.var_export_fmt,
            width=14,
            state="readonly",
        )
        self.cmb_export_fmt.grid(row=0, column=1, sticky="w", padx=(4, 16))
        self.var_export_fmt.trace_add("write", lambda *_: self._sync_export_format_widgets())

        ttk.Label(ff, text="Codec").grid(row=0, column=2, sticky="w")
        codecs = [
            "H.264 (libx264)",
            "H.265 / HEVC (libx265)",
            "VP9 (libvpx-vp9)",
            "AV1 (libsvtav1)",
        ]
        self.var_codec = tk.StringVar(value=self.settings.get("codec", codecs[0]))
        self.cmb_codec = ttk.Combobox(
            ff,
            values=codecs,
            textvariable=self.var_codec,
            width=26,
            state="readonly",
        )
        self.cmb_codec.grid(row=0, column=3, sticky="w", padx=6)

        ttk.Label(ff, text="Bitrate").grid(row=0, column=4, sticky="w")
        self.var_bitrate = tk.StringVar(value=self.settings.get("bitrate", "8M"))
        br_fr = ttk.Frame(ff)
        br_fr.grid(row=0, column=5, sticky="w", padx=6)
        self.ent_bitrate = ttk.Entry(br_fr, textvariable=self.var_bitrate, width=10)
        self.ent_bitrate.pack(side="left")
        ttk.Button(br_fr, text="Quelle", command=self._apply_source_bitrate, width=7).pack(
            side="left", padx=(6, 0)
        )

        ttk.Button(ff, text="Mit FFmpeg encodieren …", command=self._export_ffmpeg).grid(
            row=0, column=6, rowspan=3, padx=(16, 0), sticky="ne"
        )
        ttk.Button(ff, text="(i)", width=3, command=self._info_ffmpeg).grid(
            row=0, column=7, rowspan=3, padx=(6, 0), sticky="ne"
        )

        try:
            _gf = int(self.settings.get("gif_fps", 15))
        except (TypeError, ValueError):
            _gf = 15
        try:
            _gw = int(self.settings.get("gif_max_width", 720))
        except (TypeError, ValueError):
            _gw = 720
        try:
            _gc = int(self.settings.get("gif_palette_colors", 128))
        except (TypeError, ValueError):
            _gc = 128

        r_gif = 1
        ttk.Label(ff, text="GIF FPS").grid(row=r_gif, column=0, sticky="w", pady=(8, 0))
        self.var_gif_fps = tk.IntVar(value=max(1, min(_gf, 60)))
        self.spin_gif_fps = ttk.Spinbox(
            ff, from_=1, to=60, textvariable=self.var_gif_fps, width=5
        )
        self.spin_gif_fps.grid(row=r_gif, column=1, sticky="w", padx=(4, 16), pady=(8, 0))

        ttk.Label(ff, text="GIF max. Breite").grid(row=r_gif, column=2, sticky="w", pady=(8, 0))
        self.var_gif_max_w = tk.IntVar(value=max(160, min(_gw, 1920)))
        self.spin_gif_max_w = ttk.Spinbox(
            ff, from_=160, to=1920, textvariable=self.var_gif_max_w, width=6
        )
        self.spin_gif_max_w.grid(row=r_gif, column=3, sticky="w", padx=6, pady=(8, 0))

        ttk.Label(ff, text="GIF Palettenfarben").grid(row=r_gif, column=4, sticky="w", pady=(8, 0))
        self.var_gif_colors = tk.IntVar(value=max(8, min(_gc, 256)))
        self.spin_gif_colors = ttk.Spinbox(
            ff, from_=8, to=256, textvariable=self.var_gif_colors, width=5
        )
        self.spin_gif_colors.grid(row=r_gif, column=5, sticky="w", padx=6, pady=(8, 0))

        self.var_audio_copy = tk.BooleanVar(value=True)
        self.chk_audio_copy = ttk.Checkbutton(
            ff,
            text="Audio kopieren (falls möglich)",
            variable=self.var_audio_copy,
        )
        self.chk_audio_copy.grid(row=2, column=0, columnspan=6, sticky="w", pady=(6, 0))

        self.prog = ttk.Progressbar(ff, mode="determinate", maximum=100)
        self.prog.grid(row=3, column=0, columnspan=8, sticky="ew", pady=(8, 0))
        self.lbl_ff_status = ttk.Label(ff, text="")
        self.lbl_ff_status.grid(row=4, column=0, columnspan=8, sticky="w")

        self._sync_export_format_widgets()

        dv = ttk.LabelFrame(top, text="DaVinci Resolve", padding=6)
        dv.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(dv, text="Render-Preset (exakt wie in Resolve)").grid(
            row=0, column=0, sticky="w"
        )
        self.var_dv_preset = tk.StringVar(
            value=self.settings.get("davinci_preset", "YouTube - 1080p")
        )
        ttk.Entry(dv, textvariable=self.var_dv_preset, width=36).grid(
            row=0, column=1, sticky="w", padx=6
        )
        self.var_dv_use_last = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            dv,
            text="Letztes FFmpeg-Ergebnis verwenden (falls vorhanden)",
            variable=self.var_dv_use_last,
        ).grid(row=1, column=1, sticky="w", pady=(4, 0))
        ttk.Button(dv, text="In Resolve importieren & rendern …", command=self._export_resolve).grid(
            row=0, column=2, rowspan=2, padx=(12, 0), sticky="e"
        )
        ttk.Button(dv, text="(i)", width=3, command=self._info_resolve).grid(
            row=0, column=3, rowspan=2, padx=(6, 0)
        )

        self.status = ttk.Label(top, text="Bereit.")
        self.status.grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self._segments_load_from_settings()
        self._segments_refresh_listbox()

    def _overlay_text_body(self) -> str:
        """Tk „end“ enthält ein zusätzliches Newline — ``end-1c`` = nur Nutzerinhalt."""
        try:
            return self.txt_overlay.get("1.0", "end-1c")
        except tk.TclError:
            return ""

    def _overlay_text_payload(self) -> str:
        """Inhalt für FFmpeg; Zeilenenden vereinheitlicht, aber **ohne** äußeres strip.

        ``str.strip()`` würde ein nach Enter stehendes ``\\n`` (neue, noch leere Zeile)
        verwerfen — dann wirkt der Umbruch im Video nicht.
        """
        return self._overlay_text_body().replace("\r\n", "\n").replace("\r", "\n")

    def _overlay_has_visible_text(self) -> bool:
        """True, wenn überhaupt druckbare Zeichen im Overlay-Text stehen."""
        return bool(self._overlay_text_payload().strip())

    def _overlay_insert_newline(self, _evt: tk.Event | None = None) -> str:
        """Enter immer als Zeilenumbruch (unabhängig von Tk-/TkDND-Bindings)."""
        self.txt_overlay.insert(tk.INSERT, "\n")
        self._on_text_key()
        return "break"

    def _segments_load_from_settings(self) -> None:
        raw = self.settings.get("overlay_segments")
        if isinstance(raw, list) and raw:
            self.overlay_segments = [
                _segment_dict_normalize(x) for x in raw if isinstance(x, dict)
            ]
        else:
            self.overlay_segments = []

    def _segments_refresh_listbox(self) -> None:
        self.lbox_segments.delete(0, tk.END)
        for d in self.overlay_segments:
            self.lbox_segments.insert(tk.END, self._segment_list_label(d))

    def _segment_list_label(self, d: dict) -> str:
        a = (d.get("from") or "").strip() or "—"
        b = (d.get("to") or "").strip() or "—"
        snippet = (d.get("text") or "").replace("\n", " ").strip()
        if len(snippet) > 44:
            snippet = snippet[:41] + "…"
        return f"{a} → {b}  |  {snippet or '(kein Text)'}"

    def _dict_to_timed_overlay(self, d: dict) -> TimedTextOverlay:
        dn = _segment_dict_normalize(d)
        col = dn["color"]
        fp = (dn.get("font_path") or "").strip()
        font_path = fp if fp and Path(fp).is_file() else None
        ifp = (dn.get("italic_font_path") or "").strip()
        italic_font_path = ifp if ifp and Path(ifp).is_file() else None
        return TimedTextOverlay(
            text=dn["text"],
            fontcolor_hex=col,
            fontsize=dn["fontsize"],
            pos_x=dn["px"],
            pos_y=dn["py"],
            box_border_w=dn["box_border"],
            box_enabled=bool(dn.get("box_enabled", True)),
            line_spacing=dn["line_spacing"],
            text_visible_from_sec=parse_optional_seconds(dn["from"]),
            text_visible_to_sec=parse_optional_seconds(dn["to"]),
            font_path=font_path,
            italic_font_path=italic_font_path,
            bold=bool(dn.get("bold")),
            italic=bool(dn.get("italic")),
            strike=bool(dn.get("strike")),
        )

    def _form_timed_overlay(self) -> TimedTextOverlay:
        col = self.var_color.get().strip().lstrip("#") or "FFFFFF"
        if len(col) != 6:
            col = "FFFFFF"
        try:
            bb = max(0, min(int(self.var_box_border.get()), 40))
        except (tk.TclError, ValueError):
            bb = 3
        try:
            ls = max(-120, min(int(self.var_line_spacing.get()), 120))
        except (tk.TclError, ValueError):
            ls = -12
        try:
            fs = max(12, min(int(self.var_fontsize.get()), 200))
        except (tk.TclError, ValueError):
            fs = 42
        try:
            px = max(0, min(int(self.var_px.get()), 20000))
        except (tk.TclError, ValueError):
            px = 80
        try:
            py = max(0, min(int(self.var_py.get()), 20000))
        except (tk.TclError, ValueError):
            py = 80
        fpath = self.var_font_path.get().strip()
        font_path = fpath if fpath and Path(fpath).is_file() else None
        ifpath = self.var_italic_font_path.get().strip()
        italic_font_path = ifpath if ifpath and Path(ifpath).is_file() else None
        return TimedTextOverlay(
            text=self._overlay_text_payload(),
            fontcolor_hex=col.upper(),
            fontsize=fs,
            pos_x=px,
            pos_y=py,
            box_border_w=bb,
            box_enabled=bool(self.var_box_enabled.get()),
            line_spacing=ls,
            text_visible_from_sec=parse_optional_seconds(self.var_text_from.get()),
            text_visible_to_sec=parse_optional_seconds(self.var_text_to.get()),
            font_path=font_path,
            italic_font_path=italic_font_path,
            bold=bool(self.var_font_bold.get()),
            italic=bool(self.var_font_italic.get()),
            strike=bool(self.var_font_strike.get()),
        )

    def _snapshot_segment_dict(self) -> dict:
        o = self._form_timed_overlay()
        return {
            "text": o.text,
            "from": self.var_text_from.get().strip(),
            "to": self.var_text_to.get().strip(),
            "fontsize": o.fontsize,
            "color": o.fontcolor_hex,
            "px": o.pos_x,
            "py": o.pos_y,
            "line_spacing": o.line_spacing,
            "box_border": o.box_border_w,
            "box_enabled": 1 if self.var_box_enabled.get() else 0,
            "font_path": self.var_font_path.get().strip(),
            "italic_font_path": self.var_italic_font_path.get().strip(),
            "bold": 1 if self.var_font_bold.get() else 0,
            "italic": 1 if self.var_font_italic.get() else 0,
            "strike": 1 if self.var_font_strike.get() else 0,
        }

    def _overlays_for_engine(self) -> list[TimedTextOverlay]:
        merged = [self._dict_to_timed_overlay(d) for d in self.overlay_segments]
        sel = self.lbox_segments.curselection()
        if sel:
            idx = int(sel[0])
            if 0 <= idx < len(merged):
                merged[idx] = self._form_timed_overlay()
        elif self._overlay_has_visible_text():
            merged.append(self._form_timed_overlay())
        return [o for o in merged if overlay_segment_has_visible_text(o)]

    def _segment_apply_dict_to_form(self, d: dict) -> None:
        dn = _segment_dict_normalize(d)
        self._segment_loading = True
        try:
            self.txt_overlay.delete("1.0", tk.END)
            self.txt_overlay.insert("1.0", dn["text"])
            self.var_text_from.set(dn["from"])
            self.var_text_to.set(dn["to"])
            self.var_fontsize.set(dn["fontsize"])
            self.var_color.set(dn["color"])
            self.var_px.set(dn["px"])
            self.var_py.set(dn["py"])
            self.var_line_spacing.set(dn["line_spacing"])
            self.var_box_border.set(dn["box_border"])
            self.var_box_enabled.set(bool(dn.get("box_enabled", True)))
            self.var_font_path.set(dn["font_path"])
            self.var_italic_font_path.set(dn.get("italic_font_path") or "")
            self.var_font_bold.set(bool(dn["bold"]))
            self.var_font_italic.set(bool(dn["italic"]))
            self.var_font_strike.set(bool(dn["strike"]))
        finally:
            self._segment_loading = False

    def _on_segment_select(self, _evt: tk.Event | None = None) -> None:
        sel = self.lbox_segments.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.overlay_segments):
            self._segment_apply_dict_to_form(self.overlay_segments[idx])

    def _segment_new(self) -> None:
        sel = self.lbox_segments.curselection()
        src: dict | None = None
        if sel:
            i = int(sel[0])
            if 0 <= i < len(self.overlay_segments):
                src = self.overlay_segments[i]
        elif self.overlay_segments:
            src = self.overlay_segments[-1]

        self.lbox_segments.selection_clear(0, tk.END)
        self._segment_loading = True
        try:
            self.txt_overlay.delete("1.0", tk.END)
            self.var_text_from.set("")
            self.var_text_to.set("")
            if src is not None:
                dn = _segment_dict_normalize(src)
                self.var_fontsize.set(dn["fontsize"])
                self.var_color.set(dn["color"])
                self.var_px.set(dn["px"])
                self.var_py.set(dn["py"])
                self.var_line_spacing.set(dn["line_spacing"])
                self.var_box_border.set(dn["box_border"])
                self.var_box_enabled.set(bool(dn.get("box_enabled", True)))
                self.var_font_path.set(dn["font_path"])
                self.var_italic_font_path.set(dn.get("italic_font_path") or "")
                self.var_font_bold.set(bool(dn["bold"]))
                self.var_font_italic.set(bool(dn["italic"]))
                self.var_font_strike.set(bool(dn["strike"]))
            else:
                self.var_font_path.set("")
                self.var_italic_font_path.set("")
                self.var_box_enabled.set(True)
                self.var_font_bold.set(False)
                self.var_font_italic.set(False)
                self.var_font_strike.set(False)
        finally:
            self._segment_loading = False
        self.refresh_preview()

    def _segment_commit(self) -> None:
        if not self._overlay_has_visible_text():
            messagebox.showwarning(
                "Abschnitt",
                "Bitte zuerst sichtbaren Text eingeben.",
                parent=self,
            )
            return
        snap = self._snapshot_segment_dict()
        sel = self.lbox_segments.curselection()
        if sel:
            idx = int(sel[0])
            self.overlay_segments[idx] = snap
        else:
            self.overlay_segments.append(snap)
            idx = len(self.overlay_segments) - 1
        self._segments_refresh_listbox()
        self.lbox_segments.selection_set(idx)
        self.lbox_segments.see(idx)
        self._save_settings()
        self.refresh_preview()

    def _segment_delete(self) -> None:
        sel = self.lbox_segments.curselection()
        if not sel:
            messagebox.showwarning(
                "Abschnitt",
                "Bitte einen Eintrag in der Liste auswählen.",
                parent=self,
            )
            return
        idx = int(sel[0])
        del self.overlay_segments[idx]
        self._segments_refresh_listbox()
        self._save_settings()
        if self.overlay_segments:
            pick = min(idx, len(self.overlay_segments) - 1)
            self.lbox_segments.selection_set(pick)
            self._segment_apply_dict_to_form(self.overlay_segments[pick])
            self.refresh_preview()
        else:
            self._segment_new()

    def _playhead_seconds_for_timing_field(self) -> str:
        assert self.video_info is not None
        self.update_idletasks()
        try:
            t_wall = float(self.scale_time.get())
        except (tk.TclError, ValueError, TypeError):
            t_wall = float(self.current_time)
        t = max(
            0.0,
            min(t_wall, float(self.video_info.duration_sec)),
        )
        s = f"{t:.3f}".rstrip("0").rstrip(".")
        return s if s else "0"

    def _apply_playhead_to_text_from(self) -> None:
        if not self.video_info:
            messagebox.showwarning(
                "Zeit einfügen",
                "Bitte zuerst ein Video laden und die gewünschte Stelle in der Vorschau "
                "(Schieberegler oder Wiedergabe) anfahren.",
                parent=self,
            )
            return
        self.var_text_from.set(self._playhead_seconds_for_timing_field())
        self._schedule_timing_preview()

    def _apply_playhead_to_text_to(self) -> None:
        if not self.video_info:
            messagebox.showwarning(
                "Zeit einfügen",
                "Bitte zuerst ein Video laden und die gewünschte Stelle in der Vorschau "
                "(Schieberegler oder Wiedergabe) anfahren.",
                parent=self,
            )
            return
        self.var_text_to.set(self._playhead_seconds_for_timing_field())
        self._schedule_timing_preview()

    def _pick_palette_color(self, hex6: str) -> None:
        self.var_color.set(hex6.upper())
        self.refresh_preview()

    # --- Info dialogs ---

    def _show_info(self, title: str, body: str) -> None:
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        win.geometry(self._child_geometry(win, 520, 360))
        txt = tk.Text(win, wrap="word", height=16, width=60)
        txt.pack(fill="both", expand=True, padx=10, pady=10)
        txt.insert("1.0", body)
        txt.configure(state="disabled")
        ttk.Button(win, text="Schließen", command=win.destroy).pack(pady=(0, 10))

    def _child_geometry(self, child: tk.Toplevel, w: int, h: int) -> str:
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        mx = self.winfo_rootx()
        my = self.winfo_rooty()
        mw = self.winfo_width()
        mh = self.winfo_height()
        x = mx + mw + 12
        y = my
        if x + w > sw - 20:
            x = max(20, mx - w - 12)
        if y + h > sh - 40:
            y = max(40, sh - h - 40)
        return f"{w}x{h}+{x}+{y}"

    def _center_dialog_over_self(self, _child: tk.Toplevel, w: int, h: int) -> str:
        """Dialog mittig über dem Hauptfenster (nicht am rechten Rand)."""
        self.update_idletasks()
        sw = max(self.winfo_screenwidth(), w + 16)
        sh = max(self.winfo_screenheight(), h + 16)
        px = int(self.winfo_rootx())
        py = int(self.winfo_rooty())
        pw = max(int(self.winfo_width()), 1)
        ph = max(int(self.winfo_height()), 1)
        x = px + (pw - w) // 2
        y = py + (ph - h) // 2
        x = max(8, min(x, sw - w - 8))
        y = max(8, min(y, sh - h - 8))
        return f"{w}x{h}+{x}+{y}"

    def _info_text(self) -> None:
        self._show_info(
            "Text-Abschnitte & Einblendung",
            "Sie können **mehrere Text-Abschnitte** speichern, jeder mit eigenem "
            "Text, Zeitfenster (von/bis) und Darstellung (Position, Schrift, Farbe, "
            "Rahmen, Zeilenabstand).\n\n"
            "**Ablauf:** Unten Text und „von/bis“ einstellen, Optional mit „Einfügen“ "
            "die aktuelle Vorschau-Zeit setzen. Mit **„Übernehmen“** wird ein Eintrag "
            "in die Liste geschrieben bzw. der ausgewählte Eintrag aktualisiert. "
            "**„Neuer Abschnitt“** leert Text und Zeitfelder; **Schrift, Größe, Farbe, Stil, "
            "Position und Rahmen** werden vom **zuletzt markierten** Eintrag übernommen — "
            "gibt es keine Auswahl, vom **letzten Eintrag** der Liste. Ist die Liste leer, "
            "werden Schriftpfade und Stile wie beim ersten Start zurückgesetzt. Ein "
            "Listeneintrag lädt ihn wieder zum Bearbeiten.\n\n"
            "**Zeilenumbruch:** Enter erzeugt eine neue Zeile. Das Feld wrappt nicht — "
            "lange Zeilen horizontal scrollen. Im Video wird jede Zeile untereinander "
            "gezeichnet (robuster als eine gemeinsame Textdatei).\n\n"
            "**Schriftdatei:** **„Aus Windows …“** öffnet eine Liste installierter Schriften "
            "**mittig über dem Fenster**; die **Vorschau unten** zeigt die **gerade markierte** "
            "Schrift (Tk kann einzelne Schnitte nicht exakt wie FFmpeg darstellen — der Export "
            "nutzt immer die echte Schriftdatei). Alternativ **„Datei …“** oder "
            "den Pfad eintragen — beliebige **.ttf / .otf / .ttc**. Unter Windows liegen die "
            "Dateien meist unter **`%WINDIR%\\Fonts`** (oft `C:\\Windows\\Fonts`) oder bei "
            "nutzerbezogenen Fonts unter **`%LOCALAPPDATA%\\Microsoft\\Windows\\Fonts`** — "
            "im Explorer erscheint der Ort oft nur als Bereich „Schriftarten“, nicht als "
            "normaler Ordner. Leer lassen = FFmpeg nutzt eine eingebaute Standardschrift.\n\n"
            "**Texthintergrund:** Der halbtransparente Kasten kann pro Abschnitt mit "
            "**„Abgeschatteter Kasten“** ausgeschaltet werden — dann liegt nur die Schrift "
            "auf dem Video (Lesbarkeit ggf. durch Farbe/Kontrast prüfen).\n\n"
            "**Kursiv-Schrift (optional):** Nur für die **Pillow-Rasterung** bei aktivem "
            "Kursiv. Dort können Sie eine **eigene Italic-TTF** angeben (z. B. "
            "„MeineSchrift-Italic.ttf“) — unabhängig von ASS-Limits. Leer = automatische "
            "Suche nach einer passenden Italic-Datei bzw. geometrische Scherung.\n\n"
            "**Fett / Kursiv / Durchgestrichen:** FFmpeg-drawtext kann das nicht; aktivieren Sie "
            "einen dieser Stile, wird der Abschnitt **vorher als Bild gerastert** (Bibliothek "
            "**Pillow**) und mit FFmpeg als PNG überlagert — gleiches Raster wie im Export. "
            "**Vorschau:** Kurz vor der PNG-Ausgabe wird wie beim Encoder **YUV 4:2:0** "
            "simuliert (Farben/Kanten wie MP4), nicht wie ein reines RGB-Bildschirmfoto.\n\n"
            "**Kursiv:** Zuerst gilt die **optional gewählte Kursiv-Schrift**, sonst wird eine "
            "**Italic/Oblique-TTF** gesucht (gleicher Ordner wie die gewählte Schrift bzw. "
            "übliche Windows-Paare wie Arial→ariali.ttf). "
            "Ohne passende Datei bleibt nur eine **horizontale Scherung** als Ersatz — echte "
            "Kursivformen lesen sich am besten mit einer italischen Schriftdatei.\n\n"
            "**Zeilenabstand:** Pixel-Abstand zwischen den Untereinander-Zeilen "
            "(Schritt ≈ Schriftgröße + Wert); negative Werte rücken näher zusammen.\n\n"
            "**Vorschau:** Zeigt alle gespeicherten Abschnitte; wenn Sie einen Eintrag "
            "markieren, werden Ihre aktuellen Felder für diesen Listenplatz mit "
            "einberechnet (auch vor „Übernehmen“). Ist nichts markiert und das "
            "Textfeld enthält Text, zählt das wie ein zusätzlicher Entwurf. "
            "**Mausrad** zoomt zur Mausposition ins Bild, mit der **rechten Maustaste** "
            "können Sie schwenken; **Zoom 100%** setzt die Ansicht zurück.\n\n"
            "**Zeitfenster:** Leer = ganze Clip-Länge; nur „von“ = ab dieser Sekunde; "
            "nur „bis“ = bis dorthin; beide = exakter Bereich. Mit **Einfügen** wird "
            "die Schieberegler-/Wiedergabezeit eingetragen.\n\n"
            "**Position:** Klick/Ziehen in der Vorschau setzt X/Y im Originalvideo.",
        )

    def _info_subs(self) -> None:
        self._show_info(
            "Untertitel",
            "Unterstützt typischerweise SRT-Dateien im UTF-8-Format.\n\n"
            "Der Untertitel liegt unter allen eingebrannten Text-Abschnitten (die "
            "Abschnitte werden darüber gezeichnet).\n\n"
            "Pfade mit Sonderzeichen sollten möglichst normale Buchstaben ohne "
            "komische Anführungszeichen verwenden.",
        )

    def _info_ffmpeg(self) -> None:
        self._show_info(
            "FFmpeg-Export",
            "Benötigt ffmpeg und ffprobe im PATH.\n\n"
            "**Ausgabe MP4:** Codec wählt den Videoencoder (-c:v). VP9 und AV1 sind langsamer, "
            "oft aber effizienter. Bitrate z. B. 8M oder 2500k.\n\n"
            "**Ausgabe GIF:** Animiertes GIF ohne Ton (Audio wird verworfen). Es wird eine "
            "Palette aus dem fertigen Bild berechnet (gute Farben bei wenigen KB). "
            "FPS begrenzt die Bildrate (kleiner = kleinere Datei). „max. Breite“ skaliert "
            "hinunter — schmale GIFs sind oft ausreichend fürs Web. Mehr Palettenfarben "
            "= feinere Farbabstufungen, größere Datei.\n\n"
            "Beim **Laden einer GIF-Datei** werden „GIF FPS“ und „GIF max. Breite“ aus "
            "der Quelle übernommen (Breite begrenzt wie im Spinbox-Bereich).\n\n"
            "**Bitrate:** Nach dem Laden eines Clips wird die von ffprobe gemeldete "
            "Quellbitrate (Format oder Videostream) als Vorschlag ins Feld gesetzt; mit "
            "**Quelle** können Sie sie später erneut einsetzen.\n\n"
            "Reine Zahlen unter 100000 bei der Bitrate werden wie im Video-Bitrate-Tool als "
            "kbit/s interpretiert.\n\n"
            "Audio kopieren: Nur bei MP4 sinnvoll — klappt nur, wenn Container und Codecs "
            "zum gewählten Videoformat passen.",
        )

    def _info_resolve(self) -> None:
        self._show_info(
            "DaVinci Resolve",
            "Voraussetzungen: DaVinci Resolve **Studio** (Skripting), "
            "„Externes Skripting“ = Lokal in den Resolve-Einstellungen, "
            "danach Resolve neu starten.\n\n"
            "Ablauf: Das gewählte Video wird in ein neues Timeline-Segment "
            "gelegt und mit dem angegebenen **Render-Preset** ausgeliefert.\n\n"
            "Der eingebrannte Text aus diesem Tool ist hier **nicht** automatisch "
            "als editierbarer Titel in Resolve enthalten — nutzen Sie dafür "
            "zuerst den FFmpeg-Export oder setzen Sie Text in Resolve manuell.\n\n"
            "Alternativ: „Letztes FFmpeg-Ergebnis“ nutzt die zuletzt erzeugte Datei.",
        )

    # --- Media ---

    def _pick_video(self) -> None:
        initial = self.settings.get("last_video_dir") or str(Path.home())
        path = filedialog.askopenfilename(
            parent=self,
            title="Video auswählen",
            initialdir=initial,
            filetypes=[
                (
                    "Video / GIF",
                    "*.mp4 *.mov *.mkv *.webm *.avi *.mxf *.gif",
                ),
                ("Alle Dateien", "*.*"),
            ],
        )
        if not path:
            return
        self.settings["last_video_dir"] = str(Path(path).parent)
        self._save_settings()
        self._load_video(path)

    def _pick_srt(self) -> None:
        initial = self.settings.get("last_video_dir") or str(Path.home())
        path = filedialog.askopenfilename(
            parent=self,
            title="SRT auswählen",
            initialdir=initial,
            filetypes=[("Untertitel", "*.srt"), ("Alle Dateien", "*.*")],
        )
        if path:
            self.var_srt_path.set(path)
            self.lbl_srt.configure(text=Path(path).name)
            self.refresh_preview()

    def _pick_overlay_font(self) -> None:
        fp = self.var_font_path.get().strip()
        initial = (self.settings.get("last_overlay_font_dir") or "").strip()
        if fp and Path(fp).is_file():
            initial = str(Path(fp).parent)
        elif not initial:
            initial = (self.settings.get("last_video_dir") or "").strip() or preferred_font_dialog_initialdir()
        if not Path(initial).is_dir():
            initial = preferred_font_dialog_initialdir()
        if not Path(initial).is_dir():
            initial = str(Path.home())
        path = filedialog.askopenfilename(
            parent=self,
            title="Schriftdatei auswählen",
            initialdir=initial,
            filetypes=[
                ("Schriftarten", "*.ttf *.otf *.ttc"),
                ("Alle Dateien", "*.*"),
            ],
        )
        if path:
            self.settings["last_overlay_font_dir"] = str(Path(path).parent)
            self._save_settings()
            self.var_font_path.set(path)
            self._schedule_font_path_preview()

    def _pick_italic_overlay_font(self) -> None:
        fp = self.var_italic_font_path.get().strip()
        initial = (self.settings.get("last_italic_font_dir") or "").strip()
        if fp and Path(fp).is_file():
            initial = str(Path(fp).parent)
        elif not initial:
            initial = (
                (self.settings.get("last_overlay_font_dir") or "").strip()
                or self.settings.get("last_video_dir")
                or preferred_font_dialog_initialdir()
            )
        if not Path(initial).is_dir():
            initial = preferred_font_dialog_initialdir()
        if not Path(initial).is_dir():
            initial = str(Path.home())
        path = filedialog.askopenfilename(
            parent=self,
            title="Kursiv-Schrift (.ttf/.otf)",
            initialdir=initial,
            filetypes=[
                ("Schriftarten", "*.ttf *.otf *.ttc"),
                ("Alle Dateien", "*.*"),
            ],
        )
        if path:
            self.settings["last_italic_font_dir"] = str(Path(path).parent)
            self._save_settings()
            self.var_italic_font_path.set(path)
            self._schedule_font_path_preview()

    def _pick_overlay_font_windows(self) -> None:
        self._pick_font_from_windows_installiert(self.var_font_path, "Hauptschrift")

    def _pick_italic_font_windows(self) -> None:
        self._pick_font_from_windows_installiert(self.var_italic_font_path, "Kursiv-Schrift")

    def _pick_font_from_windows_installiert(
        self, var: tk.StringVar, beschreibung: str
    ) -> None:
        pairs = list_windows_font_choices(refresh=True)
        if not pairs:
            messagebox.showinfo(
                "Schriftarten",
                "Es wurden keine Schriftdateien gefunden. Unter Windows liegen sie "
                "typischerweise unter %WINDIR%\\Fonts oder unter "
                "%LOCALAPPDATA%\\Microsoft\\Windows\\Fonts — oder nutzen Sie „Datei …“.",
                parent=self,
            )
            return
        dlg = _WindowsFontPickerDialog(
            self,
            pairs,
            title=f"{beschreibung} — installierte Schriften",
        )
        dlg.update_idletasks()
        dlg.geometry(self._center_dialog_over_self(dlg, 580, 520))
        path = dlg.chosen_path()
        if not path:
            return
        var.set(path)
        parent_dir = str(Path(path).parent)
        if var is self.var_italic_font_path:
            self.settings["last_italic_font_dir"] = parent_dir
        else:
            self.settings["last_overlay_font_dir"] = parent_dir
        self._save_settings()
        self._schedule_font_path_preview()

    def _schedule_font_path_preview(self, *_args: object) -> None:
        if getattr(self, "_font_path_job", None):
            self.after_cancel(self._font_path_job)
        self._font_path_job = self.after(450, self._font_path_refresh_run)

    def _font_path_refresh_run(self) -> None:
        self._font_path_job = None
        if self._segment_loading:
            return
        self.refresh_preview()

    def _apply_source_bitrate(self) -> None:
        if not self.video_info:
            messagebox.showinfo(
                "Quellbitrate",
                "Bitte zuerst eine Videodatei laden.",
                parent=self,
            )
            return
        sug = suggest_ffmpeg_bitrate_from_bps(self.video_info.source_bitrate_bps)
        if not sug:
            messagebox.showinfo(
                "Quellbitrate",
                "ffprobe hat für diese Datei keine Bitrate geliefert (z. B. GIF ohne "
                "Bitrate oder manche Rohclips).",
                parent=self,
            )
            return
        self.var_bitrate.set(sug)

    def _load_video(self, path: str) -> None:
        try:
            find_ffmpeg()
            self.video_info = probe_video(path)
        except (FFmpegNotFoundError, ProbeError) as err:
            messagebox.showerror("Video", str(err), parent=self)
            return
        self.video_path = path
        self.lbl_video.configure(text=Path(path).name)
        self.current_time = 0.0
        self.scale_time.set(0.0)
        self.slider.configure(to=max(0.01, self.video_info.duration_sec))
        sug_br = suggest_ffmpeg_bitrate_from_bps(self.video_info.source_bitrate_bps)
        if sug_br:
            self.var_bitrate.set(sug_br)

        low = path.lower()
        if low.endswith(".gif"):
            w_src = self.video_info.width
            self.var_gif_max_w.set(max(160, min(w_src, 1920)))
            gfps = max(1, min(int(round(self.video_info.fps)), 60))
            self.var_gif_fps.set(gfps)

        self._reset_position_defaults()
        self.refresh_preview()
        parts = [
            f"{self.video_info.width}×{self.video_info.height}",
            f"{self.video_info.fps:.3f} fps",
            f"{self.video_info.duration_sec:.2f} s",
        ]
        if sug_br:
            parts.append(f"Vorschlag {sug_br}")
        self.status.configure(text=", ".join(parts))

    def _reset_position_defaults(self) -> None:
        if not self.video_info:
            return
        self.var_px.set(max(40, int(self.video_info.width * 0.05)))
        self.var_py.set(max(40, int(self.video_info.height * 0.85 - self.var_fontsize.get())))

    def _on_scrub(self, _evt=None) -> None:
        self.current_time = float(self.scale_time.get())
        self._update_time_label()
        if self._scrub_preview_job:
            self.after_cancel(self._scrub_preview_job)
        self._scrub_preview_job = self.after(90, self._scrub_preview_run)

    def _scrub_preview_run(self) -> None:
        self._scrub_preview_job = None
        self.refresh_preview()

    def _update_time_label(self) -> None:
        if not self.video_info:
            self.lbl_time.configure(text="0:00 / 0:00")
            return
        cur = self.current_time
        tot = self.video_info.duration_sec
        self.lbl_time.configure(text=f"{self._fmt_time(cur)} / {self._fmt_time(tot)}")

    @staticmethod
    def _fmt_time(sec: float) -> str:
        sec = max(0.0, sec)
        m = int(sec // 60)
        s = sec - m * 60
        return f"{m:d}:{s:05.2f}"

    def _toggle_play(self) -> None:
        if not self.video_info:
            return
        self.playing = not self.playing
        if self.playing:
            self._play_tick()
        elif self._play_after_id:
            self.after_cancel(self._play_after_id)
            self._play_after_id = None
            self.refresh_preview()

    def _play_tick(self) -> None:
        if not self.playing or not self.video_info:
            return
        fps = max(self.video_info.fps, 1.0)
        dt = 1.0 / fps
        self.current_time = min(self.video_info.duration_sec, self.current_time + dt)
        self.scale_time.set(self.current_time)
        self._update_time_label()
        now = time.monotonic()
        if now - self._last_play_preview_at >= 0.35:
            self._last_play_preview_at = now
            self.refresh_preview()
        if self.current_time >= self.video_info.duration_sec - 1e-3:
            self.playing = False
            return
        self._play_after_id = self.after(int(1000 / min(fps, 60)), self._play_tick)

    def _canvas_to_video(self, cx: float, cy: float) -> tuple[int, int]:
        dw, dh, pad_x, pad_y = self._last_disp
        if dw <= 0 or dh <= 0 or not self.video_info:
            return self.var_px.get(), self.var_py.get()
        vx = int((cx - pad_x) / dw * self.video_info.width)
        vy = int((cy - pad_y) / dh * self.video_info.height)
        vx = max(0, min(vx, self.video_info.width - 1))
        vy = max(0, min(vy, self.video_info.height - 1))
        return vx, vy

    def _on_canvas_click(self, e: tk.Event) -> None:
        vx, vy = self._canvas_to_video(e.x, e.y)
        self.var_px.set(vx)
        self.var_py.set(vy)
        self.refresh_preview()

    def _on_canvas_drag(self, e: tk.Event) -> None:
        self._on_canvas_click(e)

    def _preview_redraw_canvas(self) -> None:
        src = self._preview_src_im
        if src is None:
            return
        try:
            self.update_idletasks()
        except tk.TclError:
            return
        cw = max(int(self.canvas.winfo_width()), 1)
        ch = max(int(self.canvas.winfo_height()), 1)
        iw, ih = src.size
        base_scale = min(cw / iw, ch / ih, 1.0)
        z = max(0.25, min(float(self._pz_zoom), 8.0))
        self._pz_zoom = z
        scale_total = base_scale * z
        nw = max(1, int(round(iw * scale_total)))
        nh = max(1, int(round(ih * scale_total)))
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = Image.LANCZOS  # type: ignore[attr-defined]
        rim = src.resize((nw, nh), resample=resample)
        self._photo_ref = ImageTk.PhotoImage(rim)
        self.canvas.delete("all")
        ox = self._pz_ox
        oy = self._pz_oy
        if ox is None or oy is None:
            ox = int((cw - nw) // 2)
            oy = int((ch - nh) // 2)
            self._pz_ox, self._pz_oy = ox, oy
        self.canvas.create_image(ox, oy, anchor="nw", image=self._photo_ref)
        self._last_disp = (nw, nh, ox, oy)

    def _preview_wheel_common(self, mx: int, my: int, zoom_in: bool) -> None:
        src = self._preview_src_im
        if src is None:
            return
        iw, ih = src.size
        dw, dh, ox_old, oy_old = self._last_disp
        if dw <= 0 or dh <= 0:
            return
        st_old = dw / iw
        ix = (mx - ox_old) / st_old
        iy = (my - oy_old) / st_old
        ix = max(0.0, min(float(iw), ix))
        iy = max(0.0, min(float(ih), iy))
        factor = 1.12 if zoom_in else 1 / 1.12
        old_z = self._pz_zoom
        self._pz_zoom = max(0.25, min(old_z * factor, 8.0))
        if abs(self._pz_zoom - old_z) < 1e-9:
            return
        cw = max(int(self.canvas.winfo_width()), 1)
        ch = max(int(self.canvas.winfo_height()), 1)
        base_scale = min(cw / iw, ch / ih, 1.0)
        st_new = base_scale * self._pz_zoom
        self._pz_ox = int(mx - ix * st_new)
        self._pz_oy = int(my - iy * st_new)
        self._preview_redraw_canvas()

    def _on_preview_wheel(self, e: tk.Event) -> None:
        d = getattr(e, "delta", 0)
        if d == 0:
            return
        self._preview_wheel_common(e.x, e.y, d > 0)

    def _preview_zoom_step(self, zoom_in: bool) -> None:
        try:
            cw = max(int(self.canvas.winfo_width()), 1)
            ch = max(int(self.canvas.winfo_height()), 1)
        except tk.TclError:
            return
        self._preview_wheel_common(cw // 2, ch // 2, zoom_in)

    def _preview_zoom_reset(self) -> None:
        self._pz_zoom = 1.0
        self._pz_ox = None
        self._pz_oy = None
        self._preview_redraw_canvas()

    def _on_preview_pan_press(self, e: tk.Event) -> None:
        if self._preview_src_im is None:
            return
        self._pan_r_mark = (e.x, e.y, self._pz_ox, self._pz_oy)

    def _on_preview_pan_move(self, e: tk.Event) -> None:
        if self._pan_r_mark is None:
            return
        x0, y0, ox0, oy0 = self._pan_r_mark
        if ox0 is None or oy0 is None:
            return
        self._pz_ox = int(ox0 + (e.x - x0))
        self._pz_oy = int(oy0 + (e.y - y0))
        self._preview_redraw_canvas()

    def _on_preview_pan_release(self, _e: tk.Event) -> None:
        self._pan_r_mark = None

    def _on_preview_canvas_configure(self, _evt: tk.Event | None = None) -> None:
        if self._preview_src_im is None:
            return
        if self._preview_configure_job:
            try:
                self.after_cancel(self._preview_configure_job)
            except tk.TclError:
                pass
        self._preview_configure_job = self.after(100, self._preview_configure_run)

    def _preview_configure_run(self) -> None:
        self._preview_configure_job = None
        if self._preview_src_im is not None:
            self._preview_redraw_canvas()

    def refresh_preview(self, _evt=None) -> None:
        if not self.video_path or not self.video_info:
            return
        self._preview_gen += 1
        gen = self._preview_gen

        overlays = self._overlays_for_engine()
        srt = self.var_srt_path.get().strip()
        srt_path = srt if srt and Path(srt).is_file() else ""

        vp = self.video_path
        ct = self.current_time
        vi = self.video_info

        threading.Thread(
            target=self._preview_worker,
            args=(gen, vp, ct, overlays, srt_path, vi),
            daemon=True,
        ).start()

    def _preview_worker(
        self,
        gen: int,
        vp: str,
        ct: float,
        overlays: list[TimedTextOverlay],
        srt_path: str,
        vi,
    ) -> None:
        try:
            if overlays or (srt_path and Path(srt_path).is_file()):
                data = extract_preview_frame_with_overlay(
                    vp,
                    ct,
                    overlays=overlays,
                    srt_path=srt_path if srt_path else None,
                    cached_info=vi,
                    width_max=960,
                    height_max=720,
                )
            else:
                data = extract_frame_png_bytes(
                    vp,
                    ct,
                    cached_info=vi,
                    width_max=960,
                    height_max=720,
                )
        except Exception as err:
            self.after(0, lambda g=gen, msg=str(err): self._preview_apply_error(g, msg))
            return

        self.after(0, lambda g=gen, d=data: self._preview_apply_ok(g, d))

    def _preview_apply_error(self, gen: int, msg: str) -> None:
        if gen != self._preview_gen:
            return
        self.status.configure(text=msg)

    def _preview_apply_ok(self, gen: int, data: bytes) -> None:
        if gen != self._preview_gen:
            return
        try:
            im = Image.open(io.BytesIO(data)).convert("RGBA")
        except OSError:
            return

        if self._preview_src_size != im.size:
            self._pz_zoom = 1.0
            self._pz_ox = None
            self._pz_oy = None
        self._preview_src_size = im.size
        self._preview_src_im = im
        self._preview_redraw_canvas()
        self._update_time_label()
        self.status.configure(text="Bereit.")

    def _sync_export_format_widgets(self) -> None:
        try:
            is_gif = self.var_export_fmt.get() == EXPORT_FMT_GIF
        except (tk.TclError, AttributeError):
            return
        try:
            self.cmb_codec.configure(state=("disabled" if is_gif else "readonly"))
            self.ent_bitrate.configure(state=("disabled" if is_gif else "normal"))
            self.chk_audio_copy.configure(state=("disabled" if is_gif else "normal"))
            st_gif = "normal" if is_gif else "disabled"
            self.spin_gif_fps.configure(state=st_gif)
            self.spin_gif_max_w.configure(state=st_gif)
            self.spin_gif_colors.configure(state=st_gif)
        except (tk.TclError, AttributeError):
            pass

    # --- Export FFmpeg ---

    def _export_ffmpeg(self) -> None:
        if not self.video_path:
            messagebox.showwarning("Export", "Bitte zuerst ein Video laden.", parent=self)
            return
        is_gif = self.var_export_fmt.get() == EXPORT_FMT_GIF
        dst = filedialog.asksaveasfilename(
            parent=self,
            title="Ausgabedatei",
            defaultextension=".gif" if is_gif else ".mp4",
            filetypes=[
                ("GIF animiert", "*.gif"),
                ("MP4 Video", "*.mp4"),
                ("Alle Dateien", "*.*"),
            ],
        )
        if not dst:
            return
        if is_gif:
            dst = str(Path(dst).with_suffix(".gif"))
        overlays = self._overlays_for_engine()
        srt = self.var_srt_path.get().strip()
        if not overlays and not (srt and Path(srt).is_file()):
            messagebox.showwarning(
                "Export",
                "Bitte mindestens einen Text-Abschnitt mit „Übernehmen“ speichern oder eine "
                "SRT-Datei wählen. Ohne Listeneintrag genügt auch ein Entwurf mit Text "
                "(wird wie ein zusätzlicher Abschnitt mitgewertet).",
                parent=self,
            )
            return
        try:
            gfps = max(1, min(int(self.var_gif_fps.get()), 60))
            gmw = max(160, min(int(self.var_gif_max_w.get()), 1920))
            gncol = max(8, min(int(self.var_gif_colors.get()), 256))
        except (tk.TclError, ValueError):
            gfps, gmw, gncol = 15, 720, 128
        try:
            enc = map_codec_to_lib(self.var_codec.get())
            br = parse_bitrate(self.var_bitrate.get())
            vw = int(self.video_info.width) if self.video_info else 1920
            vh = int(self.video_info.height) if self.video_info else 1080
            cmd, ff_cwd = build_export_command(
                src=self.video_path,
                dst=dst,
                encoder=enc,
                bitrate=br,
                overlays=overlays,
                srt_path=srt if srt and Path(srt).is_file() else None,
                audio_copy=self.var_audio_copy.get(),
                progress_file=None,
                export_as_gif=is_gif,
                gif_fps=gfps,
                gif_max_width=gmw,
                gif_palette_colors=gncol,
                video_width=vw,
                video_height=vh,
            )
        except FFmpegNotFoundError as err:
            messagebox.showerror("FFmpeg", str(err), parent=self)
            return

        self.settings["codec"] = self.var_codec.get()
        self.settings["bitrate"] = self.var_bitrate.get()
        self._save_settings()

        self.prog["value"] = 0
        self.lbl_ff_status.configure(text="Encodierung läuft …")
        threading.Thread(
            target=self._run_ffmpeg_thread,
            args=(
                cmd,
                dst,
                self.video_info.duration_sec if self.video_info else 1.0,
                ff_cwd,
            ),
            daemon=True,
        ).start()

    def _run_ffmpeg_thread(
        self,
        cmd: list[str],
        dst: str,
        duration: float,
        cwd: str | None = None,
    ) -> None:
        import subprocess

        try:
            proc = subprocess.Popen(
                cmd,
                stderr=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                cwd=cwd,
            )
            assert proc.stderr is not None
            for line in proc.stderr:
                t = parse_time_seconds(line)
                if t is not None and duration > 0:
                    pct = min(100.0, max(0.0, 100.0 * t / duration))
                    self.export_queue.put(("prog", pct))
            code = proc.wait()
            if code == 0:
                self.export_queue.put(("done", dst))
            else:
                self.export_queue.put(("err", f"ffmpeg beendete sich mit Code {code}."))
        except Exception as exc:  # pragma: no cover
            self.export_queue.put(("err", str(exc)))
        finally:
            if cwd:
                shutil.rmtree(cwd, ignore_errors=True)

    def _poll_export_queue(self) -> None:
        try:
            while True:
                kind, payload = self.export_queue.get_nowait()
                if kind == "prog":
                    self.prog["value"] = float(payload)
                    self.lbl_ff_status.configure(text=f"{payload:.1f} %")
                elif kind == "done":
                    self.last_ffmpeg_output = payload
                    self.prog["value"] = 100
                    self.lbl_ff_status.configure(text=f"Fertig: {payload}")
                    self.status.configure(text="FFmpeg-Export abgeschlossen.")
                elif kind == "err":
                    self.lbl_ff_status.configure(text="Fehler.")
                    messagebox.showerror("FFmpeg", str(payload), parent=self)
                elif kind == "status":
                    self.status.configure(text=str(payload))
                elif kind == "resolve_done":
                    self.status.configure(
                        text=f"DaVinci-Render abgeschlossen. Ordner: {payload}"
                    )
                    messagebox.showinfo(
                        "DaVinci Resolve",
                        f"Render abgeschlossen.\nAusgabeordner:\n{payload}",
                        parent=self,
                    )
                elif kind == "resolve_err":
                    messagebox.showerror("DaVinci Resolve", str(payload), parent=self)
                    self.status.configure(text="DaVinci: Fehler — Details im Dialog.")
        except queue.Empty:
            pass
        self.after(120, self._poll_export_queue)

    def _schedule_timing_preview(self, *_args: object) -> None:
        if self._timing_preview_job:
            self.after_cancel(self._timing_preview_job)
        self._timing_preview_job = self.after(550, self._timing_preview_run)

    def _timing_preview_run(self) -> None:
        self._timing_preview_job = None
        self.refresh_preview()

    def _on_text_key(self, _evt=None) -> None:
        if getattr(self, "_text_refresh_job", None):
            self.after_cancel(self._text_refresh_job)
        self._text_refresh_job = self.after(950, self._text_refresh_run)

    def _text_refresh_run(self) -> None:
        self._text_refresh_job = None
        if self._segment_loading:
            return
        self.refresh_preview()

    # --- DaVinci ---

    def _export_resolve(self) -> None:
        src = self.last_ffmpeg_output if self.var_dv_use_last.get() else self.video_path
        if not src or not Path(src).is_file():
            messagebox.showwarning(
                "DaVinci",
                "Keine gültige Videodatei. Laden Sie ein Video oder exportieren Sie "
                "zuerst mit FFmpeg und aktivieren Sie „Letztes FFmpeg-Ergebnis“.",
                parent=self,
            )
            return
        raw_out = (self.settings.get("davinci_output_dir") or "").strip()
        out_dir = raw_out if raw_out else str(Path(src).parent)
        preset = self.var_dv_preset.get().strip()
        if not preset:
            messagebox.showwarning("DaVinci", "Bitte einen Render-Preset-Namen angeben.", parent=self)
            return
        self.settings["davinci_preset"] = preset
        self._save_settings()

        self.status.configure(text="DaVinci: Verbindung wird aufgebaut …")
        threading.Thread(target=self._resolve_worker, args=(src, out_dir, preset), daemon=True).start()

    def _resolve_worker(self, video_path: str, output_dir: str, preset: str) -> None:
        try:
            from davinci_api import (
                ResolveError,
                apply_project_timeline_settings,
                cleanup_timelines,
                connect_resolve,
                register_custom_resolve_paths,
                render_with_preset,
                scripting_thread,
                to_forward,
            )

            mods = (self.settings.get("resolve_modules") or "").strip()
            dll = (self.settings.get("resolve_dll") or "").strip()
            exe = (self.settings.get("resolve_exe") or "").strip()
            if mods or dll or exe:
                register_custom_resolve_paths(
                    modules_dir=mods or None,
                    fusionscript_dll=dll or None,
                    resolve_exe=exe or None,
                )

            def notify(msg: str) -> None:
                self.export_queue.put(("status", msg))

            with scripting_thread():
                _resolve, project, media_pool, _root = connect_resolve(
                    status_callback=lambda m: notify(m),
                    auto_launch=True,
                    create_scratch_project_name="SubTool",
                )
                clips = media_pool.ImportMedia([to_forward(video_path)])
                if not clips:
                    raise ResolveError("ImportMedia lieferte keine Clips.")
                clip = clips[0]
                import time as _time

                _time.sleep(0.35)
                Path(output_dir).mkdir(parents=True, exist_ok=True)
                cleanup_timelines(project, media_pool, name_prefix="SubTool_")
                fps_s = clip.GetClipProperty("FPS") or "25"
                res_s = clip.GetClipProperty("Resolution") or "1920x1080"
                apply_project_timeline_settings(project, fps_s, res_s)
                import time as _t

                tl_name = f"SubTool_{int(_t.time())}"
                timeline = media_pool.CreateEmptyTimeline(tl_name)
                if not timeline:
                    raise ResolveError("Timeline konnte nicht erstellt werden.")
                project.SetCurrentTimeline(timeline)
                media_pool.AppendToTimeline([{"mediaPoolItem": clip}])
                render_with_preset(
                    project,
                    output_dir=output_dir,
                    output_name=Path(video_path).stem + "_deliver",
                    preset_name=preset,
                    status_callback=lambda m: notify(m),
                )
            self.export_queue.put(("resolve_done", output_dir))
        except Exception as exc:
            self.export_queue.put(("resolve_err", str(exc)))

    # --- Settings window ---

    def _open_settings(self) -> None:
        win = tk.Toplevel(self)
        win.title("Einstellungen")
        win.transient(self)
        win.geometry(self._child_geometry(win, 560, 320))

        frm = ttk.Frame(win, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="DaVinci: Module-Ordner (…\\Scripting\\Modules)").grid(
            row=0, column=0, sticky="w"
        )
        v_mod = tk.StringVar(value=self.settings.get("resolve_modules", ""))
        ttk.Entry(frm, textvariable=v_mod, width=58).grid(row=1, column=0, sticky="ew")
        ttk.Label(frm, text="DaVinci: fusionscript.dll (optional)").grid(
            row=2, column=0, sticky="w", pady=(8, 0)
        )
        v_dll = tk.StringVar(value=self.settings.get("resolve_dll", ""))
        ttk.Entry(frm, textvariable=v_dll, width=58).grid(row=3, column=0, sticky="ew")
        ttk.Label(frm, text="DaVinci: Resolve.exe (optional)").grid(
            row=4, column=0, sticky="w", pady=(8, 0)
        )
        v_exe = tk.StringVar(value=self.settings.get("resolve_exe", ""))
        ttk.Entry(frm, textvariable=v_exe, width=58).grid(row=5, column=0, sticky="ew")

        ttk.Label(frm, text="Ausgabeordner für Resolve-Render (Standard: Ordner der Quelle)").grid(
            row=6, column=0, sticky="w", pady=(8, 0)
        )
        v_out = tk.StringVar(value=self.settings.get("davinci_output_dir", ""))
        ttk.Entry(frm, textvariable=v_out, width=58).grid(row=7, column=0, sticky="ew")

        def save_and_close() -> None:
            self.settings["resolve_modules"] = v_mod.get().strip()
            self.settings["resolve_dll"] = v_dll.get().strip()
            self.settings["resolve_exe"] = v_exe.get().strip()
            self.settings["davinci_output_dir"] = v_out.get().strip()
            self._save_settings()
            win.destroy()

        bf = ttk.Frame(frm)
        bf.grid(row=8, column=0, pady=(14, 0), sticky="e")
        ttk.Button(bf, text="Speichern", command=save_and_close).pack(side="right")
        ttk.Button(bf, text="Abbrechen", command=win.destroy).pack(side="right", padx=(0, 8))
        frm.columnconfigure(0, weight=1)


def main() -> None:
    app = VideoTextApp()

    def _on_close() -> None:
        app._save_settings()
        app.destroy()

    app.protocol("WM_DELETE_WINDOW", _on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
