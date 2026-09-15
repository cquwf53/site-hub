from pydantic import BaseModel, ConfigDict, Field


class MonitorPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    watched_only: bool = False
    price_drop_amount: float = Field(default=0, ge=0)
    price_drop_percent: float = Field(default=0, ge=0, le=100)
    stock_confirmations: int = Field(default=1, ge=1, le=5)
    notification_cooldown_minutes: int = Field(default=60, ge=0, le=10080)
    catalog_drop_threshold_percent: float = Field(default=70, ge=1, le=100)
    catalog_minimum_size: int = Field(default=10, ge=1, le=10000)


class ProductWatchRule(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    watched: bool = False
    price_drop_amount: float | None = Field(default=None, ge=0)
    price_drop_percent: float | None = Field(default=None, ge=0, le=100)
