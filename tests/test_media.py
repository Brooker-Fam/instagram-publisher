import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from maple_publisher.media import (
    CREAM,
    MAX_IMAGE_BYTES,
    MediaValidationError,
    _require_fast_start,
    sign_media_path,
    storage_path,
    store_upload,
    verify_media_signature,
)


def picture(size=(800, 1000), mode="RGB", color="red", fmt="PNG", **save_options):
    output = io.BytesIO()
    Image.new(mode, size, color).save(output, format=fmt, **save_options)
    return output.getvalue()


def test_image_conversion_uses_bytes_and_random_key_not_filename(tmp_path):
    info = store_upload(picture(), "../../dangerous.php", tmp_path, "image")
    assert info["mime_type"] == "image/jpeg"
    assert info["storage_key"].endswith(".jpg")
    assert "/" not in info["storage_key"]
    path = tmp_path / info["storage_key"]
    assert path.is_file()
    assert info["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert info["size_bytes"] == path.stat().st_size
    assert info["duration"] is None
    with Image.open(path) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.mode == "RGB"
        assert decoded.size == (800, 1000)


def test_alpha_flattens_to_cream_and_metadata_is_removed(tmp_path):
    exif = Image.Exif()
    exif[270] = "private metadata"
    data = picture(mode="RGBA", color=(255, 0, 0, 0), exif=exif)
    info = store_upload(data, "private.png", tmp_path, "image")
    with Image.open(tmp_path / info["storage_key"]) as decoded:
        assert not decoded.getexif()
        assert "icc_profile" not in decoded.info
        assert all(abs(a - b) <= 2 for a, b in zip(decoded.getpixel((0, 0)), CREAM))


def test_exif_orientation_applied_before_aspect_validation(tmp_path):
    exif = Image.Exif()
    exif[274] = 6
    data = picture(size=(1000, 800), fmt="JPEG", exif=exif)
    info = store_upload(data, "rotated.jpg", tmp_path, "image")
    assert (info["width"], info["height"]) == (800, 1000)


def test_large_images_are_downscaled_without_crop(tmp_path):
    info = store_upload(picture(size=(2000, 2500)), "large.png", tmp_path, "image")
    assert (info["width"], info["height"]) == (1440, 1800)


@pytest.mark.parametrize("data,kind", [
    (b"", "image"),
    (b"<svg>invalid</svg>", "image"),
    (picture(size=(200, 250)), "image"),
    (picture(size=(800, 1400)), "image"),
    (picture(size=(800, 800)), "story"),
    (picture(), "reel"),
    (picture(fmt="GIF"), "image"),
    (picture(), "unknown"),
])
def test_invalid_media_rejected_without_persisted_file(tmp_path, data, kind):
    with pytest.raises(MediaValidationError):
        store_upload(data, "claims-to-be.jpg", tmp_path, kind)
    assert list(tmp_path.iterdir()) == []


def test_rejects_large_input_before_decoding(tmp_path):
    with pytest.raises(MediaValidationError, match="8 MB"):
        store_upload(b"x" * (MAX_IMAGE_BYTES + 1), "big.jpg", tmp_path, "image")


def test_rejects_decompression_bomb_before_loading(tmp_path, monkeypatch):
    monkeypatch.setattr("maple_publisher.media.MAX_IMAGE_PIXELS", 100)
    with pytest.raises(MediaValidationError, match="25 million"):
        store_upload(picture(), "bomb.png", tmp_path, "image")


def test_story_accepts_generated_asset_rounding(tmp_path):
    info = store_upload(picture(size=(941, 1672)), "story.png", tmp_path, "stories")
    assert (info["width"], info["height"]) == (941, 1672)


def test_cover_accepts_portrait_image_and_rejects_video_or_feed_ratio(tmp_path):
    info = store_upload(picture(size=(941, 1672)), "cover.png", tmp_path, "cover")
    assert info["mime_type"] == "image/jpeg"
    assert (info["width"], info["height"]) == (941, 1672)
    with pytest.raises(MediaValidationError, match="9:16"):
        store_upload(picture(), "feed.png", tmp_path, "cover")
    with pytest.raises(MediaValidationError):
        store_upload(MP4, "cover.mp4", tmp_path, "cover")


def test_animated_webp_rejected(tmp_path):
    output = io.BytesIO()
    Image.new("RGB", (800, 1000), "red").save(
        output, format="WEBP", save_all=True,
        append_images=[Image.new("RGB", (800, 1000), "blue")], duration=100,
    )
    with pytest.raises(MediaValidationError, match="Animated"):
        store_upload(output.getvalue(), "animated.webp", tmp_path, "image")


def video_probe(**overrides):
    video = {
        "codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
        "width": 1080, "height": 1920, "avg_frame_rate": "30000/1001",
    }
    video.update(overrides)
    return {
        "streams": [video, {"codec_type": "audio", "codec_name": "aac", "channels": 2, "sample_rate": "48000"}],
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "12.5"},
    }


def mp4_box(kind, payload=b"", *, extended=False, to_eof=False):
    if extended:
        return (1).to_bytes(4, "big") + kind + (16 + len(payload)).to_bytes(8, "big") + payload
    if to_eof:
        return bytes(4) + kind + payload
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


FTYP = mp4_box(b"ftyp", b"isom\x00\x00\x00\x00")
MOOV = mp4_box(b"moov", b"mocked metadata")
MDAT = mp4_box(b"mdat", b"mocked media")
MP4 = FTYP + MOOV + MDAT


@pytest.mark.parametrize("data", [
    MP4,
    FTYP + mp4_box(b"free", b"padding") + MOOV + MDAT,
    FTYP + mp4_box(b"moov", b"metadata", extended=True) + mp4_box(b"mdat", b"media", extended=True),
    FTYP + MOOV + mp4_box(b"mdat", b"media", to_eof=True),
])
def test_fast_start_parser_accepts_regular_extended_and_eof_boxes(data):
    _require_fast_start(data)


@pytest.mark.parametrize("data", [
    FTYP + MDAT + MOOV,
    FTYP + mp4_box(b"mdat", b"moov text inside media is not a movie box"),
    FTYP + MOOV,
    FTYP + MOOV + MOOV + MDAT,
    FTYP + MOOV + mp4_box(b"moof", b"fragment") + MDAT,
    MP4 + b"bad",
    FTYP + (1).to_bytes(4, "big") + b"moov",
    FTYP + (1).to_bytes(4, "big") + b"moov" + (7).to_bytes(8, "big"),
    FTYP + (4).to_bytes(4, "big") + b"moov",
    FTYP + (999999).to_bytes(4, "big") + b"moov",
    FTYP + mp4_box(b"free") * 4096 + MOOV + MDAT,
])
def test_fast_start_parser_rejects_bad_order_and_malformed_boxes_before_probe(tmp_path, monkeypatch, data):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid atom structure must be rejected before spawning ffprobe")
    monkeypatch.setattr("maple_publisher.media.subprocess.run", forbidden)
    with pytest.raises(MediaValidationError):
        store_upload(data, "reel.mp4", tmp_path, "reel")
    assert list(tmp_path.iterdir()) == []


def mock_probe(monkeypatch, probe):
    def run(args, **kwargs):
        assert args[0] == "ffprobe"
        assert kwargs["timeout"] == 20
        assert "shell" not in kwargs
        assert args[args.index("-protocol_whitelist") + 1] == "file"
        assert Path(args[-1]).read_bytes() == MP4
        kwargs["stdout"].write(json.dumps(probe).encode())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("maple_publisher.media.subprocess.run", run)


def test_valid_mp4_persisted_only_after_inspection(tmp_path, monkeypatch):
    mock_probe(monkeypatch, video_probe())
    info = store_upload(MP4, "../video;rm.jpg", tmp_path, "reel")
    assert info["mime_type"] == "video/mp4"
    assert info["duration"] == 12.5
    assert (tmp_path / info["storage_key"]).read_bytes() == MP4


@pytest.mark.parametrize("changes", [
    {"codec_name": "hevc"},
    {"pix_fmt": "yuv420p10le"},
    {"width": 1920, "height": 1080},
    {"width": 0},
    {"avg_frame_rate": "120/1"},
    {"avg_frame_rate": "0/0"},
    {"tags": {"rotate": "90"}},
    {"side_data_list": [{"rotation": -90}]},
])
def test_invalid_video_properties_rejected(tmp_path, monkeypatch, changes):
    mock_probe(monkeypatch, video_probe(**changes))
    with pytest.raises(MediaValidationError):
        store_upload(MP4, "video.mp4", tmp_path, "reel")
    assert list(tmp_path.iterdir()) == []


def test_story_video_duration_limit(tmp_path, monkeypatch):
    probe = video_probe()
    probe["format"]["duration"] = "60.1"
    mock_probe(monkeypatch, probe)
    with pytest.raises(MediaValidationError, match="60 seconds"):
        store_upload(MP4, "story.mp4", tmp_path, "story")


def test_infinite_duration_rejected(tmp_path, monkeypatch):
    probe = video_probe()
    probe["format"]["duration"] = "inf"
    mock_probe(monkeypatch, probe)
    with pytest.raises(MediaValidationError, match="duration"):
        store_upload(MP4, "reel.mp4", tmp_path, "reel")


def test_missing_ffprobe_is_actionable(tmp_path, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError
    monkeypatch.setattr("maple_publisher.media.subprocess.run", missing)
    with pytest.raises(MediaValidationError, match="install ffmpeg"):
        store_upload(MP4, "video.mp4", tmp_path, "reel")


def test_ffprobe_timeout_is_bounded(tmp_path, monkeypatch):
    import subprocess
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("ffprobe", 20)
    monkeypatch.setattr("maple_publisher.media.subprocess.run", timeout)
    with pytest.raises(MediaValidationError, match="time limit"):
        store_upload(MP4, "video.mp4", tmp_path, "reel")


def test_signatures_expire_and_bind_asset_and_timestamp():
    secret = "s" * 32
    signature = sign_media_path("asset-123", 2000, secret)
    assert verify_media_signature("asset-123", 2000, signature, secret, now=1999)
    assert not verify_media_signature("asset-123", 2000, signature, secret, now=2000)
    assert not verify_media_signature("other-asset", 2000, signature, secret, now=1999)
    assert not verify_media_signature("asset-123", 3000, signature, secret, now=1999)
    assert not verify_media_signature("asset-123", 2000, signature, "x" * 32, now=1999)
    assert not verify_media_signature("asset-123", 2000, signature.upper(), secret, now=1999)
    assert not verify_media_signature("asset-123", 2000, signature, secret, now=float("nan"))


@pytest.mark.parametrize("asset_id", ["../asset", "asset\n2000", "", "x" * 129])
def test_invalid_signature_identifier_rejected(asset_id):
    with pytest.raises(ValueError):
        sign_media_path(asset_id, 2000, "s" * 32)


def test_signing_rejects_weak_secret_and_invalid_timestamp():
    with pytest.raises(ValueError):
        sign_media_path("asset", 2000, "short")
    with pytest.raises(ValueError):
        sign_media_path("asset", True, "s" * 32)
    assert not verify_media_signature("asset", "2000", "x" * 64, "s" * 32)


def test_storage_key_prevents_traversal_and_symlink_escape(tmp_path):
    key = "a" * 32 + ".jpg"
    assert storage_path(tmp_path, key) == tmp_path / key
    with pytest.raises(MediaValidationError):
        storage_path(tmp_path, "../private.env")
    with pytest.raises(MediaValidationError):
        storage_path(tmp_path, "arbitrary.jpg")
    (tmp_path / key).symlink_to(tmp_path.parent / "private.env")
    with pytest.raises(MediaValidationError):
        storage_path(tmp_path, key)
