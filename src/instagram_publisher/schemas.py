from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PostDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str | None = None
    kind: Literal["IMAGE", "CAROUSEL", "REEL", "STORY"] = "IMAGE"
    asset_ids: list[str] = Field(min_length=1, max_length=10)
    cover_asset_id: str | None = None
    caption: str = Field(default="", max_length=2200)
    alt_text: str = Field(default="", max_length=1000)
    share_to_feed: bool = True
    scheduled_at: datetime | None = None

    @field_validator("scheduled_at")
    @classmethod
    def require_timezone(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Include a timezone, for example 2026-10-30T09:00:00Z")
        return value

    @model_validator(mode="after")
    def validate_shape(self):
        if len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError("Each carousel asset must be distinct")
        if self.kind == "CAROUSEL" and len(self.asset_ids) < 2:
            raise ValueError("An image carousel needs 2 to 10 images")
        if self.kind != "CAROUSEL" and len(self.asset_ids) != 1:
            raise ValueError("This format needs exactly one asset")
        if self.cover_asset_id and self.kind != "REEL":
            raise ValueError("Only Reels have a separate cover")
        if self.alt_text and self.kind != "IMAGE":
            raise ValueError("This release supports alt text on single-image posts only")
        if self.kind == "STORY" and self.caption:
            raise ValueError("Story text must be in the image/video; API Story captions are not supported")
        return self


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)


class DraftUpdate(PostDraft):
    revision: int = Field(ge=1)
