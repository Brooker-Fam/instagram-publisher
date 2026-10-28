from fastapi import HTTPException

from instagram_publisher.db import Account, Asset


def validate_assets(session, draft):
    for asset_id in draft.asset_ids:
        asset = session.get(Asset, asset_id)
        if asset is None:
            raise HTTPException(422, "An asset does not exist")
        is_image = asset.mime_type == "image/jpeg"
        if draft.kind in {"IMAGE", "CAROUSEL"}:
            if not is_image or not 0.8 <= asset.width / asset.height <= 1.91:
                raise HTTPException(422, "Feed posts need JPEG images with a 4:5 to 1.91:1 aspect ratio")
        elif draft.kind == "REEL":
            if asset.mime_type != "video/mp4" or not asset.duration or not 3 <= asset.duration <= 900:
                raise HTTPException(422, "A Reel needs an MP4 video lasting 3 seconds to 15 minutes")
        elif draft.kind == "STORY":
            if abs((asset.width / asset.height) / (9 / 16) - 1) > 0.01:
                raise HTTPException(422, "Stories in this service require 9:16 media")
            if not is_image and (not asset.duration or not 3 <= asset.duration <= 60):
                raise HTTPException(422, "Story videos must last 3 to 60 seconds")
    if draft.cover_asset_id:
        cover = session.get(Asset, draft.cover_asset_id)
        if cover is None or cover.mime_type != "image/jpeg":
            raise HTTPException(422, "The Reel cover must be an uploaded JPEG image")
    if draft.account_id and session.get(Account, draft.account_id) is None:
        raise HTTPException(422, "The selected Instagram account does not exist")
