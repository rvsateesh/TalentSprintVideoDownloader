# TalentSprint Video Downloader

Small Python utility to inspect a TalentSprint learning page for direct video sources (mp4 or m3u8) and download them.

Usage examples:

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Download a page (provide cookies if login required):

```bash
# Provide the full page URL (do not include angle brackets). Example:
python download_talentsprint.py https://example.com/lesson --cookie "sessionid=..." -o my_videos
```

If the page exposes an HLS playlist (.m3u8), install `ffmpeg` and run:

```bash
# Provide the full HLS page URL (do not include angle brackets). Example:
python download_talentsprint.py https://example.com/lesson-with-hls --ffmpeg /path/to/ffmpeg
```

If the site uses heavy JS or DRM, the script may not find direct sources. Use browser devtools Network tab to capture direct video or HLS links.
