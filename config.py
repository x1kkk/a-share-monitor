"""Single-process settings. Environment variables override the defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
MOCK_MODE: bool = True
HTTP_PROXY: str | None = None
WECHAT_WEBHOOK_URL: str = ""


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    mock_mode: bool = field(default_factory=lambda: env_bool("MOCK_MODE", MOCK_MODE))
    http_proxy: str | None = field(default_factory=lambda: os.getenv("HTTP_PROXY") or HTTP_PROXY)
    wechat_webhook_url: str = field(default_factory=lambda: os.getenv("WECHAT_WEBHOOK_URL", WECHAT_WEBHOOK_URL))
    # Off-hours simulation must never send pretend stock signals to a real group by default.
    mock_wechat: bool = field(default_factory=lambda: env_bool("MOCK_WECHAT", False))
    api_token: str = field(default_factory=lambda: os.getenv("API_TOKEN", ""))
    poll_seconds: float = 2.0
    http_timeout: float = 3.0
    eastmoney_deadline: float = 5.0
    book_deadline: float = 1.5
    page_size: int = 100
    max_pages: int = 80
    max_concurrency: int = 12
    bootstrap_deadline: float = 10.0
    bootstrap_http_timeout: float = 3.0
    calendar_timeout: float = 18.0
    notify_cooldown: float = 300.0
    active_seconds: float = 120.0
    remove_seconds: float = 420.0
    minimum_listing_sessions: int = 15
    max_new_per_minute: int = 10
    break_sell_lots: float = 10000.0
    # Only a small, fixed cohort is stimulated; the rest can age out naturally.
    mock_cohort: int = 8
    mock_seed: int = 20261103
    eastmoney_url: str = "https://push2.eastmoney.com/api/qt/clist/get"
    tencent_url: str = "https://qt.gtimg.cn/q="
    # httpx encodes spaces as '+'. Literal '+' here would become '%2B'.
    market_filter: str = "m:0 t:6,m:1 t:2"
    fields: str = "f2,f3,f5,f6,f8,f12,f14,f15,f18,f21,f26,f62,f66,f72,f100,f103,f124"
    cors_origins: tuple[str, ...] = field(default_factory=lambda: tuple(
        origin.strip().rstrip("/") for origin in os.getenv(
            "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,"
            "http://localhost:8000,http://127.0.0.1:8000"
        ).split(",") if origin.strip()))


DEFAULT_WEIGHTS = {"speed_2m": 35.0, "main_inflow": 20.0, "turnover": 20.0,
                   "large_inflow": 15.0, "amount": 10.0}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
           "Referer": "https://quote.eastmoney.com/"}
settings = Settings()
