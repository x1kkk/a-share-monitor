"""One cooldown gate shared by browser events and a bounded WeCom queue."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from urllib.parse import urlparse

import httpx

from config import Settings

log = logging.getLogger(__name__)
EVENT_NAMES = {"anomaly": "实时异动", "limit_up": "首次封板", "break_open": "炸板告警"}


class Notifier:
    def __init__(self, client: httpx.AsyncClient, cfg: Settings):
        self.client, self.cfg = client, cfg
        self.last_notify_time: dict[tuple[str, str], float] = {}
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)
        self.sent = self.failed = self.suppressed = self.dropped = 0
        if cfg.wechat_webhook_url:
            url = urlparse(cfg.wechat_webhook_url)
            if url.scheme != "https" or url.hostname != "qyapi.weixin.qq.com" or url.path != "/cgi-bin/webhook/send":
                raise ValueError("WECHAT_WEBHOOK_URL must be an HTTPS WeCom group robot webhook")

    def publish(self, events: list[dict], *, clock: float | None = None) -> list[dict]:
        clock = time.monotonic() if clock is None else clock
        accepted = []
        # Prune old cooldown keys, retaining a bounded daily dictionary.
        self.last_notify_time = {key: ts for key, ts in self.last_notify_time.items()
                                 if clock - ts < self.cfg.notify_cooldown}
        for raw in events:
            # Simulation never consumes the live-event cooldown budget.
            code = ("mock:" if raw["simulated"] else "live:") + raw["code"]
            key = (code, raw["event_type"])
            if key in self.last_notify_time:
                self.suppressed += 1
                log.info("冷却抑制 %s %s %s", raw["code"], raw["name"], EVENT_NAMES[raw["event_type"]])
                continue
            self.last_notify_time[key] = clock
            event = {**raw, "id": uuid.uuid4().hex}
            accepted.append(event)
            log.info("%s[%s] %s %s 价格=%.2f 涨幅=%.2f%% 涨速=%s 分数=%s",
                     "模拟" if raw["simulated"] else "实盘", EVENT_NAMES[raw["event_type"]],
                     raw["code"], raw["name"], raw["price"], raw["pct"],
                     raw.get("speed_2m", "-"), raw.get("score", "-"))
            if self.cfg.wechat_webhook_url and (not raw["simulated"] or self.cfg.mock_wechat):
                try:
                    self.queue.put_nowait(event)
                except asyncio.QueueFull:
                    self.dropped += 1
                    log.error("企业微信队列已满：事件 %s 未投递", event["id"])
        return accepted

    @staticmethod
    def markdown(event: dict) -> str:
        name = str(event["name"]).replace("<", "").replace(">", "").replace("\n", " ")
        mode = "模拟演练" if event["simulated"] else "实盘监控"
        content = (f"### 【{mode}】{EVENT_NAMES[event['event_type']]}\n"
                   f"**{name} ({event['code']})**\n"
                   f"> 价格：{event['price']:.2f}　涨幅：{event['pct']:.2f}%\n"
                   f"> 时间：{event['time']}\n")
        if "speed_2m" in event:
            content += f"> 两分钟涨速：{event['speed_2m']:.2f}%　强度：{event['score']:.2f}\n"
        if "break_count" in event:
            content += f"> 今日开板次数：{event['break_count']}\n"
        return content + f"\n事件编号：{event['id']}"

    async def worker(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                # This worker is independent of the two-second polling task.
                response = await self.client.post(
                    self.cfg.wechat_webhook_url,
                    json={"msgtype": "markdown", "markdown": {"content": self.markdown(event)}},
                    timeout=5.0,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("errcode") != 0:
                    raise ValueError("WeCom rejected robot message")
                self.sent += 1
            except (httpx.HTTPError, ValueError):
                # No ambiguous retries: a timed-out POST may already have been delivered.
                self.failed += 1
                log.error("企业微信发送失败 event=%s；检查网络、Webhook 和机器人限流", event["id"])
            finally:
                self.queue.task_done()
            await asyncio.sleep(3.1)

    def status(self) -> dict:
        return {"configured": bool(self.cfg.wechat_webhook_url), "mock_enabled": self.cfg.mock_wechat,
                "queued": self.queue.qsize(), "sent": self.sent, "failed": self.failed,
                "suppressed": self.suppressed, "dropped": self.dropped}
