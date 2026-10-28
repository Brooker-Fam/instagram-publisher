from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select

from instagram_publisher.api_dependencies import require_admin
from instagram_publisher.db import (
    Asset,
)
from instagram_publisher.media import (
    MAX_VIDEO_BYTES,
    MediaValidationError,
    storage_path,
    store_upload,
    verify_media_signature,
)
from instagram_publisher.serializers import asset_dict

router = APIRouter()


@router.post("/api/assets", status_code=201, dependencies=[Depends(require_admin)], tags=["Media"])
def upload(request: Request, file: Annotated[UploadFile, File()], kind: Annotated[str, Form()] = "image"):
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    chunks, size = [], 0
    try:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_VIDEO_BYTES:
                raise HTTPException(413, "File exceeds the 100 MB upload limit")
            chunks.append(chunk)
        result = store_upload(b"".join(chunks), file.filename or "upload", settings.media_root, kind)
    except MediaValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        file.file.close()
    try:
        with session_factory.begin() as session:
            asset = Asset(**result)
            session.add(asset)
            session.flush()
            return asset_dict(asset)
    except Exception:
        storage_path(settings.media_root, result["storage_key"]).unlink(missing_ok=True)
        raise


@router.get("/api/assets", dependencies=[Depends(require_admin)], tags=["Media"])
def assets(
    request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 50, offset: Annotated[int, Query(ge=0)] = 0
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        return [
            asset_dict(a)
            for a in session.scalars(
                select(Asset).order_by(Asset.created_at.desc()).limit(limit).offset(offset)
            )
        ]


@router.get("/media/{asset_id}", include_in_schema=False)
def serve_media(request: Request, asset_id: str, expires: int, signature: str):
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    if not verify_media_signature(
        asset_id, expires, signature, settings.media_signing_key.get_secret_value()
    ):
        raise HTTPException(403, "Invalid or expired media link")
    with session_factory() as session:
        asset = session.get(Asset, asset_id)
        if not asset:
            raise HTTPException(404, "Media not found")
        path = storage_path(settings.media_root, asset.storage_key)
        if not path.is_file():
            raise HTTPException(404, "Media not found")
        return FileResponse(path, media_type=asset.mime_type)
