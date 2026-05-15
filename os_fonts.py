"""Installierte Schriftarten ermitteln (Windows: Registry + gängige Ordner)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple


def windows_fonts_directories() -> List[Path]:
    """Typische Ordner mit .ttf/.otf unter Windows."""
    out: List[Path] = []
    windir = os.environ.get("WINDIR", r"C:\Windows")
    out.append(Path(windir) / "Fonts")
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        out.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    return out


def preferred_font_dialog_initialdir() -> str:
    """Startordner für einen Dateidialog (erster vorhandener Schriftordner)."""
    for p in windows_fonts_directories():
        if p.is_dir():
            return str(p)
    return os.environ.get("USERPROFILE", str(Path.home()))


def _expand_font_reg_value(raw: str) -> str:
    t = raw.strip().strip('"')
    return os.path.expandvars(t)


def _resolve_font_file(
    value: str,
    system_fonts_dir: Path,
    local_fonts_dir: Path,
) -> Path | None:
    v = _expand_font_reg_value(value)
    if not v:
        return None
    low = v.lower()
    if low.endswith(".fon"):
        return None
    p = Path(v)
    if p.is_file():
        return p
    if not p.is_absolute():
        for base in (system_fonts_dir, local_fonts_dir):
            cand = (base / v).resolve()
            if cand.is_file():
                return cand
    return None


def _fonts_from_registry_windows() -> Dict[str, str]:
    """Pfad → Anzeigename (Registry liefert lesbare Namen wie „Segoe UI (TrueType)“)."""
    if sys.platform != "win32":
        return {}

    import winreg

    dirs = windows_fonts_directories()
    system_dir = dirs[0] if dirs else Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    local_dir = dirs[1] if len(dirs) > 1 else (Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts")

    subkey = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    by_path: Dict[str, str] = {}

    def harvest(root: int) -> None:
        try:
            with winreg.OpenKey(root, subkey) as key:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    i += 1
                    if not value or not isinstance(value, str):
                        continue
                    path = _resolve_font_file(value, system_dir, local_dir)
                    if not path:
                        continue
                    suf = path.suffix.lower()
                    if suf not in (".ttf", ".otf", ".ttc"):
                        continue
                    ps = str(path.resolve())
                    if ps not in by_path:
                        by_path[ps] = name

        except OSError:
            pass

    harvest(winreg.HKEY_LOCAL_MACHINE)
    harvest(winreg.HKEY_CURRENT_USER)
    return by_path


def _merge_scanned_font_files(by_path: Dict[str, str]) -> None:
    """Ergänzt Dateien im Fonts-Ordner, die nicht (oder falsch) in der Registry stehen."""
    seen = set(by_path.keys())
    for d in windows_fonts_directories():
        if not d.is_dir():
            continue
        for pattern in ("*.ttf", "*.otf", "*.ttc"):
            for p in sorted(d.glob(pattern)):
                try:
                    ps = str(p.resolve())
                except OSError:
                    continue
                if ps in seen:
                    continue
                seen.add(ps)
                by_path[ps] = p.stem


_FONT_LIST_CACHE: List[Tuple[str, str]] | None = None


def list_windows_font_choices(*, refresh: bool = False) -> List[Tuple[str, str]]:
    """[(Anzeigename, absoluter Pfad), …] sortiert; nur Windows, sonst []."""
    global _FONT_LIST_CACHE
    if sys.platform != "win32":
        return []
    if _FONT_LIST_CACHE is not None and not refresh:
        return list(_FONT_LIST_CACHE)

    reg = _fonts_from_registry_windows()
    _merge_scanned_font_files(reg)
    pairs = sorted(((label, path) for path, label in reg.items()), key=lambda x: x[0].lower())
    _FONT_LIST_CACHE = pairs
    return list(pairs)


def clear_font_list_cache() -> None:
    global _FONT_LIST_CACHE
    _FONT_LIST_CACHE = None
