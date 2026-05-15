"""FFmpeg/ffprobe helpers for preview frames and text/subtitle burn-in."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


class FFmpegNotFoundError(RuntimeError):
    pass


class ProbeError(RuntimeError):
    pass


def _which_or_raise(names: Tuple[str, ...]) -> str:
    import shutil

    for n in names:
        p = shutil.which(n)
        if p:
            return p
    raise FFmpegNotFoundError(
        "ffmpeg/ffprobe wurde nicht im PATH gefunden. Bitte installieren und PATH setzen."
    )


def find_ffmpeg() -> str:
    return _which_or_raise(("ffmpeg",))


def find_ffprobe() -> str:
    return _which_or_raise(("ffprobe",))


def run_cmd(
    args: List[str],
    *,
    timeout: Optional[float] = None,
    cwd: Optional[str] = None,
) -> Tuple[int, str, str]:
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=cwd,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


@dataclass
class VideoInfo:
    path: str
    duration_sec: float
    width: int
    height: int
    fps: float
    #: Gesamt- oder Videostream-Bitrate laut ffprobe (bit/s), oft bei RAW/GIF leer
    source_bitrate_bps: Optional[int] = None


def _parse_positive_int(raw: object) -> Optional[int]:
    try:
        n = int(str(raw).strip())
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def probe_video(video_path: str) -> VideoInfo:
    ffprobe = find_ffprobe()
    code, out, err = run_cmd(
        [
            ffprobe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            video_path,
        ],
        timeout=120,
    )
    if code != 0:
        raise ProbeError(err.strip() or out.strip() or "ffprobe fehlgeschlagen.")

    data = json.loads(out)
    dur = float(data.get("format", {}).get("duration") or 0.0)
    w, h = 1920, 1080
    fps = 25.0
    fmt_br = _parse_positive_int(data.get("format", {}).get("bit_rate"))
    stream_br: Optional[int] = None
    for st in data.get("streams") or []:
        if st.get("codec_type") == "video":
            w = int(st.get("width") or w)
            h = int(st.get("height") or h)
            stream_br = _parse_positive_int(st.get("bit_rate"))
            fr = st.get("r_frame_rate") or st.get("avg_frame_rate") or ""
            if "/" in str(fr):
                a, b = str(fr).split("/", 1)
                try:
                    af, bf = float(a), float(b)
                    if bf:
                        fps = af / bf
                except ValueError:
                    pass
            break
    # Videostream-Bitrate bevorzugen (ohne Audio); sonst Format-Gesamtbitrate
    source_br = stream_br if stream_br is not None else fmt_br
    if dur <= 0:
        dur = 1.0
    return VideoInfo(
        path=video_path,
        duration_sec=dur,
        width=w,
        height=h,
        fps=max(fps, 1.0),
        source_bitrate_bps=source_br,
    )


def suggest_ffmpeg_bitrate_from_bps(bps: Optional[int]) -> Optional[str]:
    """Aus ffprobe-Bitrate einen üblichen FFmpeg-Zielwert wie ``8M`` / ``3500k`` bilden."""
    if bps is None or bps <= 0:
        return None
    if bps >= 950_000:
        mb = bps / 1e6
        if mb >= 10:
            return f"{int(round(mb))}M"
        s = f"{mb:.1f}".rstrip("0").rstrip(".")
        return f"{s}M"
    kb = max(1, int(round(bps / 1000)))
    return f"{kb}k"


def escape_filter_path(path: str) -> str:
    """Fallback für seltene Fälle mit vollem Pfad im Filter (möglichst vermeiden).

    Zuverlässiger auf Windows: *_vf_overlay_parts_and_cwd* legt Dateien ins
    Arbeitsverzeichnis und nutzt nur Basisnamen.
    """
    p = Path(path).resolve().as_posix()
    p = p.replace("\\", "/")
    p = p.replace(":", r"\:")
    p = p.replace("'", r"\'")
    p = p.replace(" ", r"\ ")
    return p


_FONT_SRC_RESOLVED: Optional[Path] = None


def _system_ttf_path() -> Path:
    """Erste vorhandene TTF für drawtext (Segoe/Arial/ENV)."""
    global _FONT_SRC_RESOLVED
    if _FONT_SRC_RESOLVED is not None and _FONT_SRC_RESOLVED.is_file():
        return _FONT_SRC_RESOLVED

    env = (os.environ.get("VIDEO_TEXT_FONTFILE") or "").strip()
    candidates: List[str] = []
    if env:
        candidates.append(env)
    if sys.platform == "win32":
        win = os.environ.get("WINDIR", r"C:\Windows")
        local = os.environ.get("LOCALAPPDATA", "").strip()
        candidates.extend(
            [
                os.path.join(win, "Fonts", "segoeui.ttf"),
                os.path.join(win, "Fonts", "arial.ttf"),
            ]
        )
        if local:
            candidates.extend(
                [
                    os.path.join(local, "Microsoft", "Windows", "Fonts", "segoeui.ttf"),
                    os.path.join(local, "Microsoft", "Windows", "Fonts", "arial.ttf"),
                ]
            )
    else:
        candidates.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/TTF/DejaVuSans.ttf",
            ]
        )
    for c in candidates:
        if c and os.path.isfile(c):
            _FONT_SRC_RESOLVED = Path(c).resolve()
            return _FONT_SRC_RESOLVED
    raise ProbeError(
        "Keine Schriftdatei (.ttf) gefunden. Unter Windows: %WINDIR%\\Fonts oder "
        "%LOCALAPPDATA%\\Microsoft\\Windows\\Fonts — oder Umgebungsvariable "
        "VIDEO_TEXT_FONTFILE auf eine .ttf setzen."
    )


def _ensure_draw_font_basename(work_dir: Path) -> str:
    """Kopiert die System-TTF nach *work_dir*, Filter nutzt nur Basisname → kein ``C:`` in ``-vf``."""
    name = "_vttool_drawfont.ttf"
    dest = work_dir / name
    src = _system_ttf_path()
    if not dest.is_file() or dest.stat().st_size != src.stat().st_size:
        shutil.copy2(src, dest)
    return name


def drawtext_visible_at_wallclock(
    time_sec: float,
    t_from: Optional[float],
    t_to: Optional[float],
) -> bool:
    """Gleiche Sichtbarkeit wie drawtext-*enable*, aber mit bekannter Videosekunde.

    Für Einzelbild-Vorschau nach ``-ss`` liefert der Filter-Timestamp ``t`` oft nicht die
    echte Position im Clip; deshalb in der Vorschau diese Hilfe statt *enable=* verwenden.
    """
    if t_from is None and t_to is None:
        return True
    if t_from is not None and t_to is not None:
        a, b = (t_from, t_to) if t_from <= t_to else (t_to, t_from)
        return a <= time_sec <= b
    if t_from is not None:
        return time_sec >= t_from
    assert t_to is not None
    return time_sec <= t_to


@dataclass
class TimedTextOverlay:
    """Text mit eigenem Erscheinungszeitfenster und Darstellung."""

    text: str
    fontcolor_hex: str
    fontsize: int
    pos_x: int
    pos_y: int
    box_border_w: int = 3
    #: Halbtransparenter Kasten hinter dem Text (drawtext + Pillow).
    box_enabled: bool = True
    line_spacing: int = 0
    text_visible_from_sec: Optional[float] = None
    text_visible_to_sec: Optional[float] = None
    # Leer/None = Standard-Schrift (_ensure_draw_font_basename); sonst Pfad zu .ttf/.otf/.ttc
    font_path: Optional[str] = None
    # Optional: eigene Italic-TTF für Pillow-Raster (Kursiv ohne Scherung); ASS kann das nicht.
    italic_font_path: Optional[str] = None
    bold: bool = False
    italic: bool = False
    strike: bool = False


def _overlay_visual_lines(content: str) -> List[str]:
    """Zeilen wie der Nutzer sie mit Enter trennt (auch leere Zeilen)."""
    return (content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def overlay_segment_has_visible_text(overlay: TimedTextOverlay) -> bool:
    return any(line.strip() for line in _overlay_visual_lines(overlay.text))


def _drawtext_line_step_px(fontsize: int, line_spacing: int) -> int:
    """Vertikaler Abstand zwischen gestapelten Ein-Zeil-drawtext-Filtern."""
    sz = max(8, min(int(fontsize), 400))
    ls = max(-120, min(int(line_spacing), 120))
    return max(4, sz + ls)


def _hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    h = hex_color.strip().lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _blend_toward_white(rgb: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    """Mischt RGB Richtung Weiß (t∈[0,1]) — gleicht dünnere Kursiv-Kanten/Anti-Alias aus."""
    t = max(0.0, min(1.0, t))
    return tuple(int(round(c + (255 - c) * t)) for c in rgb)


def _overlay_enable_suffix(t_from: Optional[float], t_to: Optional[float]) -> str:
    """FFmpeg *overlay*-Filter ``enable=…`` ; Kommas als ``\\,``."""
    if t_from is None and t_to is None:
        return ""

    def fmt(x: float) -> str:
        s = f"{float(x):.6f}".rstrip("0").rstrip(".")
        return s if s else "0"

    if t_from is not None and t_to is not None:
        a, b = (t_from, t_to) if t_from <= t_to else (t_to, t_from)
        return f":enable='between(t\\,{fmt(a)}\\,{fmt(b)})'"
    if t_from is not None:
        return f":enable='gte(t\\,{fmt(t_from)})'"
    assert t_to is not None
    return f":enable='lte(t\\,{fmt(t_to)})'"


def _strike_line_rgba(rgb: Tuple[int, int, int]) -> Tuple[int, int, int, int]:
    """Durchstreich-Linie dunkel auf hellem Text, hell auf dunklem Text."""
    lum = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    if lum >= 140:
        return (0, 0, 0, 252)
    return (255, 255, 255, 252)


def _tight_crop_rgba(im: Any) -> Any:
    """Entfernt überflüssige Transparenz — kleinerer Overlay-Kasten beeinträchtigt weniger andere Layer."""
    alpha = im.split()[3]
    bb = alpha.getbbox()
    if bb:
        return im.crop(bb)
    return im


# Häufige Windows-Kombinationen Regular → Italic (Dateiname in Fonts)
_WIN_ITALIC_FILE: Dict[str, str] = {
    "arial": "ariali.ttf",
    "arialbd": "arialbi.ttf",
    "segoeui": "segoeuii.ttf",
    "calibri": "calibrii.ttf",
    "verdana": "verdanai.ttf",
    "tahoma": "tahomai.ttf",
    "times": "timesi.ttf",
    "timesnr": "timesi.ttf",
}


def _guess_italic_font_file(regular: Path) -> Optional[Path]:
    """Sucht eine passende Italic/Oblique-Datei zur gewählten Schrift (echtes Kursiv)."""
    if not regular.is_file():
        return None
    parent = regular.resolve().parent
    stem = regular.stem
    suf = regular.suffix.lower()
    if suf not in (".ttf", ".otf", ".ttc"):
        suf = ".ttf"

    for suffix in (
        f"{stem}-Italic{suf}",
        f"{stem}_Italic{suf}",
        f"{stem} Italic{suf}",
        f"{stem}-Italic.ttf",
        f"{stem}-Oblique{suf}",
        f"{stem}Oblique{suf}",
        f"{stem}-Oblique.ttf",
    ):
        p = parent / suffix
        if p.is_file():
            return p

    stem_l = stem.lower()
    win = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    mapped = _WIN_ITALIC_FILE.get(stem_l)
    if mapped and (win / mapped).is_file():
        return win / mapped

    if "dejavu" in stem_l and "sans" in stem_l:
        for name in ("DejaVuSans-Oblique.ttf", "DejaVuSans-Obl.ttf"):
            p = win / name
            if p.is_file():
                return p
        for name in ("DejaVuSans-Oblique.ttf",):
            p = parent / name
            if p.is_file():
                return p

    return None


def _italic_shear_pil(im: Any, slant: float = 0.18) -> Any:
    """Nur horizontale Scherung (Basis bleibt waagerecht), kein Drehen.

    Pillow: Ausgabe-Pixel (x,y) liest Eingabe bei (a*x+b*y+c, …); für Slant nach rechts
    unten: Quelle (x - slant*y + pad, y).
    """
    from PIL import Image

    if abs(slant) < 1e-6:
        return im
    _w, h = im.size
    pad = int(round(slant * h)) + 8
    nw = _w + pad
    c_off = float(pad - 2)
    data = (1.0, -slant, c_off, 0.0, 1.0, 0.0)
    fillcolor = (0, 0, 0, 0)
    try:
        aff = Image.Transform.AFFINE
        res = Image.Resampling.LANCZOS
    except AttributeError:
        aff = Image.AFFINE  # type: ignore[attr-defined]
        res = Image.LANCZOS  # type: ignore[attr-defined]
    return im.transform((nw, h), aff, data, resample=res, fillcolor=fillcolor)


def _render_styled_overlay_png(
    work_dir: Path,
    ov: TimedTextOverlay,
    lines: List[str],
    font_bn: str,
    *,
    original_font_path: Optional[str],
) -> str:
    """Text mit Fett/Kursiv/Durchgestrichen als RGBA-PNG; Basisname zurück."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise ProbeError(
            "Pillow wird für Fett/Kursiv/Durchgestrichen benötigt (pip install Pillow)."
        ) from exc

    fp_draw = work_dir / font_bn
    sz = max(8, min(int(ov.fontsize), 400))
    italic_bn = _stage_optional_font_basename(work_dir, ov.italic_font_path)
    # Hohes Supersampling bei Kursiv: schmale Italic-Striche + Scherung wirken sonst grau/dunkel.
    hi = 4 if ov.italic else 2
    sz_hi = sz * hi

    lookup_regular: Optional[Path] = None
    if original_font_path and Path(original_font_path).is_file():
        lookup_regular = Path(original_font_path).resolve()
    else:
        try:
            lookup_regular = _system_ttf_path()
        except ProbeError:
            lookup_regular = None

    italic_file: Optional[Path] = None
    if ov.italic:
        if italic_bn is not None:
            italic_file = work_dir / italic_bn
        elif lookup_regular is not None:
            italic_file = _guess_italic_font_file(lookup_regular)
    use_shear_italic = bool(ov.italic and italic_file is None)

    font_path_load = str(italic_file) if italic_file is not None else str(fp_draw)
    try:
        font = ImageFont.truetype(font_path_load, sz_hi)
    except OSError as exc:
        raise ProbeError(
            f"Schriftdatei konnte nicht geladen werden: {Path(font_path_load).name}"
        ) from exc

    rgb_base = _hex_to_rgb(ov.fontcolor_hex)
    # Dünne geneigte Glyphen: mehr Kanten-Anteil auf dunklem box → wirkt matschig/dunkler als drawtext.
    if ov.italic and not ov.bold:
        rgb = _blend_toward_white(rgb_base, 0.13)
    else:
        rgb = rgb_base
    fill_rgba = (*rgb, 255)
    strike_rgba = _strike_line_rgba(rgb)

    stroke_w = 0
    if ov.bold:
        stroke_w = max(stroke_w, max(hi, sz_hi // 22))
    if ov.italic and use_shear_italic:
        stroke_w = max(stroke_w, max(hi, sz_hi // 19))
    elif ov.italic and not ov.bold:
        stroke_w = max(stroke_w, max(hi, sz_hi // 28))

    dy_hi = _drawtext_line_step_px(ov.fontsize, ov.line_spacing) * hi
    bb_draw = max(0, min(int(ov.box_border_w), 80))
    bb_hi = bb_draw * hi
    box_bg_rgba = (0, 0, 0, int(255 * 0.35))

    probe = Image.new("RGBA", (2, 2), (0, 0, 0, 0))
    probe_dr = ImageDraw.Draw(probe)

    line_imgs: List[Any] = []
    max_w = 1

    for raw in lines:
        display = raw if raw.strip() else "\u00a0"
        tb_kw: Dict[str, object] = {"font": font}
        if stroke_w:
            tb_kw["stroke_width"] = stroke_w
        try:
            bbox = probe_dr.textbbox((0, 0), display, **tb_kw)
        except TypeError:
            bbox = probe_dr.textbbox((0, 0), display, font=font)

        bw_raw = max(1, bbox[2] - bbox[0])
        bh_raw = max(1, bbox[3] - bbox[1])
        pad_line = (4 * hi) + stroke_w * 2
        lw = bw_raw + pad_line
        lh = bh_raw + pad_line
        lim = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
        dr = ImageDraw.Draw(lim)
        ox = pad_line // 2 - bbox[0]
        oy = pad_line // 2 - bbox[1]

        try:
            tbb = dr.textbbox((ox, oy), display, font=font, stroke_width=stroke_w or 0)
        except TypeError:
            tbb = dr.textbbox((ox, oy), display, font=font)
        bx0, by0, bx1, by1 = tbb[0], tbb[1], tbb[2], tbb[3]
        bg_pad = max(2 * hi, hi + bb_hi // 4)
        bg_rect = [
            max(0, bx0 - bg_pad),
            max(0, by0 - bg_pad),
            min(lw - 1, bx1 + bg_pad),
            min(lh - 1, by1 + bg_pad),
        ]
        rad = min(6 * hi, bg_pad + hi)
        if ov.box_enabled:
            try:
                dr.rounded_rectangle(bg_rect, radius=rad, fill=box_bg_rgba)
                if bb_hi > 0:
                    dr.rounded_rectangle(
                        bg_rect,
                        radius=rad,
                        outline=(255, 255, 255, 40),
                        width=min(bb_hi, 4 * hi),
                    )
            except AttributeError:
                dr.rectangle(bg_rect, fill=box_bg_rgba)
                if bb_hi > 0:
                    dr.rectangle(
                        bg_rect,
                        outline=(255, 255, 255, 40),
                        width=min(bb_hi, 4 * hi),
                    )

        t_kw: Dict[str, object] = {"font": font, "fill": fill_rgba}
        if stroke_w:
            t_kw["stroke_width"] = stroke_w
            t_kw["stroke_fill"] = fill_rgba
        try:
            dr.text((ox, oy), display, **t_kw)
        except TypeError:
            dr.text((ox, oy), display, font=font, fill=fill_rgba)

        if ov.strike:
            try:
                bb = dr.textbbox((ox, oy), display, font=font, stroke_width=stroke_w or 0)
            except TypeError:
                bb = dr.textbbox((ox, oy), display, font=font)
            sx0, sy0, sx1, sy1 = bb
            cy = (sy0 + sy1) // 2
            sw_line = max(hi, sz_hi // 22)
            x1, x2 = sx0 - hi, sx1 + hi
            dr.line((x1, cy, x2, cy), fill=strike_rgba, width=sw_line)

        if use_shear_italic:
            lim = _italic_shear_pil(lim, slant=0.085)

        line_imgs.append(lim)
        max_w = max(max_w, lim.size[0])

    if not line_imgs:
        raise ProbeError("Kein Text zum Rendern.")

    stack_h = dy_hi * (len(line_imgs) - 1) + line_imgs[-1].size[1]
    stack = Image.new("RGBA", (max_w, stack_h), (0, 0, 0, 0))
    for i, lim in enumerate(line_imgs):
        stack.paste(lim, (0, i * dy_hi), lim)

    stack = _tight_crop_rgba(stack)

    try:
        res_hi = Image.Resampling.BICUBIC if ov.italic else Image.Resampling.LANCZOS
    except AttributeError:
        res_hi = Image.BICUBIC if ov.italic else Image.LANCZOS  # type: ignore[attr-defined]
    nw = max(1, stack.size[0] // hi)
    nh = max(1, stack.size[1] // hi)
    canvas = stack.resize((nw, nh), resample=res_hi)

    png_bn = f"_vtst_{uuid.uuid4().hex[:12]}.png"
    canvas.save(work_dir / png_bn, format="PNG")
    return png_bn


def _prepare_segment_font(work_dir: Path, font_path: Optional[str]) -> str:
    """Kopiert Schrift nach *work_dir*, gibt Basisnamen für drawtext/Pillow zurück."""
    if font_path and os.path.isfile(font_path):
        src = Path(font_path).resolve()
        suf = src.suffix.lower()
        if suf not in (".ttf", ".otf", ".ttc"):
            suf = ".ttf"
        bn = f"_vtfu_{uuid.uuid4().hex[:12]}{suf}"
        shutil.copy2(src, work_dir / bn)
        return bn

    return _ensure_draw_font_basename(work_dir)


def _stage_optional_font_basename(work_dir: Path, font_path: Optional[str]) -> Optional[str]:
    """Kopiert eine beliebige Schrift nach *work_dir*; None wenn Pfad fehlt oder ungültig."""
    if not font_path or not os.path.isfile(font_path):
        return None
    src = Path(font_path).resolve()
    suf = src.suffix.lower()
    if suf not in (".ttf", ".otf", ".ttc"):
        suf = ".ttf"
    bn = f"_vtfi_{uuid.uuid4().hex[:12]}{suf}"
    shutil.copy2(src, work_dir / bn)
    return bn


def _append_subtitles_vf_part(parts: List[str], work_dir: Path, srt_path: Optional[str]) -> None:
    if not srt_path or not os.path.isfile(srt_path):
        return
    sf = Path(srt_path).resolve()
    if sf.parent.resolve() == work_dir.resolve():
        parts.append(f"subtitles={sf.name}")
    else:
        sub_copy = work_dir / "_vttool_overlay.srt"
        shutil.copy2(sf, sub_copy)
        parts.append(f"subtitles={sub_copy.name}")


def _vf_parts_use_movie_overlay(parts: Sequence[str]) -> bool:
    """True wenn PNG-Text mit ffmpeg *movie*+*overlay* eingebunden wird (nicht mit einfachem *-vf*)."""
    return any(p.startswith("movie=") for p in parts)


def _compile_vf_parts_to_filter_complex(parts: Sequence[str], trailing: str) -> str:
    """Baut *filter_complex*: ``movie`` erzeugt einen zweiten Zweig — geht nicht mit einfachem *-vf*.

    *trailing* wird direkt an die aktuelle Videokette angehängt und sollte mit einem Filter
    beginnen (z.B. ``scale=960:-2[vout]`` oder ``null[vout]``) und das Ausgabe-Pad ``[vout]``
    enthalten (bei GIF inkl. Palette bis ``[vout]``).
    """
    idx = 0
    cur = "[0:v]"
    chunks: List[str] = []
    linear_buf: List[str] = []
    plist = list(parts)
    i = 0

    def flush_linear() -> None:
        nonlocal cur, linear_buf, idx
        if not linear_buf:
            return
        idx += 1
        lab = f"vt{idx}"
        chunks.append(f"{cur}{','.join(linear_buf)}[{lab}]")
        cur = f"[{lab}]"
        linear_buf.clear()

    while i < len(plist):
        p = plist[i]
        if p.startswith("movie="):
            flush_linear()
            if i + 1 >= len(plist):
                raise ProbeError("Unvollständige Filterkette (movie ohne overlay).")
            ov_line = plist[i + 1].replace("[in]", cur)
            idx += 1
            lab = f"vt{idx}"
            chunks.append(f"{p};{ov_line}[{lab}]")
            cur = f"[{lab}]"
            i += 2
        else:
            linear_buf.append(p)
            i += 1

    flush_linear()

    body = ";".join(chunks)
    joiner = ";" if body else ""
    return f"{body}{joiner}{cur}{trailing}"


def build_timed_overlay_vf_parts(
    work_dir: Path,
    *,
    srt_path: Optional[str],
    overlays: Sequence[TimedTextOverlay],
    preview_wallclock_sec: Optional[float],
    play_res_width: int,
    play_res_height: int,
) -> List[str]:
    """Filterketten-Segmente (ohne *scale*). Legt Hilfsdateien in *work_dir* an."""
    parts: List[str] = []
    _append_subtitles_vf_part(parts, work_dir, srt_path)

    for ov in overlays:
        if not overlay_segment_has_visible_text(ov):
            continue

        lines = _overlay_visual_lines(ov.text)

        if preview_wallclock_sec is not None:
            if not drawtext_visible_at_wallclock(
                preview_wallclock_sec,
                ov.text_visible_from_sec,
                ov.text_visible_to_sec,
            ):
                continue

        font_bn = _prepare_segment_font(work_dir, ov.font_path)

        use_raster_styles = ov.bold or ov.italic or ov.strike
        if use_raster_styles:
            png_bn = _render_styled_overlay_png(
                work_dir,
                ov,
                lines,
                font_bn,
                original_font_path=ov.font_path,
            )
            lbl = f"st{uuid.uuid4().hex[:10]}"
            if preview_wallclock_sec is not None:
                enable_sfx = ""
            else:
                enable_sfx = _overlay_enable_suffix(
                    ov.text_visible_from_sec,
                    ov.text_visible_to_sec,
                )
            parts.append(f"movie={png_bn}[{lbl}]")
            parts.append(
                f"[in][{lbl}]overlay={int(ov.pos_x)}:{int(ov.pos_y)}{enable_sfx}"
            )
            continue

        if preview_wallclock_sec is not None:
            timing = ""
        else:
            timing = _drawtext_timing_prefix(
                ov.text_visible_from_sec,
                ov.text_visible_to_sec,
            )

        fc = hex_to_drawtext_color(ov.fontcolor_hex)
        sz = max(8, min(int(ov.fontsize), 400))
        bw = max(0, min(int(ov.box_border_w), 80))
        dy = _drawtext_line_step_px(ov.fontsize, ov.line_spacing)

        if ov.box_enabled:
            box_tail = f"box=1:boxcolor=black@0.35:boxborderw={bw}"
        else:
            box_tail = "box=0"
        tail = f"{timing}fontsize={sz}:fontcolor={fc}:{box_tail}"

        for i, line in enumerate(lines):
            display = line if line.strip() else "\u00a0"
            fn = f"_vtln_{uuid.uuid4().hex}.txt"
            (work_dir / fn).write_text(display, encoding="utf-8")
            y = int(ov.pos_y) + i * dy
            parts.append(
                f"drawtext=fontfile={font_bn}:textfile={fn}:"
                f"x={int(ov.pos_x)}:y={y}:{tail}"
            )

    return parts


def _drawtext_timing_prefix(
    t_from: Optional[float],
    t_to: Optional[float],
) -> str:
    """FFmpeg drawtext *enable=* ; Kommas im Filter als ``\\,`` (Komma trennt Filter)."""
    if t_from is None and t_to is None:
        return ""

    def _fmt(x: float) -> str:
        s = f"{float(x):.6f}".rstrip("0").rstrip(".")
        return s if s else "0"

    if t_from is not None and t_to is not None:
        a, b = (t_from, t_to) if t_from <= t_to else (t_to, t_from)
        return f"enable=between(t\\,{_fmt(a)}\\,{_fmt(b)}):"
    if t_from is not None:
        return f"enable=gte(t\\,{_fmt(t_from)}):"
    assert t_to is not None
    return f"enable=lte(t\\,{_fmt(t_to)}):"


def hex_to_drawtext_color(hex_color: str) -> str:
    h = hex_color.strip().lstrip("#")
    if len(h) != 6:
        return "white"
    return "0x" + h.upper()


def extract_frame_png_bytes(
    video_path: str,
    time_sec: float,
    *,
    cached_info: Optional[VideoInfo] = None,
    width_max: int = 1280,
    height_max: int = 720,
) -> bytes:
    """Decode one preview frame to PNG bytes (scaled down, keeps aspect)."""
    ffmpeg = find_ffmpeg()
    info = cached_info if cached_info is not None else probe_video(video_path)
    t = max(0.0, min(time_sec, max(0.0, info.duration_sec - 1e-3)))
    scale_filter = _preview_scale_filter(width_max, height_max)

    args = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-ss",
        f"{t:.3f}",
        "-i",
        video_path,
        "-vf",
        scale_filter,
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "-",
    ]
    proc = subprocess.run(
        args,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0 or not proc.stdout:
        raise ProbeError(
            (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            or "Vorschau-Frame konnte nicht gelesen werden."
        )
    return proc.stdout


def extract_preview_frame_with_overlay(
    video_path: str,
    time_sec: float,
    *,
    overlays: Sequence[TimedTextOverlay],
    srt_path: Optional[str],
    cached_info: Optional[VideoInfo] = None,
    width_max: int = 1280,
    height_max: int = 720,
) -> bytes:
    """Einzelbild-Vorschau inkl. Untertitel + gestapelte drawtext-Zeilen."""
    ffmpeg = find_ffmpeg()
    info = cached_info if cached_info is not None else probe_video(video_path)
    t = max(0.0, min(time_sec, max(0.0, info.duration_sec - 1e-3)))

    sf = srt_path if srt_path and os.path.isfile(srt_path) else None
    need_workspace = bool([o for o in overlays if overlay_segment_has_visible_text(o)]) or bool(
        sf
    )

    work: Optional[Path] = None
    try:
        vf_segments: List[str]
        if need_workspace:
            work = Path(tempfile.mkdtemp(prefix="vttool_prev_"))
            vf_segments = build_timed_overlay_vf_parts(
                work,
                srt_path=sf,
                overlays=overlays,
                preview_wallclock_sec=t,
                play_res_width=int(info.width),
                play_res_height=int(info.height),
            )
        else:
            vf_segments = []

        scale_f = _preview_scale_filter(width_max, height_max)
        # Wie Export: zuerst yuv420p (Chroma-Subsampling), dann runterskalieren — sonst wirkt
        # die Vorschau farb-/kantenreiner als das encodierte Video.
        preview_tail = f"format=yuv420p,{scale_f},format=rgb24[vout]"
        if vf_segments and _vf_parts_use_movie_overlay(vf_segments):
            fc = _compile_vf_parts_to_filter_complex(vf_segments, preview_tail)
            vf_args: List[str] = ["-filter_complex", fc, "-map", "[vout]"]
        elif vf_segments:
            vf_segments.extend(["format=yuv420p", scale_f, "format=rgb24"])
            vf_args = ["-vf", ",".join(vf_segments)]
        else:
            vf_args = ["-vf", ",".join(["format=yuv420p", scale_f, "format=rgb24"])]

        args = [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-ss",
            f"{t:.3f}",
            "-i",
            video_path,
            *vf_args,
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ]
        proc = subprocess.run(
            args,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            cwd=str(work) if work else None,
        )
        if proc.returncode != 0 or not proc.stdout:
            err = (proc.stderr or b"").decode("utf-8", errors="replace")
            raise ProbeError(err.strip() or "Vorschau mit Text konnte nicht erstellt werden.")
        return proc.stdout
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


def _preview_scale_filter(wmax: int, hmax: int) -> str:
    _ = hmax  # height follows aspect when width is capped
    return f"scale={int(wmax)}:-2"


def map_codec_to_lib(codec_label: str) -> str:
    m = {
        "H.264 (libx264)": "libx264",
        "H.265 / HEVC (libx265)": "libx265",
        "VP9 (libvpx-vp9)": "libvpx-vp9",
        "AV1 (libsvtav1)": "libsvtav1",
    }
    return m.get(codec_label, "libx264")


def parse_bitrate(s: str) -> str:
    t = (s or "").strip().replace(",", ".")
    if not t:
        return "8M"
    low = t.lower()
    if low.endswith("k") or low.endswith("m") or low.endswith("g"):
        return t
    # bare number → treat as kbps for convenience (like user's bitrate tool readme)
    try:
        n = float(t)
        if n < 100000:
            return f"{int(n)}k"
        return str(int(n))
    except ValueError:
        return "8M"


_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")


def parse_time_seconds(stderr_line: str) -> Optional[float]:
    m = _TIME_RE.search(stderr_line)
    if not m:
        return None
    hh, mm, ss = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return hh * 3600 + mm * 60 + ss


def build_gif_palette_vf_suffix(fps: int, max_width: int, palette_colors: int) -> str:
    """Hohe GIF-Qualität: FPS limitieren, skalieren, Palette aus dem gefilterten Video."""
    fp = max(1, min(int(fps), 60))
    mw = max(16, min(int(max_width), 4096))
    ncol = max(8, min(int(palette_colors), 256))
    # Kommas in scale-Ausdrücken für FFmpeg escapen.
    # stats_mode=full: gesamte Frames fließen ins Histogramm — mit diff fehlen oft Farben
    # von statischem Text (z. B. gerasterte PNG-Overlays), dann verschwindet er nach paletteuse.
    return (
        f"fps={fp},scale=min(iw\\,{mw}):-2:flags=lanczos,"
        f"split[s0][s1];[s0]palettegen=max_colors={ncol}:stats_mode=full[p];"
        f"[s1][p]paletteuse=dither=bayer:bayer_scale=5"
    )


def build_export_command(
    *,
    src: str,
    dst: str,
    encoder: str,
    bitrate: str,
    overlays: Sequence[TimedTextOverlay],
    srt_path: Optional[str],
    audio_copy: bool,
    progress_file: Optional[str],
    export_as_gif: bool = False,
    gif_fps: int = 15,
    gif_max_width: int = 720,
    gif_palette_colors: int = 128,
    video_width: int = 1920,
    video_height: int = 1080,
) -> Tuple[List[str], Optional[str]]:
    """Gibt (ffmpeg-Argumentliste, Arbeitsordner) zurück.

    Der Ordner enthält Schrift-, Untertitel- und Zeilentextdateien und soll nach ffmpeg
    mit ``shutil.rmtree`` entfernt werden.
    """
    ffmpeg = find_ffmpeg()
    sf = srt_path if srt_path and os.path.isfile(srt_path) else None
    need_workspace = bool([o for o in overlays if overlay_segment_has_visible_text(o)]) or bool(
        sf
    )

    vf_arg: Optional[str] = None
    vf_cwd: Optional[str] = None
    ov_parts: List[str] = []
    uses_movie_overlay = False

    if need_workspace:
        work = Path(tempfile.mkdtemp(prefix="vttool_export_"))
        vf_cwd = str(work)
        pw = max(16, min(int(video_width), 8192))
        ph = max(16, min(int(video_height), 8192))
        ov_parts = build_timed_overlay_vf_parts(
            work,
            srt_path=sf,
            overlays=overlays,
            preview_wallclock_sec=None,
            play_res_width=pw,
            play_res_height=ph,
        )
        vf_arg = ",".join(ov_parts) if ov_parts else None
        uses_movie_overlay = bool(ov_parts and _vf_parts_use_movie_overlay(ov_parts))

    args: List[str] = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", src]

    if export_as_gif:
        sfx = build_gif_palette_vf_suffix(gif_fps, gif_max_width, gif_palette_colors)
        if vf_arg:
            if uses_movie_overlay:
                tail = f"format=yuv420p,{sfx}[vout]"
                fc = _compile_vf_parts_to_filter_complex(ov_parts, tail)
                args.extend(["-filter_complex", fc, "-map", "[vout]"])
            else:
                vf_merged = f"{vf_arg},format=yuv420p,{sfx}"
                args.extend(["-vf", vf_merged])
        else:
            args.extend(["-vf", sfx])
        args.extend(["-c:v", "gif", "-loop", "0", "-an"])
    else:
        if vf_arg:
            if uses_movie_overlay:
                fc = _compile_vf_parts_to_filter_complex(ov_parts, "null[vout]")
                args.extend(["-filter_complex", fc, "-map", "[vout]", "-map", "0:a?"])
            else:
                args.extend(["-vf", vf_arg])
        args.extend(["-c:v", encoder, "-b:v", bitrate])

        if encoder == "libx264":
            args.extend(["-profile:v", "high", "-pix_fmt", "yuv420p"])
        elif encoder == "libx265":
            args.extend(["-pix_fmt", "yuv420p"])
        elif encoder == "libvpx-vp9":
            args.extend(["-row-mt", "1", "-pix_fmt", "yuv420p"])
        elif encoder == "libsvtav1":
            args.extend(["-pix_fmt", "yuv420p"])

        if audio_copy:
            args.extend(["-c:a", "copy"])
        else:
            args.extend(["-c:a", "aac", "-b:a", "192k"])

    low = dst.lower()
    if not export_as_gif and low.endswith((".mp4", ".m4v", ".mov")):
        args.extend(["-movflags", "+faststart"])

    if progress_file:
        args.extend(["-progress", progress_file, "-nostats"])

    args.append(dst)
    return args, vf_cwd


def write_temp_textfile(content: str) -> str:
    fd, path = tempfile.mkstemp(prefix="vttext_", suffix=".txt", text=False)
    os.close(fd)
    data = (content or "").replace("\r\n", "\n")
    Path(path).write_text(data, encoding="utf-8")
    return path
