from datetime import datetime, timezone


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def asset_dict(asset):
    return {
        key: getattr(asset, key)
        for key in ("id", "mime_type", "size_bytes", "width", "height", "duration", "sha256")
    } | {"created_at": iso(asset.created_at)}


def post_dict(post):
    values = {
        key: getattr(post, key)
        for key in (
            "id",
            "account_id",
            "kind",
            "asset_ids",
            "cover_asset_id",
            "caption",
            "alt_text",
            "share_to_feed",
            "status",
            "revision",
            "approved_revision",
            "approved_mode",
            "container_id",
            "published_media_id",
            "permalink",
            "error",
        )
    }
    values.update(
        {
            key: iso(getattr(post, key))
            for key in (
                "scheduled_at",
                "approved_at",
                "published_at",
                "created_at",
                "updated_at",
                "publish_requested_at",
            )
        }
    )
    return values


def draft_values(draft):
    values = draft.model_dump(exclude={"revision"})
    if values["scheduled_at"] is not None:
        values["scheduled_at"] = values["scheduled_at"].timestamp()
    return values
