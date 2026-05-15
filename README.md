# Subs

Desktop tool to **burn text overlays and subtitles into videos** — preview on a frame, tune timing and fonts, then export with **FFmpeg** or send to **DaVinci Resolve Studio**.

## What it does

- Load a video and edit **timed text segments** (position, font, visibility window)
- **Preview** a frame with overlays before export
- Export to **MP4** (H.264/H.265) or **GIF**
- Optional **DaVinci** path via `davinci_api.py` for Studio workflows

## Requirements

- Windows (primary)
- Python 3.10+
- **FFmpeg** on `PATH`
- **Pillow** (see `requirements.txt`)
- Resolve **Studio** only if you use the DaVinci export button

## Install

```bat
install.bat
python app.py
```

Settings are saved in `video_text_tool_settings.json` (local; see `video_text_tool_settings.example.json`).

## License

MIT
