"""Exchange sessions and yesterday's streak cache; no weekday approximation in live mode."""
from __future__ import annotations

import asyncio
import bisect
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime

from config import SHANGHAI, Settings

log = logging.getLogger(__name__)

# A subprocess bounds AKShare's synchronous network work and can actually be killed
# on timeout (cancelling asyncio.to_thread would leave its requests running).
AK_BOOTSTRAP = r'''
import contextlib, datetime, json, sys
with contextlib.redirect_stdout(sys.stderr):
    import akshare as ak
    dates = sorted(str(x)[:10] for x in ak.tool_trade_date_hist_sina()["trade_date"])
    today = sys.argv[1]
    previous = [x for x in dates if x < today]
    streaks = {}
    streaks_ready = False
    if previous:
        try:
            pool = ak.stock_zt_pool_em(date=previous[-1].replace("-", ""))
            for _, row in pool.iterrows():
                streaks[str(row["代码"]).zfill(6)] = int(row["连板数"])
            streaks_ready = True
        except Exception:
            pass
print(json.dumps({"sessions": dates, "streaks": streaks, "streaks_ready": streaks_ready}))
'''


@dataclass
class MarketCalendar:
    sessions: list[date] = field(default_factory=list)
    yesterday_streaks: dict[str, int] = field(default_factory=dict)
    streaks_ready: bool = False
    loaded_for: date | None = None
    error: str | None = "calendar not initialized"

    async def load(self, cfg: Settings, today: date) -> None:
        process = None
        if self.loaded_for != today:
            self.yesterday_streaks.clear()
            self.streaks_ready = False
        try:
            process_env = {key: value for key, value in os.environ.items()
                           if key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}}
            if cfg.http_proxy:
                process_env.update(HTTP_PROXY=cfg.http_proxy, HTTPS_PROXY=cfg.http_proxy)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", AK_BOOTSTRAP, today.isoformat(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=process_env,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), cfg.calendar_timeout)
            if process.returncode:
                raise ValueError("AKShare calendar process failed")
            data = json.loads(stdout)
            sessions = sorted({date.fromisoformat(x) for x in data["sessions"]})
            if not sessions or sessions[-1] < today:
                raise ValueError("calendar does not cover today; update AKShare")
            self.sessions = sessions
            self.yesterday_streaks = {k: int(v) for k, v in data["streaks"].items()}
            self.streaks_ready = bool(data["streaks_ready"])
            self.loaded_for, self.error = today, None
            log.info("盘前缓存：交易日=%d 昨日连板记录=%d", len(sessions), len(self.yesterday_streaks))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.loaded_for = today
            self.error = str(exc)
            self.yesterday_streaks.clear()
            self.streaks_ready = False
            log.warning("盘前初始化不可用：%s；实盘严格过滤关闭，模拟模式仍可运行", self.error)
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()

    def covered(self, today: date) -> bool:
        return bool(self.error is None and self.sessions and self.sessions[-1] >= today and self.loaded_for == today)

    def listing_is_mature(self, listed: date | None, today: date, minimum: int = 15) -> bool:
        if listed is None or listed > today or not self.covered(today):
            return False
        # At the 15th session (listing day inclusive) the stock is eligible.
        return bisect.bisect_right(self.sessions, today) - bisect.bisect_left(self.sessions, listed) >= minimum

    def session(self, now: datetime) -> str:
        now = now.astimezone(SHANGHAI)
        if not self.covered(now.date()):
            return "calendar_unavailable"
        if now.date() not in self.sessions:
            return "closed"
        minute = now.hour * 60 + now.minute
        if 555 <= minute < 690:
            return "morning"
        if 780 <= minute < 900:
            return "afternoon"
        return "closed"
