from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum
from typing import Any, Generic, Literal, TypeVar
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, HttpUrl, model_validator, field_validator


T = TypeVar("T")


class ErrorDetail(BaseModel):
    code: str
    message: str
    field: str | None = None
    context: dict[str, Any] | None = None


class Error(BaseModel):
    error: ErrorDetail
    request_id: str | None = None


class Page(BaseModel, Generic[T]):
    items: list[T]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)
    total: int = Field(ge=0)


class JobKind(StrEnum):
    CHECKIN = "checkin"
    PRODUCT_SYNC = "product_sync"
    BOOKMARK_IMPORT = "bookmark_import"
    LOGIN_SESSION = "login_session"
    ADAPTER_TEST = "adapter_test"


class JobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    EFFECT_UNKNOWN = "effect_unknown"
    CANCELLED = "cancelled"


class TaskRef(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)
    job_id: UUID = Field(alias="jobId")
    kind: JobKind
    status: JobStatus
    deduplicated: bool = False
    href: str


class SiteBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2048, validation_alias=AliasChoices("url", "origin"))
    folder_id: UUID | None = None
    kind: str = Field(default="other", max_length=64)
    capabilities: list[str] = Field(default_factory=lambda: ["bookmark"])
    description: str = Field(default="", max_length=4000)
    note: str = Field(default="", max_length=10000)
    icon: str = Field(default="", max_length=64)
    status: str = Field(default="active", max_length=32)
    pinned: bool = False

    @field_validator("url")
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        value = value.strip()
        # HttpUrl validates syntax while intentionally allowing private/loopback targets.
        HttpUrl(value)
        if urlsplit(value).username is not None or urlsplit(value).password is not None:
            raise ValueError("URL userinfo is not allowed")
        return value


class SiteCreate(SiteBase):
    tag_ids: list[UUID] = Field(default_factory=list)


class SiteUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=2048, validation_alias=AliasChoices("url", "origin"))
    folder_id: UUID | None = None
    kind: str | None = Field(default=None, max_length=64)
    capabilities: list[str] | None = None
    description: str | None = Field(default=None, max_length=4000)
    note: str | None = Field(default=None, max_length=10000)
    icon: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=32)
    pinned: bool | None = None
    tag_ids: list[UUID] | None = None
    adapter: str | None = Field(default=None, max_length=100)
    config: dict[str, Any] | None = None
    clear_secrets: list[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def validate_http_url(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            HttpUrl(value)
            if urlsplit(value).username is not None or urlsplit(value).password is not None:
                raise ValueError("URL userinfo is not allowed")
        return value


class SiteOut(SiteBase):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    favicon_path: str | None = None
    tags: list[dict[str, Any]] = Field(default_factory=list)
    checkin_account_count: int = 0
    product_count: int = 0
    adapter: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    configured: bool = False
    configured_secret_fields: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class CheckinScheduleOverride(BaseModel):
    mode: Literal["fixed_time", "custom_window"]
    time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}(?::\d{2})?$")
    start: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}(?::\d{2})?$")
    end: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}(?::\d{2})?$")
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"

    @model_validator(mode="after")
    def validate_shape(self) -> "CheckinScheduleOverride":
        if self.mode == "fixed_time" and not self.time:
            raise ValueError("fixed_time requires time")
        if self.time is not None:
            try:
                time.fromisoformat(self.time)
            except ValueError:
                raise ValueError("time must be a valid clock time") from None
        if self.mode == "custom_window":
            if not self.start or not self.end:
                raise ValueError("custom_window requires start and end")
            try:
                start = time.fromisoformat(self.start)
                end = time.fromisoformat(self.end)
            except ValueError:
                raise ValueError("custom_window times must be valid clock times") from None
            if start >= end:
                raise ValueError("custom_window end must be after start")
        return self


class CheckinAccountCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str = Field(min_length=1, max_length=200, validation_alias=AliasChoices("name", "label"))
    adapter: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    enabled: bool = False
    schedule_override: CheckinScheduleOverride | None = Field(default=None, validation_alias=AliasChoices("schedule_override", "schedule"))
    config: dict[str, Any] = Field(default_factory=dict)


class CheckinAccountUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str | None = Field(default=None, min_length=1, max_length=200, validation_alias=AliasChoices("name", "label"))
    adapter: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    enabled: bool | None = None
    schedule_override: CheckinScheduleOverride | None = Field(default=None, validation_alias=AliasChoices("schedule_override", "schedule"))
    clear_schedule_override: bool = False
    config: dict[str, Any] | None = None
    clear_secrets: list[str] = Field(default_factory=list)


class CheckinAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    site_id: UUID
    name: str
    adapter: str
    enabled: bool
    schedule_override: CheckinScheduleOverride | None = None
    profile_key: str
    configured: bool = False
    configured_secret_fields: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    login_state: str = "not_configured"
    created_at: str | None = None
    updated_at: str | None = None


class ProductSettingsUpdate(BaseModel):
    enabled: bool | None = None
    interval_override_minutes: int | None = Field(default=None, ge=1, le=10080, validation_alias=AliasChoices("interval_override_minutes", "interval_minutes"))
    clear_interval_override: bool = False
    scraper_type: str | None = Field(default=None, max_length=64)
    config: dict[str, Any] | None = None
    clear_secrets: list[str] = Field(default_factory=list)


class ProductSettingsOut(BaseModel):
    site_id: UUID
    enabled: bool
    interval_override_minutes: int | None = None
    effective_interval_minutes: int = 60
    scraper_type: str = "none"
    configured: bool = False
    configured_secret_fields: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    kind: JobKind
    status: JobStatus
    site_id: UUID | None = None
    account_id: UUID | None = None
    scheduled_for: datetime
    attempt: int = 0
    max_attempts: int = 3
    progress: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error_kind: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ScheduleSettingsUpdate(BaseModel):
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    checkin_window_start: str = Field(pattern=r"^\d{2}:\d{2}:\d{2}$")
    checkin_window_end: str = Field(pattern=r"^\d{2}:\d{2}:\d{2}$")
    product_interval_minutes: int = Field(ge=1, le=10080)

    @model_validator(mode="after")
    def validate_window(self) -> "ScheduleSettingsUpdate":
        try:
            start = time.fromisoformat(self.checkin_window_start)
            end = time.fromisoformat(self.checkin_window_end)
        except ValueError:
            raise ValueError("check-in window times must be valid") from None
        if start >= end:
            raise ValueError("check-in window end must be after start")
        return self


class ScheduleSettingsOut(ScheduleSettingsUpdate):
    pass


class WorkerStatusOut(BaseModel):
    state: Literal["disabled", "starting", "ready", "degraded", "circuit_open", "stopped"]
    pid: int | None = None
    instance_id: str | None = None
    protocol_version: int = 1
    restart_count: int = 0
    last_heartbeat_at: datetime | None = None
    last_error: str | None = None


class ArtifactOut(BaseModel):
    id: UUID
    run_id: UUID | None = None
    job_id: UUID | None = None
    role: str
    relative_path: str
    size: int
    sha256: str
    mime_type: str | None = None
    created_at: datetime


class HealthOut(BaseModel):
    status: Literal["healthy", "degraded", "unhealthy"]
    database: str
    worker: str
    scheduler: str
    uptime_seconds: int
