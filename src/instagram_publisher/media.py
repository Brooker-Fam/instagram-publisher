"""Bounded local media validation and signed delivery URLs.

This application deliberately accepts a conservative subset of Instagram media:
JPEG/PNG/WebP images, and MP4 with H.264 video and optional AAC audio. It never
downloads an arbitrary URL. Source filenames are labels, never filesystem paths.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import math
import re
import subprocess
import tempfile
import time
import uuid
import warnings
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_BYTES = 8_000_000
MAX_VIDEO_BYTES = 100_000_000
MAX_IMAGE_PIXELS = 25_000_000
MAX_IMAGE_WIDTH = 1440
MIN_IMAGE_WIDTH = 320
CREAM = (255, 245, 231)
_STORAGE_KEY = re.compile(r"\A[0-9a-f]{32}\.(?:jpg|mp4)\Z")
_ASSET_ID = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")
_SIGNATURE = re.compile(r"\A[0-9a-f]{64}\Z")
_IMAGE_KINDS = {"image", "feed", "carousel"}
_VIDEO_KINDS = {"video", "reel", "reels"}
_STORY_KINDS = {"story", "stories"}
_COVER_KINDS = {"cover"}


class MediaValidationError(ValueError):
    """A safe, actionable validation message that may be shown to the caller."""


def _portrait_story(width: int, height: int) -> bool:
    # Generated and camera images commonly differ by a pixel after export.
    return abs((width / height) / (9 / 16) - 1) <= 0.01


def _normalize_image(data: bytes, *, story: bool) -> tuple[bytes, int, int]:
    if len(data) > MAX_IMAGE_BYTES:
        raise MediaValidationError("Images must be no larger than 8 MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP"}:
                    raise MediaValidationError("Use a JPEG, PNG, or WebP image.")
                if getattr(source, "is_animated", False):
                    raise MediaValidationError(
                        "Animated images are not supported; upload a still image or MP4."
                    )
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise MediaValidationError("Images must contain no more than 25 million pixels.")
                # Transpose before dimensions/aspect validation. Loading is bounded
                # by the explicit pixel check above, independent of Pillow defaults.
                oriented = ImageOps.exif_transpose(source)
                width, height = oriented.size
                if width < MIN_IMAGE_WIDTH:
                    raise MediaValidationError("Images must be at least 320 pixels wide.")
                if story:
                    if not _portrait_story(width, height):
                        raise MediaValidationError("Use a vertical 9:16 image for a Story or Reel cover.")
                elif not 0.8 <= width / height <= 1.91:
                    raise MediaValidationError(
                        "Feed images must have an aspect ratio between 4:5 and 1.91:1."
                    )

                # Construct a fresh image to strip EXIF, GPS, comments, and ICC
                # metadata. Flatten transparency to a neutral cream background.
                rgba = oriented.convert("RGBA")
                clean = Image.new("RGB", rgba.size, CREAM)
                clean.paste(rgba, mask=rgba.getchannel("A"))
                if width > MAX_IMAGE_WIDTH:
                    height = round(height * MAX_IMAGE_WIDTH / width)
                    width = MAX_IMAGE_WIDTH
                    clean = clean.resize((width, height), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                clean.save(output, format="JPEG", quality=92, optimize=True)
                normalized = output.getvalue()
                if len(normalized) > MAX_IMAGE_BYTES:
                    raise MediaValidationError("Converted JPEG exceeds 8 MB; upload a smaller image.")
                return normalized, width, height
    except MediaValidationError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise MediaValidationError("The image is invalid or too large to decode safely.") from exc


def _looks_like_mp4(data: bytes) -> bool:
    # Inspect the actual ISO base media header, not the untrusted extension.
    return len(data) >= 16 and data[4:8] == b"ftyp" and data[8:12] != b"qt  "


def _require_fast_start(data: bytes) -> None:
    """Inspect bounded top-level ISO-BMFF boxes without decoding their payloads.

    Instagram fetches a video over HTTP and requires its movie metadata before
    the media data. File extension, ffprobe success, and a ``moov`` byte pattern
    anywhere in the payload cannot establish that ordering.
    """
    offset, boxes = 0, 0
    seen_movie, seen_media = False, False
    while offset < len(data):
        boxes += 1
        if boxes > 4096:
            raise MediaValidationError("The MP4 contains too many top-level boxes; export a standard MP4.")
        if len(data) - offset < 8:
            raise MediaValidationError("The MP4 contains a truncated box header.")
        size = int.from_bytes(data[offset : offset + 4], "big")
        box_type = data[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if len(data) - offset < 16:
                raise MediaValidationError("The MP4 contains a truncated extended box header.")
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header_size = 16
        elif size == 0:
            size = len(data) - offset
        if size < header_size or size > len(data) - offset:
            raise MediaValidationError("The MP4 contains an invalid box length.")
        if box_type == b"moov":
            if seen_movie:
                raise MediaValidationError(
                    "The MP4 contains duplicate movie metadata; export a standard MP4."
                )
            seen_movie = True
        elif box_type == b"mdat":
            if not seen_movie:
                raise MediaValidationError(
                    "Export a fast-start MP4 with movie metadata before media data (ffmpeg -movflags +faststart)."
                )
            seen_media = True
        elif box_type == b"moof":
            raise MediaValidationError("Export a standard fast-start MP4 rather than a fragmented MP4.")
        offset += size
    if not seen_movie or not seen_media:
        raise MediaValidationError("The MP4 is missing movie metadata or media data.")


def _number(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaValidationError(f"Video has no valid {field}.") from exc
    if not math.isfinite(result):
        raise MediaValidationError(f"Video has no valid {field}.")
    return result


def _frame_rate(stream: dict[str, Any]) -> float:
    raw = stream.get("avg_frame_rate", stream.get("r_frame_rate", ""))
    try:
        numerator, denominator = str(raw).split("/", maxsplit=1)
        return _number(numerator, "frame rate") / _number(denominator, "frame rate")
    except (ValueError, ZeroDivisionError) as exc:
        raise MediaValidationError("Video has no valid frame rate.") from exc


def _probe_video(path: Path, *, story: bool) -> tuple[int, int, float]:
    try:
        # Stream probe output to a temporary file, then read a bounded amount.
        # Malformed files with many tracks must not create unbounded Python RAM
        # allocations, and raw parser errors may contain sensitive file metadata.
        with tempfile.TemporaryFile() as probe_output:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=probe_output,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            )
            probe_output.seek(0)
            raw_output = probe_output.read(1_000_001)
    except FileNotFoundError as exc:
        raise MediaValidationError(
            "Video validation is unavailable: install ffmpeg/ffprobe on the server."
        ) from exc
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise MediaValidationError("The video could not be validated within the time limit.") from exc
    if result.returncode != 0 or len(raw_output) > 1_000_000:
        raise MediaValidationError("The MP4 file is invalid or could not be inspected.")
    try:
        probe = json.loads(raw_output)
        streams = probe["streams"]
        format_info = probe["format"]
        if not isinstance(streams, list) or not isinstance(format_info, dict):
            raise TypeError
        if "mp4" not in str(format_info.get("format_name", "")).split(","):
            raise MediaValidationError("Upload an MP4 video.")
        video_streams = [s for s in streams if s.get("codec_type") == "video"]
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        if len(video_streams) != 1 or len(audio_streams) > 1:
            raise MediaValidationError("Use one video track and at most one audio track.")
        if any(s.get("codec_type") not in {"video", "audio"} for s in streams):
            raise MediaValidationError("Export an MP4 without subtitle, data, or attachment tracks.")
        video = video_streams[0]
        if video.get("codec_name") != "h264":
            raise MediaValidationError("Export the video with the H.264 codec.")
        if video.get("pix_fmt") not in {"yuv420p", "yuvj420p"}:
            raise MediaValidationError("Export the video with 8-bit 4:2:0 color (yuv420p).")
        if audio_streams and audio_streams[0].get("codec_name") != "aac":
            raise MediaValidationError("Export the audio with the AAC codec.")
        if audio_streams:
            audio = audio_streams[0]
            if int(audio.get("channels", 0)) not in {1, 2}:
                raise MediaValidationError("Use mono or stereo audio.")
            if _number(audio.get("sample_rate"), "audio sample rate") > 48_000:
                raise MediaValidationError("Use an audio sample rate of 48 kHz or lower.")
        width, height = int(video["width"]), int(video["height"])
        if not 320 <= width <= 1920 or not 320 <= height <= 3840:
            raise MediaValidationError("Video must be 320–1920 pixels wide and 320–3840 pixels tall.")
        # A rotated track would change the displayed aspect without altering its
        # encoded dimensions. Require an explicit portrait export for predictability.
        rotations = [video.get("tags", {}).get("rotate", 0)]
        rotations.extend(s.get("rotation", 0) for s in video.get("side_data_list", []))
        if any(_number(rotation, "rotation") % 360 != 0 for rotation in rotations):
            raise MediaValidationError("Export portrait video without rotation metadata.")
        if not _portrait_story(width, height):
            raise MediaValidationError("Use a vertical 9:16 video for Reels and Stories.")
        fps = _frame_rate(video)
        if not 23 <= fps <= 60:
            raise MediaValidationError("Video frame rate must be between 23 and 60 fps.")
        duration = _number(format_info.get("duration"), "duration")
        max_duration = 60 if story else 900
        if not 3 <= duration <= max_duration:
            raise MediaValidationError(f"Video duration must be between 3 and {max_duration} seconds.")
        return width, height, duration
    except MediaValidationError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        raise MediaValidationError("The video has incomplete or invalid metadata.") from exc


def store_upload(data: bytes, filename: str, directory: Path, kind: str) -> dict[str, Any]:
    """Validate bytes and store under a generated name, returning persisted metadata.

    ``kind`` is image/feed/carousel, reel/reels/video, story/stories, or cover.
    Story uploads may be either an image or MP4; covers are 9:16 still images.
    Videos must be fast-start MP4 and are validated, not transcoded.
    ``directory`` must be a server-controlled storage directory. ``filename`` is
    intentionally never used for format detection or path construction.
    """
    del filename
    if not isinstance(kind, str):
        raise MediaValidationError("Unknown media kind; use image, reel, story, or cover.")
    kind = kind.lower()
    if kind not in _IMAGE_KINDS | _VIDEO_KINDS | _STORY_KINDS | _COVER_KINDS:
        raise MediaValidationError("Unknown media kind; use image, reel, story, or cover.")
    if not isinstance(data, bytes) or not data:
        raise MediaValidationError("Upload a non-empty media file.")
    if len(data) > MAX_VIDEO_BYTES:
        raise MediaValidationError("Uploads must be no larger than 100 MB.")
    directory = Path(directory)
    is_video = kind in _VIDEO_KINDS or (kind in _STORY_KINDS and _looks_like_mp4(data))
    duration = None
    if is_video:
        if not _looks_like_mp4(data):
            raise MediaValidationError("Upload an MP4 video.")
        _require_fast_start(data)
        # Keep rejected uploads outside the public media directory and always
        # remove the temporary source, including on ffprobe errors/timeouts.
        with tempfile.TemporaryDirectory(prefix="instagram-video-") as temp_dir:
            source = Path(temp_dir) / "source.mp4"
            source.write_bytes(data)
            width, height, duration = _probe_video(source, story=kind in _STORY_KINDS)
        content, extension, mime_type = data, "mp4", "video/mp4"
    else:
        content, width, height = _normalize_image(data, story=kind in _STORY_KINDS | _COVER_KINDS)
        extension, mime_type = "jpg", "image/jpeg"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    storage_key = f"{uuid.uuid4().hex}.{extension}"
    path = directory / storage_key
    created = False
    try:
        with path.open("xb") as output:
            created = True
            output.write(content)
        path.chmod(0o600)
    except OSError:
        if created:
            path.unlink(missing_ok=True)
        raise
    return {
        "storage_key": storage_key,
        "mime_type": mime_type,
        "size_bytes": len(content),
        "width": width,
        "height": height,
        "duration": duration,
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def storage_path(directory: Path, storage_key: str) -> Path:
    """Resolve only generated file keys beneath the configured media directory."""
    if not isinstance(storage_key, str) or not _STORAGE_KEY.fullmatch(storage_key):
        raise MediaValidationError("Invalid stored media key.")
    base = Path(directory).resolve()
    result = (base / storage_key).resolve()
    if result.parent != base:
        raise MediaValidationError("Invalid stored media path.")
    return result


def sign_media_path(asset_id: str, expires: int, secret: str) -> str:
    """Sign the immutable asset identifier and expiration, never a caller URL."""
    if not isinstance(asset_id, str) or not _ASSET_ID.fullmatch(asset_id):
        raise ValueError("Invalid asset identifier.")
    if type(expires) is not int or expires <= 0:
        raise ValueError("Expiration must be a positive Unix timestamp.")
    if not isinstance(secret, str) or len(secret.encode()) < 32:
        raise ValueError("Media signing secret must contain at least 32 bytes.")
    payload = f"instagram-media-v1\n{asset_id}\n{expires}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def verify_media_signature(
    asset_id: str,
    expires: int,
    signature: str,
    secret: str,
    now: float | None = None,
) -> bool:
    """Verify a signed delivery request; expiration is exclusive."""
    try:
        if not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature):
            return False
        current = time.time() if now is None else float(now)
        if not math.isfinite(current) or type(expires) is not int or expires <= current:
            return False
        expected = sign_media_path(asset_id, expires, secret)
        return hmac.compare_digest(expected, signature)
    except (TypeError, ValueError, OverflowError):
        return False
