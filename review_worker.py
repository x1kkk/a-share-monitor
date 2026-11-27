"""One bounded AKShare job per child process; JSON stdin/stdout only.

Amounts are yuan, prices yuan, volume hands. AKShare already scales pool prices.
The parent owns deadlines, cancellation, caching and concurrency.
"""
from __future__ import annotations

import contextlib
import json
import math
import re
import sys
from datetime import date


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def text(value):
    return value.strip() if isinstance(value, str) and value.strip().lower() not in {"nan", "none", "-"} else None


def main_board(code, name=None):
    return bool(isinstance(code, str) and re.fullmatch(r"(?:00|60)[0-9]{4}", code)
                and (name is None or text(name) and "ST" not in name.upper()))


def seal_time(value):
    if value is None:
        return None
    value = str(value).split(".")[0].zfill(6)
    if value != "000000" and re.fullmatch(r"(?:[01][0-9]|2[0-3])[0-5][0-9][0-5][0-9]", value):
        return f"{value[:2]}:{value[2:4]}:{value[4:]}"
    return None


def pool_rows(records, sealed):
    rows = []
    for raw in records:
        code, name = raw.get("代码"), text(raw.get("名称"))
        if not main_board(code, name) or name is None:
            continue
        streak = number(raw.get("连板数"))
        breaks = number(raw.get("炸板次数"))
        rows.append({
            "code": code, "name": name, "price": number(raw.get("最新价")),
            "change_pct": number(raw.get("涨跌幅")), "amount": number(raw.get("成交额")),
            "turnover_rate": number(raw.get("换手率")),
            "seal_amount": number(raw.get("封板资金")) if sealed else None,
            "streak_days": int(streak) if streak is not None and streak >= 1 and streak.is_integer() else None,
            "break_count": int(breaks) if breaks is not None and breaks >= 0 and breaks.is_integer() else None,
            "first_seal_time": seal_time(raw.get("首次封板时间")),
            "industry": text(raw.get("所属行业")), "volume": number(raw.get("成交量")),
            "is_limit_up": sealed,
        })
    if len({r["code"] for r in rows}) != len(rows):
        raise ValueError("duplicate pool code")
    return rows


def pools(ak, day):
    result = {"success": [], "broken": [], "status": {}}
    # AKShare collapses data:null and pool:[] into the same empty DataFrame.
    # Observe the raw response only to preserve that essential distinction.
    try:
        from akshare.utils import http
    except ImportError:  # Older supported AKShare releases use requests directly.
        http = None
    original = http.get if http else None
    for key, func in (("success", ak.stock_zt_pool_em), ("broken", ak.stock_zt_pool_zbgc_em)):
        observed = []

        def tracked_get(*args, **kwargs):
            kwargs.setdefault("timeout", 8)
            response = original(*args, **kwargs)
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            observed.append(isinstance(data, dict) and isinstance(data.get("pool"), list))
            return response

        if http:
            http.get = tracked_get
        try:
            frame = func(date=day.replace("-", ""))
            if not frame.empty and not {"代码", "名称", "最新价"} <= set(frame.columns):
                raise ValueError("invalid pool schema")
            rows = pool_rows(frame.to_dict("records"), key == "success")
            valid = bool(rows) or bool(observed and all(observed))
            result[key] = rows
            result["status"][key] = "ready" if valid else "unavailable"
        except Exception as exc:
            print(f"review {key}: {type(exc).__name__}", file=sys.stderr)
            result["status"][key] = "unavailable"
        finally:
            if http:
                http.get = original
    return result


def iso_day(value):
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def stock_rows(prices, flows, start, end):
    rows = {}
    fields = {"price": "收盘", "change_pct": "涨跌幅", "amount": "成交额",
              "turnover_rate": "换手率", "volume": "成交量", "high": "最高"}
    for raw in prices:
        day = iso_day(raw.get("日期"))
        if day is None or not start <= day <= end:
            continue
        if day in rows:
            raise ValueError("duplicate daily price")
        rows[day] = {"date": day, **{key: number(raw.get(source)) for key, source in fields.items()},
                     "main_inflow": None}
    for raw in flows:
        day = iso_day(raw.get("日期"))
        if day is None or not start <= day <= end:
            continue
        row = rows.setdefault(day, {"date": day, **dict.fromkeys(fields), "main_inflow": None})
        row["main_inflow"] = number(raw.get("主力净流入-净额"))
    return [rows[day] for day in sorted(rows)]


def stock(ak, body):
    code = body["code"]
    if not main_board(code, body.get("name")):
        raise ValueError("main-board stock required")
    start, end = date.fromisoformat(body["start_date"]).isoformat(), date.fromisoformat(body["end_date"]).isoformat()
    if start > end:
        raise ValueError("invalid date range")
    prices, flows, status, rate_limited = [], [], {}, False
    for key, fetch in (("price", lambda: ak.stock_zh_a_hist(
            symbol=code, period="daily", start_date=start.replace("-", ""),
            end_date=end.replace("-", ""), adjust="")),
            ("flow", lambda: ak.stock_individual_fund_flow(stock=code, market="sh" if code.startswith("60") else "sz"))):
        if rate_limited:
            status[key] = "unavailable"
            continue
        try:
            frame = fetch()
            records = frame.to_dict("records")
            if key == "price":
                prices = records
            else:
                flows = records
            status[key] = "ready" if any((day := iso_day(r.get("日期"))) is not None
                                          and start <= day <= end for r in records) else "unavailable"
        except Exception as exc:
            print(f"review stock {key}: {type(exc).__name__}", file=sys.stderr)
            status[key] = "unavailable"
            rate_limited |= "RateLimit" in type(exc).__name__
    return {"rows": stock_rows(prices, flows, start, end), "status": status, "rate_limited": rate_limited}


def lhb_rows(records, day):
    values = {}
    for raw in records:
        code, name = raw.get("代码"), text(raw.get("名称"))
        if not main_board(code, name) or name is None or iso_day(raw.get("上榜日")) != day:
            continue
        # Multi-session abnormal-movement disclosures are a different time scope.
        reason = str(raw.get("上榜原因") or "")
        if "连续" in reason or "三个交易日" in reason or "3个交易日" in reason:
            continue
        value = number(raw.get("龙虎榜净买额"))
        if value is not None:
            values.setdefault(code, set()).add(value)
    # Repeated reasons frequently report the same total; never sum them. If the
    # source reports conflicting totals, keep the symbol unknown rather than guess.
    return {code: next(iter(numbers)) if len(numbers) == 1 else None for code, numbers in values.items()}


def lhb(ak, day):
    try:
        compact = day.replace("-", "")
        frame = ak.stock_lhb_detail_em(start_date=compact, end_date=compact)
        records = frame.to_dict("records")
        if records and (not {"代码", "名称", "上榜日", "龙虎榜净买额"} <= set(frame.columns)
                        or not any(iso_day(r.get("上榜日")) == day for r in records)):
            raise ValueError("invalid LHB schema or date")
        # Empty results may precede publication. Keep pending so the scheduler retries.
        return {"net_buy": lhb_rows(records, day), "status": "ready" if records else "pending"}
    except Exception as exc:
        print(f"review lhb: {type(exc).__name__}", file=sys.stderr)
        return {"net_buy": {}, "status": "unavailable"}


def run(body):
    if not isinstance(body, dict):
        raise ValueError("object required")
    operation = body.get("operation")
    if operation not in {"pools", "stock", "lhb"}:
        raise ValueError("invalid review operation")
    day = date.fromisoformat(body["date"]).isoformat() if operation != "stock" else None
    import akshare as ak
    try:
        from akshare.utils.context import set_timeout
        set_timeout(8)
    except ImportError:
        pass  # Parent's process deadline also bounds older AKShare releases.
    if operation == "pools":
        return pools(ak, day)
    if operation == "stock":
        return stock(ak, body)
    return lhb(ak, day)


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        body = json.load(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):
            result = run(body)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}))
        sys.exit(1)
