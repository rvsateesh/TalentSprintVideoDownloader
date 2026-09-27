#!/usr/bin/env python3
"""Download video assets from a given TalentSprint learning page.

Usage: python download_talentsprint.py <url> [--output-dir DIR] [--cookie COOKIE] [--cookie-file FILE]

Notes:
- Supply authentication cookies if the content requires login.
- If the page exposes an HLS (.m3u8) playlist, ffmpeg is used to download it.
"""
import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from tqdm import tqdm


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"


def parse_args():
    p = argparse.ArgumentParser(description="Download video assets from a TalentSprint learning page")
    p.add_argument("url", help="Page URL to inspect and download from")
    p.add_argument("--output-dir", "-o", default="downloads", help="Directory to save files")
    p.add_argument("--cookie", help="One cookie string, e.g. 'sessionid=...' (can include multiple with '; ')")
    p.add_argument("--cookie-file", help="Load raw cookie header from a file")
    p.add_argument("--referer", help="Optional Referer header to send when fetching media/playlist")
    p.add_argument("--header", action="append", help="Extra header to send, format 'Name: value' (can be passed multiple times)")
    p.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable path if not on PATH")
    p.add_argument("--select", type=int, help="Select a candidate source index (0-based) if multiple found")
    return p.parse_args()


def build_session(cookie_header=None, extra_headers=None):
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    if cookie_header:
        # requests expects a dict of cookies; we can set the header directly for complex cases
        s.headers.update({"Cookie": cookie_header})
    if extra_headers:
        # extra_headers expected as dict
        for k, v in extra_headers.items():
            s.headers.update({k: v})
    return s


def find_video_sources(text, base_url=""):
    """Return list of candidate video URLs (m3u8, mp4) found in the HTML/text."""
    candidates = []
    # use finditer to get full match
    for it in re.finditer(r"https?://[^\'\"\s<>]+\.(?:m3u8|mp4)", text, flags=re.IGNORECASE):
        url = it.group(0)
        candidates.append(url)

    # Also check <video> and <source> tags via BeautifulSoup caller if provided separately
    return list(dict.fromkeys(candidates))


def extract_from_soup(soup, base_url=""):
    candidates = []
    # <video src>
    for v in soup.find_all("video"):
        src = v.get("src")
        if src:
            candidates.append(urljoin(base_url, src))
        for s in v.find_all("source"):
            src2 = s.get("src")
            if src2:
                candidates.append(urljoin(base_url, src2))

    # <a href> that end with mp4/m3u8
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"\.(mp4|m3u8)$", href, flags=re.IGNORECASE):
            candidates.append(urljoin(base_url, href))

    # look into scripts for JSON blobs
    for script in soup.find_all("script"):
        if script.string:
            candidates += find_video_sources(script.string, base_url)

    return list(dict.fromkeys(candidates))


def download_file(session, url, out_path):
    r = session.get(url, stream=True, allow_redirects=True)
    total = r.headers.get("content-length")
    if r.status_code >= 400:
        raise RuntimeError(f"Failed to download {url}: {r.status_code}")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    if total is None:
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    else:
        total = int(total)
        with open(out_path, "wb") as f, tqdm(total=total, unit="B", unit_scale=True, desc=os.path.basename(out_path)) as pbar:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    pbar.update(len(chunk))


def download_hls(ffmpeg_cmd, m3u8_url, out_path, headers=None, user_agent=None, use_local_playlist=False):
    cmd = [ffmpeg_cmd, "-y", "-hide_banner", "-loglevel", "error"]
    if user_agent:
        cmd += ["-user_agent", user_agent]
    # allow common protocols when using a local playlist that references http(s)
    cmd += ["-protocol_whitelist", "file,http,https,tcp,tls"]

    if headers:
        # ffmpeg expects a single header block separated by CRLF
        cmd += ["-headers", headers]

    cmd += ["-i", m3u8_url, "-c", "copy", out_path]
    print("Running:", " ".join(cmd))
    res = subprocess.run(cmd)
    if res.returncode != 0:
        raise RuntimeError("ffmpeg failed to download HLS stream")


def write_local_playlist(session, remote_m3u8_url, local_path):
    """Fetch the remote m3u8 with `session`, rewrite relative segment URLs to absolute,
    and write to `local_path`. Returns True on success, False otherwise."""
    try:
        r = session.get(remote_m3u8_url, timeout=15)
    except requests.exceptions.RequestException:
        return False
    if r.status_code != 200 or "#EXTM3U" not in r.text:
        return False

    lines = []
    for line in r.text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
        else:
            # resolve relative URIs against the playlist URL
            abs_url = urljoin(remote_m3u8_url, stripped)
            lines.append(abs_url)

    with open(local_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return True


def detect_encrypted_hls(playlist_text):
    """Return True if the playlist contains an EXT-X-KEY stanza with a METHOD other than NONE."""
    for line in playlist_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-KEY"):
            if "METHOD=AES-128" in stripped.upper() or "METHOD=SAMPLE-AES" in stripped.upper():
                return True
    return False


def parse_key_info(playlist_text):
    """Parse EXT-X-KEY attributes from the playlist text, returning (method, uri, iv_bytes, has_explicit_iv)."""
    for line in playlist_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#EXT-X-KEY"):
            continue
        method = None
        uri = None
        iv = None
        attrs = stripped.split(":", 1)[1] if ":" in stripped else stripped
        for part in attrs.split(","):
            if part.upper().startswith("METHOD="):
                method = part.split("=", 1)[1].strip().strip('"')
            elif part.upper().startswith("URI="):
                uri = part.split("=", 1)[1].strip().strip('"')
            elif part.upper().startswith("IV="):
                iv = part.split("=", 1)[1].strip().strip('"')
        if iv:
            iv_value = iv.lstrip("0x").lstrip("0X")
            iv_bytes = bytes.fromhex(iv_value) if iv_value else b"\x00" * 16
            return method, uri, iv_bytes, True
        return method, uri, b"\x00" * 16, False
    return None, None, b"\x00" * 16, False


def decrypt_segment(payload, key, iv):
    """Decrypt AES-128 CBC payload using the supplied key and IV."""
    if len(key) not in {16, 24, 32}:
        raise RuntimeError(f"Unsupported AES key length: {len(key)}")
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return unpad(cipher.decrypt(payload), AES.block_size)


def download_hls_without_ffmpeg(session, m3u8_url, out_path):
    """Download an HLS playlist and its segments with requests, then concatenate them into a playable file."""
    try:
        playlist_resp = session.get(m3u8_url, timeout=30)
        playlist_resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to fetch playlist {m3u8_url}: {e}") from e

    if "#EXTM3U" not in playlist_resp.text:
        raise RuntimeError(f"Playlist {m3u8_url} did not contain an EXTM3U header")

    method, key_uri, iv_bytes, has_explicit_iv = parse_key_info(playlist_resp.text)
    if method and method.upper() != "NONE":
        if method.upper() != "AES-128":
            raise RuntimeError(f"Unsupported HLS encryption method: {method}")
        if not key_uri:
            raise RuntimeError("The playlist declares AES-128 encryption but no key URI was found")
        try:
            key_resp = session.get(urljoin(m3u8_url, key_uri), timeout=30)
            key_resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Failed to fetch HLS key {key_uri}: {e}") from e
        key = key_resp.content
    else:
        key = None
        iv_bytes = b"\x00" * 16

    segment_urls = []
    for line in playlist_resp.text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lower().endswith((".m3u8", ".m3u")):
            child_url = urljoin(m3u8_url, stripped)
            child_resp = session.get(child_url, timeout=30)
            child_resp.raise_for_status()
            if "#EXTM3U" not in child_resp.text:
                continue
            child_method, child_key_uri, child_iv, child_has_iv = parse_key_info(child_resp.text)
            if child_method and child_method.upper() != "NONE":
                if child_method.upper() != "AES-128":
                    raise RuntimeError(f"Unsupported HLS encryption method in child playlist: {child_method}")
                if not child_key_uri:
                    raise RuntimeError("The child playlist declares AES-128 encryption but no key URI was found")
                try:
                    child_key_resp = session.get(urljoin(child_url, child_key_uri), timeout=30)
                    child_key_resp.raise_for_status()
                except requests.exceptions.RequestException as e:
                    raise RuntimeError(f"Failed to fetch HLS key {child_key_uri}: {e}") from e
                key = child_key_resp.content
                iv_bytes = child_iv
                has_explicit_iv = child_has_iv
            for child_line in child_resp.text.splitlines():
                child_stripped = child_line.strip()
                if not child_stripped or child_stripped.startswith("#"):
                    continue
                if child_stripped.lower().endswith((".m3u8", ".m3u")):
                    continue
                segment_urls.append(urljoin(child_url, child_stripped))
        else:
            segment_urls.append(urljoin(m3u8_url, stripped))

    if not segment_urls:
        raise RuntimeError("No segment URLs were found in the playlist")

    # Choose a container extension that players understand.
    container_ext = ".ts"
    for segment_url in segment_urls:
        seg_ext = os.path.splitext(os.path.basename(urlparse(segment_url).path))[1].lower()
        if seg_ext in {".m4s", ".mp4", ".m4v", ".mov"}:
            container_ext = ".mp4"
            break
        if seg_ext in {".ts", ".m2ts", ".m2t"}:
            container_ext = ".ts"
            break

    if os.path.splitext(out_path)[1].lower() not in {".ts", ".mp4"}:
        out_path += container_ext

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    temp_paths = []
    try:
        for idx, segment_url in enumerate(segment_urls):
            parsed = urlparse(segment_url)
            ext = os.path.splitext(os.path.basename(parsed.path))[1] or ".ts"
            temp_path = os.path.join(os.path.dirname(out_path), f"segment_{idx:03d}{ext}")
            segment_resp = session.get(segment_url, stream=True, allow_redirects=True)
            if segment_resp.status_code >= 400:
                raise RuntimeError(f"Failed to download segment {segment_url}: {segment_resp.status_code}")
            segment_bytes = segment_resp.content
            if key is not None:
                if has_explicit_iv:
                    segment_iv = iv_bytes
                else:
                    segment_iv = struct.pack(">Q", idx).ljust(16, b"\x00")
                decrypted_bytes = decrypt_segment(segment_bytes, key, segment_iv)
                with open(temp_path, "wb") as f:
                    f.write(decrypted_bytes)
            else:
                with open(temp_path, "wb") as f:
                    f.write(segment_bytes)
            temp_paths.append(temp_path)

        with open(out_path, "wb") as dest:
            for temp_path in temp_paths:
                with open(temp_path, "rb") as src:
                    shutil.copyfileobj(src, dest)

        return out_path
    finally:
        for temp_path in temp_paths:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def main():
    args = parse_args()
    cookie_header = None
    if args.cookie_file:
        with open(args.cookie_file, "r", encoding="utf-8") as f:
            cookie_header = f.read().strip()
    elif args.cookie:
        cookie_header = args.cookie.strip()

    # prepare extra headers from --header and --referer
    extra_headers = {}
    if args.header:
        for h in args.header:
            if ":" in h:
                name, val = h.split(":", 1)
                extra_headers[name.strip()] = val.strip()
    if args.referer:
        extra_headers["Referer"] = args.referer

    session = build_session(cookie_header, extra_headers=extra_headers)

    # validate URL has a scheme (helps catch placeholder input like "<PAGE_URL>")
    parsed_input = urlparse(args.url)
    if not parsed_input.scheme or parsed_input.scheme.lower() not in ("http", "https"):
        print(f"Invalid URL: {args.url!r}. Please provide a full URL including scheme (https://...).")
        print("Example: python download_talentsprint.py https://example.com/lesson --cookie \"sessionid=...\"")
        return 2

    # If the provided URL is a direct media URL, try fetching it with the authenticated session.
    is_direct_media = re.search(r"\.m3u8($|\?)|\.mp4($|\?)", args.url, flags=re.IGNORECASE)
    if is_direct_media:
        print(f"Fetching media URL with authenticated session: {args.url}")
        try:
            resp = session.get(args.url, timeout=15)
        except requests.exceptions.RequestException as e:
            print(f"Failed to fetch {args.url!r}: {e}")
            return 3

        if resp.status_code == 401 or "login" in resp.url.lower():
            print("Media URL requires authentication. Provide cookies via --cookie or --cookie-file.")
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"HTTP error fetching {args.url!r}: {e}")
            if not args.referer:
                print("If this is a CDN URL, try passing the original page as --referer or run the script against the lesson page instead.")
            else:
                print("Check the Referer and cookies are correct for this resource.")
            return 4

        base_url = args.url
        candidates = [args.url]
    else:
        print(f"Fetching page: {args.url}")
        try:
            resp = session.get(args.url, timeout=15)
        except requests.exceptions.RequestException as e:
            print(f"Failed to fetch {args.url!r}: {e}")
            return 3

        if resp.status_code == 401 or "login" in resp.url.lower():
            print("Page requires authentication. Provide cookies via --cookie or --cookie-file.")
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"HTTP error fetching {args.url!r}: {e}")
            print("Check the URL is correct and authentication cookies (if required).")
            return 4

        soup = BeautifulSoup(resp.text, "html.parser")
        base_url = resp.url
        candidates = extract_from_soup(soup, base_url)
        # also scan raw text for direct links
        candidates += find_video_sources(resp.text, base_url)
        candidates = list(dict.fromkeys(candidates))

    if not candidates:
        print("No direct mp4/m3u8 sources found on the page. The site may use dynamic JS or DRM.")
        print("Try opening the browser devtools Network tab, filter by 'm3u8' or 'mp4' and paste the direct URL here.")
        return 1

    print("Found the following candidate sources:")
    for i, c in enumerate(candidates):
        print(f"[{i}] {c}")

    idx = args.select if args.select is not None else 0
    if idx < 0 or idx >= len(candidates):
        print("--select index out of range")
        return 1
    src = candidates[idx]
    os.makedirs(args.output_dir, exist_ok=True)
    parsed = urlparse(src)
    fname = os.path.basename(parsed.path)
    if not fname:
        fname = "video"
    base_name = os.path.splitext(fname)[0] or "video"
    out_path = os.path.join(args.output_dir, base_name)

    if src.lower().endswith(".m3u8"):
        ffmpeg_path = args.ffmpeg
        print(f"Downloading HLS stream to {out_path}")
        # Build optional headers for ffmpeg (send cookie and referer if present)
        headers = None
        if cookie_header or base_url:
            hdr_lines = []
            if cookie_header:
                hdr_lines.append(f"Cookie: {cookie_header}")
            if base_url:
                hdr_lines.append(f"Referer: {base_url}")
            headers = "\\r\\n".join(hdr_lines) + "\\r\\n"

        target_mp4 = out_path + ".mp4"
        fallback_output = out_path

        if not shutil.which(ffmpeg_path):
            print("ffmpeg not found on PATH. Falling back to Python-based HLS segment download.")
            try:
                final_output = download_hls_without_ffmpeg(session, src, fallback_output)
                print(f"Saved concatenated HLS segments to {final_output}")
            except Exception as e:
                print("Python HLS fallback failed:", e)
                if "encrypted" in str(e).lower() or "decryption key" in str(e).lower():
                    print("This HLS stream appears to be encrypted. You will need the playlist decryption key or a player/browser with proper DRM support.")
                return 1
        else:
            # First attempt: let ffmpeg download directly, passing headers/user-agent when available
            try:
                download_hls(ffmpeg_path, src, target_mp4, headers=headers, user_agent=USER_AGENT)
            except RuntimeError:
                print("ffmpeg direct download failed, attempting authenticated playlist fallback...")
                # Fallback: fetch m3u8 with authenticated session, rewrite segment URLs to absolute,
                # save local playlist and call ffmpeg on that local file.
                import tempfile
                local_playlist = None
                try:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".m3u8", dir=args.output_dir) as tf:
                        local_playlist = tf.name
                    ok = write_local_playlist(session, src, local_playlist)
                    if not ok:
                        print("Failed to fetch or parse remote m3u8 playlist with the authenticated session.")
                        return 1

                    # Call ffmpeg on the local playlist; still pass headers/user-agent if needed
                    try:
                        download_hls(ffmpeg_path, local_playlist, target_mp4, headers=headers, user_agent=USER_AGENT)
                    finally:
                        try:
                            os.remove(local_playlist)
                        except Exception:
                            pass
                except Exception as e:
                    print("Fallback failed:", e)
                    if local_playlist:
                        try:
                            os.remove(local_playlist)
                        except Exception:
                            pass
                    return 1
    else:
        print(f"Downloading file to {out_path}")
        download_file(session, src, out_path)

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
