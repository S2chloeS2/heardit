"""Fetch audio from a URL and prepare it for transcription.

Used by the paste-a-link mode. Whisper caps uploads at 25 MB, so anything long
is split into chunks that are transcribed in order and stitched back together.
"""

import ipaddress
import os
import shutil
import socket
import subprocess
import tempfile
from urllib.parse import urlparse

import yt_dlp

# Comfortably inside Whisper's 25 MB limit at the bitrate we encode to.
CHUNK_SECONDS = int(os.getenv("CHUNK_SECONDS", "600"))  # 10 minutes
MAX_DURATION = int(os.getenv("MAX_DURATION", "14400"))  # 4 hours


class MediaError(Exception):
    """Something went wrong fetching or splitting the audio."""


def assert_public_url(url):
    """Refuse URLs that resolve to the machine itself or a private network.

    Without this the import feature is a server-side request forgery hole: a
    link to 169.254.169.254 would hand back cloud instance credentials, and one
    to an internal host would let a stranger probe the private network.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise MediaError("http 또는 https 주소만 사용할 수 있습니다.")
    if not parsed.hostname:
        raise MediaError("주소에서 호스트를 찾을 수 없습니다.")

    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 0, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise MediaError(f"주소를 찾을 수 없습니다: {parsed.hostname}") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise MediaError("내부 네트워크 주소는 가져올 수 없습니다.")


def resolve(url):
    """Turn a pasted link into what yt-dlp should actually fetch.

    TED's own site breaks yt-dlp's extractor, but every TED talk is also on
    TED's YouTube channel — so a ted.com link becomes a YouTube search for the
    same talk. The caller shows the matched title so a wrong match is visible.
    """
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("ted.com"):
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
        words = " ".join(w for w in slug.replace("_", " ").replace("-", " ").split() if w)
        if words:
            return f"ytsearch1:{words} TED", True
    return url, False


def duration_of(path):
    """Length of a local audio/video file in seconds, via ffprobe. 0 if unknown."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


def _ydl_common():
    """Options every yt-dlp call shares.

    YouTube blocks most datacenter IPs (Render, AWS) with HTTP 403. Two ways
    around it, both optional and set by env: YTDLP_PROXY (a residential or
    rotating proxy URL) and YTDLP_COOKIES (path to a cookies.txt exported
    from a signed-in browser)."""
    opts = {"quiet": True, "no_warnings": True}
    proxy = os.getenv("YTDLP_PROXY")
    if proxy:
        opts["proxy"] = proxy
    cookies = os.getenv("YTDLP_COOKIES")
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies
    return opts


def _friendly(exc):
    text = str(exc)
    if "403" in text or "Sign in to confirm" in text or "bot" in text.lower():
        return ("YouTube is refusing requests from this server. Download the video's audio "
                "and upload the file instead, or try a TED link.")
    return text


def probe(url):
    """Read a URL's metadata without downloading it."""
    target, via_search = resolve(url)
    if not via_search:
        assert_public_url(url)
    opts = {**_ydl_common(), "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception as exc:
        if via_search:
            raise MediaError("TED 강연을 유튜브에서 찾지 못했습니다. 유튜브 TED 채널의 링크를 직접 붙여넣어 주세요.") from exc
        raise MediaError(f"Could not read that link: {_friendly(exc)}") from exc

    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries", []) if e]
        if not entries:
            raise MediaError("That playlist is empty.")
        info = entries[0]

    duration = info.get("duration") or 0
    if duration > MAX_DURATION:
        raise MediaError(
            f"That is {duration // 3600}h long. The limit is {MAX_DURATION // 3600}h."
        )

    return {
        "title": info.get("title") or "Untitled",
        "duration": duration,
        "uploader": info.get("uploader") or "",
        # For a TED link this is the YouTube URL we matched — the caller should
        # download from here, not from the original address.
        "webpage_url": info.get("webpage_url") or url,
        "via_youtube": via_search,
    }


def download_audio(url, workdir):
    """Download a URL's audio track as mp3. Returns the file path."""
    # Re-check: DNS could have changed between probe and download.
    assert_public_url(url)
    template = os.path.join(workdir, "audio.%(ext)s")
    opts = {
        **_ydl_common(),
        "format": "bestaudio/best",
        "outtmpl": template,
        "noplaylist": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }
        ],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        raise MediaError(f"Could not download the audio: {_friendly(exc)}") from exc

    path = os.path.join(workdir, "audio.mp3")
    if not os.path.exists(path):
        raise MediaError("The download finished but produced no audio file.")
    return path


def split(path, workdir, seconds=CHUNK_SECONDS):
    """Split audio into fixed-length mp3 chunks. Returns paths in order."""
    if not shutil.which("ffmpeg"):
        raise MediaError("ffmpeg is not installed. Run: brew install ffmpeg")

    pattern = os.path.join(workdir, "chunk_%03d.mp3")
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
            "-f", "segment", "-segment_time", str(seconds),
            "-c:a", "libmp3lame", "-b:a", "64k", "-ac", "1", "-ar", "16000",
            pattern,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MediaError(f"Could not split the audio: {result.stderr[:300]}")

    chunks = sorted(
        os.path.join(workdir, f)
        for f in os.listdir(workdir)
        if f.startswith("chunk_") and f.endswith(".mp3")
    )
    if not chunks:
        raise MediaError("Splitting produced no chunks.")
    return chunks


def workspace():
    """A temp directory the caller is responsible for cleaning up."""
    return tempfile.mkdtemp(prefix="heardit_")
