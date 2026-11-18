"""市场页：把市场切片收成设计稿上的结论、资金、好不好做、宏观、日历、板块和自选。

首屏摘要和右侧依据各 tab 都从同一份切片包里算，切片包 5 分钟内只取一次。
国家队没有单独的日频接口，这里不编造申购金额；打开这一页也不给自选排队诊断。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.services import market_rules as rules
from app.services import market_changes, market_emotion, market_intraday, market_notify, market_sector, market_team
from app.services.market_store import get_store
from app.services import market_disk
from app.services.market_tape import build, judge_pulse
from app.tools import cache

_ZONE = ZoneInfo("Asia/Shanghai")
_BUNDLE_TTL = 300
_BUNDLE_LOCK = threading.Lock()
_REFRESH_LOCK = threading.Lock()
_REFRESHING: set[str] = set()
_GENERATION = 0
# 首屏用得到的。南向、Shibor、新发基金、行业两融只在点开右侧时再取。
CORE = (
    "turnover", "etf", "margin", "supply", "breadth", "limits", "index", "sw",
    "macro", "valuation", "fx", "us", "hk", "ipo", "calendar", "unlock",
)
LAZY = ("south", "shibor", "funds", "margin_industry")
PARTS = CORE + LAZY
TABS = ("fund", "team", "sector", "mood", "cal", "macro")
# 首屏三张独立卡：资金 / 国家队 / 板块（money 是 fund 的别名）
CARDS = ("fund", "team", "sector")
_CARD_ALIASES = {"money": "fund"}
_WEEKDAY = "一二三四五六日"


# ---------------------------------------------------------------- 文案小工具
def _pct(value, digits: int = 1) -> str:
    if value is None:
        return "没有"
    return f"{value * 100:+.{digits}f}%"


def _yi(value, signed: bool = False) -> str:
    if value is None:
        return "没有"
    body = f"{abs(value) / 10000:.2f} 万亿" if abs(value) >= 10000 else f"{abs(value):.{0 if abs(value) >= 100 else 1}f} 亿"
    if not signed:
        return body if value >= 0 else "−" + body
    return ("+" if value > 0 else "−" if value < 0 else "") + body


def _plain(value, digits: int = 2) -> str:
    return "没有" if value is None else f"{value:.{digits}f}"


def _delta(value, digits: int = 1) -> str:
    return "没有" if value is None else f"{value:+.{digits}f}".replace("-", "−")


def _word(pctile) -> str:
    if pctile is None:
        return ""
    return "很低" if pctile < 0.2 else "偏低" if pctile < 0.4 else "中等" if pctile < 0.6 else "偏高" if pctile < 0.8 else "很高"


def _pctile_info(label: str, pctile) -> dict | None:
    if pctile is None:
        return None
    return {"label": label, "value": pctile, "word": _word(pctile), "text": f"{round(pctile * 100)}% 分位"}


def _age(day: str, today: date | None = None, previous: str = "") -> str:
    """统一写月-日。today、previous 保留是为了旧的调用签名。"""
    if not day:
        return ""
    return day[5:]


def _updated_label(iso: str, today: date | None = None) -> str:
    """卡首行用的时效标签：一律写月-日，不出现「今日」。today 保留兼容旧调用。"""
    if not iso:
        return ""
    day = iso[:10] if len(iso) >= 10 else iso
    if len(day) >= 7 and day[4] == "-":
        return day[5:]
    return day


def _previous_open(parts: dict, today: date) -> str:
    return parts.get("_prev") or ""


def _session_phase(now: datetime | None = None) -> dict:
    """盘前 / 盘中 / 盘后会话态，供头栏 wm=pre|live|post 对齐各端。"""
    now = now or datetime.now(_ZONE)
    day = now.date()
    trading = True
    try:
        from app.scheduler.calendar import TradingCalendar

        trading = TradingCalendar().is_trading_day(day)
    except Exception:
        trading = day.weekday() < 5
    minute = now.hour * 60 + now.minute
    open_m, close_m = 9 * 60 + 30, 15 * 60
    if not trading:
        phase, label = "post", "休市日"
    elif minute < open_m:
        phase, label = "pre", "盘前"
    elif minute < close_m:
        phase, label = "live", "盘中"
    else:
        phase, label = "post", "盘后"
    # 免费行情常见延迟；盘中标 0 表示按实时口径，盘前/盘后用收盘价。
    delay = 0 if phase == "live" else None
    return {
        "session": phase,
        "phase": phase,
        "phase_label": label,
        "trading_day": trading,
        "quote_delay_sec": delay,
        "clock": now.strftime("%H:%M:%S"),
    }


def _unchanged_since(day: date) -> str:
    """当前结论档位从哪天起没变；没有快照时返回空。"""
    try:
        rows = market_changes.history(get_store().snapshots())
    except Exception:
        return ""
    if not rows:
        return ""
    latest = rows[0]
    since = str(latest.get("since") or "")
    until = str(latest.get("until") or "")
    if until and until != day.isoformat() and until < day.isoformat():
        return since
    return since


def _stat(label: str, value: str, on: bool = False) -> dict:
    return {"label": label, "value": value, "on": on}


_SPANS = (1, 3, 5, 30, 60)


def _stored(bucket, n: int):
    if not isinstance(bucket, dict):
        return None
    if n in bucket:
        return bucket[n]
    return bucket.get(str(n))


def _etf_marks(etf: dict) -> list[int]:
    if etf.get("votes") is not None:
        return etf["votes"]
    label, windows = etf.get("label"), etf.get("windows")
    marks = []
    for n in (5, 30, 60):
        value = _stored(windows, n)
        if value is None:
            continue
        if label == "净流入" and value >= 0.01:
            marks.append(n)
        elif label == "净流出" and value <= -0.01:
            marks.append(n)
    return marks


def _margin_marks(margin: dict) -> list[int]:
    if margin.get("votes") is not None:
        return margin["votes"]
    label, windows = margin.get("label"), margin.get("windows")
    day30, day60 = _stored(windows, 30), _stored(windows, 60)
    if label == "加速下降":
        return [n for n, value in ((30, day30), (60, day60)) if value is not None and value < -0.05]
    if label == "在降" and day30 is not None and day30 < -0.02:
        return [30]
    if label == "在升" and day30 is not None and day30 > 0.02:
        return [30, 60] if day60 is not None and day60 > 0 else [30]
    return []


def _mz_unit(value) -> str:
    if value is None:
        return "—"
    rounded = round(float(value), 1)
    if rounded == 0:
        return "0.0 Mz"
    sign = "+" if rounded > 0 else "−"
    return f"{sign}{abs(rounded):.1f} Mz"


def _mz_stats(mz, days, mark=()) -> list[dict]:
    marks = {mark} if isinstance(mark, int) else set(mark or [])
    return [_stat(f"{n} 日", _mz_unit(_stored(mz, n)), n in marks) for n in days]


def _span_stats(bucket, fmt, mark=()) -> list[dict]:
    """1 / 3 / 5 / 30 / 60 个交易日，同一排。mark 里的档位是这次判断用上的。"""
    marks = {mark} if isinstance(mark, int) else set(mark or [])
    return [_stat(f"{n} 日", fmt(_stored(bucket, n)), n in marks) for n in _SPANS]


# ---------------------------------------------------------------- 取数
def _fetch(name: str, day: date) -> tuple[str, dict]:
    try:
        return name, build(name, day)
    except Exception:
        return name, {"metrics": {}, "text": "", "facts": [], "unavailable": ["没有取到"]}


def _bundle_key(day: date) -> str:
    return f"market_page:bundle:{day.isoformat()}"


def _store_bundle(payload: dict, day: date, generation: int) -> dict:
    with _REFRESH_LOCK:
        if generation != _GENERATION:
            return payload
    cache.set(_bundle_key(day), payload, _BUNDLE_TTL)
    market_disk.save_bundle(day.isoformat(), payload)
    return payload


def bundle(today: date | None = None) -> dict:
    """首屏切片。内存或磁盘里有今天的结果就直接给；过了 5 分钟在后台重算。没有今天的就先给上一份。"""
    day = today or datetime.now(_ZONE).date()
    key = _bundle_key(day)
    cached = cache.get(key)
    if cached is not None:
        return cached
    disk = market_disk.load_bundle(day.isoformat())
    age = market_disk.bundle_age(day.isoformat())
    if disk is not None and age is not None and age < _BUNDLE_TTL:
        cache.set(key, disk, _BUNDLE_TTL)
        return disk
    if disk is not None:
        _kick(day)
        return disk
    with _BUNDLE_LOCK:
        cached = cache.get(key)
        if cached is not None:
            return cached
        older = market_disk.latest_bundle()
        if older and older.get("_day") != day.isoformat():
            _kick(day)
            return older
        with _REFRESH_LOCK:
            generation = _GENERATION
        return _store_bundle(_compute(day), day, generation)


def _warm_members() -> None:
    from app.services import market_tape

    market_tape._member_map()


def _compute(day: date) -> dict:
    with ThreadPoolExecutor(max_workers=4) as pool:
        part_futures = [pool.submit(_fetch, name, day) for name in CORE]
        sector_future = pool.submit(_sector, day)
        member_future = pool.submit(_safe, _warm_members, None)
        extra = {
            "team": pool.submit(_safe, lambda: market_team.build(day), {"ok": False}),
            "announce": pool.submit(_safe, market_team.announcements, {"items": [], "ok": False}),
            "emotion": pool.submit(_safe, lambda: market_emotion.build(day), {"ok": False, "days": []}),
            "catalysts": pool.submit(_safe, market_sector.catalysts, {}),
        }
        docs = dict(future.result() for future in part_futures)
        result = {name: doc.get("metrics") or {} for name, doc in docs.items()}
        result.update({name: future.result() for name, future in extra.items()})
        result["_text"] = {name: doc.get("text") or "" for name, doc in docs.items()}
        result["sectors"] = sector_future.result()
        member_future.result()
    result["watch"] = _watch(result["sectors"], day)
    result["personal"] = _personal(result["watch"], day)
    result["_day"] = day.isoformat()
    result["_prev"] = _prev_trade_day(day)
    return result


def _kick(day: date) -> None:
    token = day.isoformat()
    with _REFRESH_LOCK:
        if token in _REFRESHING:
            return
        _REFRESHING.add(token)
        generation = _GENERATION

    def run() -> None:
        try:
            payload = _compute(day)
            if _store_bundle(payload, day, generation) is payload and generation == _GENERATION:
                try:
                    market_notify.record(summarize(payload, day), payload)
                except Exception:
                    pass
        finally:
            with _REFRESH_LOCK:
                _REFRESHING.discard(token)

    threading.Thread(target=run, daemon=True).start()


def ensure(parts: dict, names: tuple[str, ...], day: date) -> None:
    """右侧 tab 才用到的切片。补进当前这份结果，刷新进行中时不写回磁盘。"""
    missing = [name for name in names if name not in parts]
    if not missing:
        return
    with ThreadPoolExecutor(max_workers=4) as pool:
        docs = dict(pool.map(lambda name: _fetch(name, day), missing))
    text = parts.setdefault("_text", {})
    for name, doc in docs.items():
        parts[name] = doc.get("metrics") or {}
        text[name] = doc.get("text") or ""
    token = parts.get("_day") or day.isoformat()
    with _REFRESH_LOCK:
        refreshing = token in _REFRESHING
    if refreshing:
        return
    cache.set(f"market_page:bundle:{token}", parts, _BUNDLE_TTL)
    market_disk.save_bundle(token, parts)


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def _prev_trade_day(day: date) -> str:
    from app.services import market_tape

    try:
        before = [item for item in market_tape.open_dates(day, 3) if item < day.strftime("%Y%m%d")]
    except Exception:
        return ""
    return market_tape.iso(before[-1]) if before else ""


def _sector(day: date) -> dict:
    try:
        return market_sector.build(day)
    except Exception:
        return {"rows": [], "date": "", "windows": list(market_sector.WINDOWS), "conclusion_days": market_sector.CONCLUSION_DAYS}


def warm() -> None:
    """后台预热：页面第一次打开前把切片包取好。"""
    threading.Thread(target=lambda: bundle(), daemon=True).start()


def _watch(sectors: dict, day: date) -> list[dict]:
    try:
        from app.services import market_tape
        from app.synthesis import watchlist, worker
        from app.synthesis.service import current_mode

        store = worker.get_store()
        mode = current_mode()
        by_name = {row["name"]: row for row in sectors.get("rows") or []}
        items = []
        for row in store.watch_list():
            code = row["ts_code"]
            info = _industry_info(code, market_tape)
            l1 = info.get("l1_name", "")
            sector_name = market_sector.sector_for(l1, info.get("l2_name", ""), info.get("l3_name", ""))
            sector = by_name.get(sector_name or "")
            record = store.latest(code)
            page = watchlist.shown_page(record, mode) if record else None
            lean = ((page or {}).get("direction") or {}).get("lean")
            verdict = sector["verdict"] if sector else None
            phase = sector["phase"] if sector else None
            bucket = sector.get("bucket") if sector else None
            action, reason = rules.watch_action(phase, lean, bucket, verdict)
            items.append({
                "ts_code": code, "name": row["name"], "industry": l1 or "—",
                "sector": sector_name or "", "verdict": verdict or phase, "phase": phase,
                "direction": {"bullish": "偏多", "bearish": "偏空", "unclear": "不明"}.get(lean, "还没有判断"),
                "action": action, "reason": reason,
            })
        return items
    except Exception:
        return []


def _industry_info(code: str, tape) -> dict:
    """这只股票的申万一二三级行业名。和持仓页共用同一份缓存。"""
    try:
        rows = tape.cached(f"holdind:{code}", lambda: tape.pro().index_member_all(ts_code=code, is_new="Y"))
    except Exception:
        rows = []
    row = rows[0] if rows else {}
    return {key: str(row.get(key) or "") for key in ("l1_name", "l2_name", "l3_name")}


# ---------------------------------------------------------------- 结论
def conclusion(turnover: str, etf: str, margin: str) -> dict:
    """短期「钱」的结论只看成交、宽基份额和两融。南向、流动性和增减持不改这一档。"""
    lean = judge_pulse(turnover, etf, margin)
    gate = "通过" if lean == "在进" else "不过"
    notes = []
    if etf == "净流入" and turnover in ("正常", "平时") and margin == "加速下降":
        notes.append("资金在进、热度中性、杠杆在退")
    elif etf == "净流入" and margin == "加速下降":
        notes.append("杠杆背离：宽基在净流入，两融在加速下降")
    toward_in = []
    if etf != "净流入":
        toward_in.append("宽基 3 日偏离超过自身近 60 天常态，且 30 日、60 日也同向")
    if turnover == "冷清":
        toward_in.append("成交不再冷清")
    if turnover in ("正常", "平时") and margin == "加速下降" and etf == "净流入":
        toward_in.append("成交转活跃，或两融不再加速下降")
    toward_weak_met = []
    toward_weak_open = []
    if margin in ("在降", "加速下降"):
        toward_weak_met.append(f"两融{margin}")
    else:
        toward_weak_open.append("两融 3 日偏离落到在降")
    if turnover == "冷清":
        toward_weak_met.append("成交冷清")
    else:
        toward_weak_open.append("成交转为冷清")
    if lean == "在进":
        summary = "宽基在净流入，成交也不冷清，短期可以看成有钱进来。"
    elif notes:
        summary = "宽基在净流入，成交正常，两融在加速下降：资金在进、热度中性、杠杆在退，总档仍是观望。"
    elif lean == "偏弱" and etf == "净流入":
        summary = "成交冷清、借钱炒股的在还钱，但宽基 ETF 在净流入：有钱往 ETF 里去，要等成交回到正常才算「在进」。"
    elif lean == "偏弱":
        summary = "成交冷清，借钱炒股的余额还在降，没有新钱推动。"
    else:
        summary = "成交、宽基和两融没有一起指向进场或撤出。"
    return {
        "label": lean,
        "gate": gate,
        "gate_text": "只减不加" if lean == "偏弱" else ("可以看增量" if lean == "在进" else "先看，不加码"),
        "summary": summary,
        "notes": notes,
        "toward_in": toward_in,
        "toward_weak_met": toward_weak_met,
        "toward_weak_open": toward_weak_open,
    }


def _trend(indexes: list[dict]) -> tuple[str, str]:
    vote = rules.trend_vote(indexes)
    if not vote:
        return "没有", "指数日线不够 60 天"
    known = [row for row in indexes if row.get("above_ma60") is not None]
    below = [row["name"] for row in known if not row["above_ma60"]]
    above = [row["name"] for row in known if row["above_ma60"]]
    broke = [row["name"] for row in known if row.get("broke_low")]
    bits = []
    if vote["kind"] == "急跌":
        bits.append("、".join(vote["plunged"]) + "短期急跌（3 日跌超 4% 或 5 日跌超 6%）")
    if above:
        bits.append("、".join(above) + "在 60 日线上")
    if below:
        bits.append("、".join(below) + "在 60 日线下")
    if vote["kind"] == "收复":
        bits.append(f"但 {vote['above20']} 个已收复 20 日线，是在反弹，先按震荡看")
    if broke:
        bits.append("、".join(broke) + "已跌破前低")
    return vote["word"], "；".join(bits)


def _volume(turn: dict) -> dict | None:
    """今天成交额跟前 20 个交易日均值比，再看近一年分位。"""
    series = [point["value"] for point in (turn or {}).get("series") or [] if point.get("value") is not None]
    if len(series) < 21:
        return None
    return rules.volume_state(series[-1], series[-21:-1], turn.get("pctile"))


def _theme_view(last: dict) -> dict | None:
    """涨停最集中在哪几个行业，以及昨天的主线今天赚不赚钱。

    前三占比高只说明「涨停扎堆」，不等于可追的主线。大盘昨涨停今天还在亏钱、
    或昨天主线今天也翻绿时，最多写成「有集中」，避免冰点日误报「有主线」。
    """
    themes = last.get("themes") or []
    if not themes:
        return None
    share, follow = last.get("theme_share"), last.get("theme_follow")
    overall = last.get("follow")  # 昨涨停整体今天平均涨跌
    if follow is not None and follow <= rules.THEME_BREAK:
        word = "主线在退"
    elif share is not None and share >= 0.35:
        # 集中 + 追涨还在亏 / 主线自己翻绿 → 只算有集中
        if (overall is not None and overall < 0) or (follow is not None and follow < 0):
            word = "有集中"
        else:
            word = "有主线"
    elif share is not None and share <= 0.20:
        word = "没主线"
    else:
        word = "一般"
    detail = "今天涨停最集中：" + "、".join(f"{item['name']} {item['n']} 家" for item in themes)
    if share is not None:
        detail += f"（前三合计占 {round(share * 100)}%）"
    if follow is not None:
        detail += f"；昨天的主线今天平均 {follow:+.2f}%（{last.get('theme_n', 0)} 只，20cm 折成 10cm）"
    if word == "有集中":
        if overall is not None and overall < 0:
            detail += f"；昨涨停整体今天 {overall:+.2f}%，集中不等于能赚钱的主线"
        elif follow is not None and follow < 0:
            detail += "；主线今天翻绿，先不算有主线"
    return {"word": word, "detail": detail, "tone": {"有主线": "good", "主线在退": "bad"}.get(word, "flat")}


def _mood_notes(
    indexes: list[dict],
    breadth: dict,
    limits: dict,
    emotion: dict,
    volume: dict | None = None,
    etf: dict | None = None,
) -> list[str]:
    """几种 A 股常见的状态，只说「现在是什么状态、要看什么」，不给买卖指令。"""
    notes = []
    vote = rules.trend_vote(indexes) or {}
    up, down = breadth.get("up"), breadth.get("down")
    share = up / (up + down) if up is not None and down is not None and (up + down) else None
    last = (emotion or {}).get("last") or {}
    shanghai = next((row for row in indexes if row.get("name") == "上证指数"), None) or {}
    move = shanghai.get("pct")
    heavy = bool(volume) and volume["word"] in ("放量", "天量")
    light = bool(volume) and volume["word"] in ("缩量", "地量")
    if vote.get("kind") == "急跌":
        notes.append("急剧下跌：急跌后的第一天常有反抽，但这时追涨没有安全垫；要看第二天昨天涨停的票能不能转正。")
    if share is not None and share >= rules.THRUST and vote.get("word") in ("向下", "震荡") and vote.get("above", 0) * 4 < vote.get("known", 4) * 3:
        tail = "放量反抽，资金参与度比缩量反弹高；" if heavy else "缩量反弹，持续性要打问号；" if light else ""
        notes.append(f"普涨 {round(share * 100)}% 但指数还没站回 60 日线：多是下跌或急跌之后的反抽日。{tail}A 股常说「反抽不等于反转」，要看下一天能不能站稳、涨停股有没有接力。")
    if move is not None and move <= -1.0 and volume:
        if heavy:
            notes.append("放量下跌：分歧大，抛压还没衰竭。")
        elif light:
            notes.append("缩量下跌：抛压在减弱，但缩量不等于见底，要等放量企稳。")
    if volume and volume["word"] == "地量":
        notes.append("成交额处在近一年最低的 10%：常被说「地量见地价」，但地量可以持续，要等放量确认。")
    if volume and volume["word"] == "天量":
        notes.append("成交额处在近一年最高的 10%：天量之后常有分歧，要看后面能不能守住。")
    if last.get("down") is not None and last["down"] >= rules.PANIC_DOWN:
        notes.append(f"跌停 {last['down']} 家：恐慌日。常被当作情绪低点，但真正的修复要等昨涨停重新赚钱。")
    if (emotion or {}).get("stage") == "冰点":
        extra = "，且放量，恐慌还在释放" if heavy else "，且缩量，抛压在衰竭" if light else ""
        notes.append(f"冰点：亏钱效应集中{extra}。是不是低点，要看下一天昨涨停是否转正、跌停是否明显减少；转正之前只能算还在冰点。")
    if last.get("height_drop"):
        notes.append(f"高标断板：最高连板从 {last['height_prev']} 板掉到 {last['height']} 板，常被当作退潮的信号。")
    theme = last.get("theme_follow")
    if theme is not None and theme <= rules.THEME_BREAK:
        notes.append(f"主线大面：昨天涨停最集中的行业今天平均 {theme:+.2f}%，接力常在这时断掉。")
    risk = _price_flow_risk(etf or {}, indexes)
    if risk:
        notes.append(risk)
    return notes


def _price_flow_risk(etf: dict, indexes: list[dict]) -> str | None:
    """宽基判定为净流出，且大盘趋势向下：价、钱同弱，单独提示风险。
    不改总档位，只出警示；「基本没有」不算流出。"""
    if (etf or {}).get("label") != "净流出":
        return None
    vote = rules.trend_vote(indexes or []) or {}
    if vote.get("word") != "向下":
        return None
    return "风险偏高：宽基在流出，且大盘趋势向下（价、钱同弱）。"


def _money_word(tier: str | None) -> str:
    """数据没齐时档位是空的，页面显示「没有」，不能拿空档位去查词表。"""
    return rules.MONEY_WORD.get(tier) or "没有"


def _money_signal(parts: dict) -> dict:
    turn, etf, margin = parts["turnover"], parts["etf"], parts["margin"]
    labels = (turn.get("label") or "没有", etf.get("label") or "没有", margin.get("label") or "没有")
    if "没有" in labels:
        head = {
            "label": "没有", "gate": "不过", "gate_text": "数据没齐",
            "summary": "成交、宽基或两融有一项没取到，这一档先不算进总判断。",
            "notes": [],
            "toward_in": [], "toward_weak_met": [], "toward_weak_open": [],
        }
        return {"head": head, "tier": None, "tone": "flat"}
    head = conclusion(*labels)
    for part, name in ((turn, "成交"), (etf, "宽基"), (margin, "两融")):
        if part.get("baseline") == "不足":
            head.setdefault("notes", []).append(f"{name}基线不足，这档退回固定阈值")
    for mark in margin.get("marks") or []:
        head.setdefault("notes", []).append(mark)
    tier = rules.money_tier(head["label"])
    tone = {"顺": "good", "平": "warn", "逆": "bad"}[tier]
    return {"head": head, "tier": tier, "tone": tone}


def _mood_card(parts: dict) -> dict:
    indexes = (parts["index"] or {}).get("rows") or []
    breadth, limits = parts["breadth"], parts["limits"]
    signal = rules.mood_signal(indexes, breadth, limits)
    tier = signal["tier"]
    trend_word, trend_text = _trend(indexes)
    up, down = breadth.get("up"), breadth.get("down")
    share = up / (up + down) if up is not None and down is not None and (up + down) else None
    wide = "没有" if share is None else "有" if share >= 0.55 else "没有" if share <= 0.40 else "分化"
    follow, broken = limits.get("follow"), limits.get("broken_rate")
    if follow is None or broken is None:
        chase = "没有"
    else:
        chase = "能" if follow > 0 and broken < 0.30 else "难" if follow < -0.5 or broken > 0.40 else "一般"
    known = [row for row in indexes if row.get("above_ma60") is not None]
    above_n = sum(1 for row in known if row["above_ma60"])
    qa = [
        {"q": "大盘趋势怎么样？", "a": trend_word, "detail": trend_text or "没有取到指数", "tone": _tone3(trend_word, "向上", "向下"),
         "facts": [{"k": "60 日线上", "v": f"{above_n}/{len(known)}" if known else "没有", "tone": _tone3(trend_word, "向上", "向下")}]},
        {"q": "多数股票在涨吗？", "a": wide, "detail": (
            f"今天 {breadth.get('up', '—')} 涨、{breadth.get('down', '—')} 跌"
            + ("" if share is None else f"，上涨占 {round(share * 100)}%")
            + f"；近 25 个交易日新高 {breadth.get('highs', '—')}，新低 {breadth.get('lows', '—')}"
        ), "tone": _tone3(wide, "有", "没有"),
         "facts": ([{"k": "上涨", "v": f"{round(share * 100)}%", "tone": _tone3(wide, "有", "没有")}] if share is not None else [])
         + [{"k": "新高/新低", "v": f"{breadth.get('highs', '—')}/{breadth.get('lows', '—')}", "tone": "flat"}]},
        {"q": "短线追涨能赚钱吗？", "a": chase, "detail": (
            f"涨停 {limits.get('up', '—')} / 跌停 {limits.get('down', '—')}，最高 {limits.get('height', '—')} 板，炸板率 "
            + ("没有" if broken is None else f"{round(broken * 100)}%")
            + ("" if follow is None else f"；昨天涨停的 {limits.get('follow_count', 0)} 只今天平均 {follow:+.2f}%")
        ), "tone": _tone3(chase, "能", "难"),
         "facts": ([{"k": "昨涨停", "v": f"{follow:+.2f}%".replace("-", "−"), "tone": "good" if follow > 0 else "bad" if follow < 0 else "flat"}] if follow is not None else [])
         + ([{"k": "炸板", "v": f"{round(broken * 100)}%", "tone": "bad" if broken > 0.40 else "flat"}] if broken is not None else [])},
    ]
    emotion = parts.get("emotion") or {}
    if emotion.get("ok"):
        last = emotion["last"]
        follow = last.get("follow")
        qa.append({
            "q": "短线情绪走到哪了？", "a": emotion["stage"], "tone": {"发酵": "good", "高潮": "warn", "退潮": "bad", "冰点": "bad"}.get(emotion["stage"], "flat"),
            "facts": [{"k": "涨停", "v": str(last.get("up", "—")), "tone": "good"}, {"k": "跌停", "v": str(last.get("down", "—")), "tone": "bad" if last.get("down") else "flat"}],
            "detail": f"涨停 {last['up']} / 跌停 {last['down']}，最高 {last['height']} 板" + ("" if follow is None else f"，昨涨停今日 {follow:+.2f}%")
            + ("" if last.get("follow_board") is None or last.get("board_n", 0) < 3 else f"（其中昨天连板的 {last['board_n']} 只 {last['follow_board']:+.2f}%）")
            + "；按当天的数字判，再看前一天在哪一段",
        })
    volume = _volume(parts.get("turnover") or {})
    if volume:
        qa.append({
            "q": "成交量怎么样？", "a": volume["word"], "tone": "flat",
            "facts": [{"k": "今天", "v": _yi(volume["amount"]), "tone": "flat"}, {"k": "较前 20 日", "v": f"{round(volume['ratio'] * 100)}%", "tone": "flat"}],
            "detail": f"今天 {_yi(volume['amount'])}，是前 20 个交易日均值（{_yi(volume['base'])}）的 {round(volume['ratio'] * 100)}%"
                      + ("" if volume["pctile"] is None else f"；处在近一年的 {round(volume['pctile'] * 100)}% 分位"),
        })
    theme_view = _theme_view(emotion["last"]) if emotion.get("ok") else None
    if theme_view:
        top = (emotion.get("last") or {}).get("themes") or []
        theme_facts = []
        if top:
            theme_facts.append({"k": top[0]["name"], "v": f"{top[0]['n']} 家", "tone": theme_view["tone"]})
        share = (emotion.get("last") or {}).get("theme_share")
        if share is not None:
            theme_facts.append({"k": "前三占比", "v": f"{round(share * 100)}%", "tone": theme_view["tone"]})
        qa.append({"q": "涨停集中在哪几个行业？", "a": theme_view["word"], "tone": theme_view["tone"], "facts": theme_facts, "detail": theme_view["detail"]})
    means = {
        "顺": "环境对做多友好，但仍要挑有新钱的板块。",
        "平": "涨跌互有，选股比选时点重要。",
        "逆": "现在买进去，大概率要先扛一段；钱只往有新钱的板块去。",
    }.get(tier, "没有足够的数据判断。")
    notes = _mood_notes(indexes, breadth, limits, emotion, volume, parts.get("etf") or {})
    if notes:
        means = means + " " + " ".join(notes)
    when = {
        "顺": "多数指数跌回 60 日线下、或上涨家数不足四成 → 「好不好做」转平",
        "平": "多数指数站上 60 日线、上涨占五成五以上、追涨开始有收益 → 转顺",
        "逆": "多数指数站回 60 日线、上涨家数过半 → 「好不好做」转平",
    }.get(tier, "")
    _mood_attach_charts(qa, parts, emotion)
    return {
        "tier": tier, "label": signal["label"], "tone": {"顺": "good", "平": "warn", "逆": "bad"}.get(tier, "flat"),
        "reasons": signal["reasons"], "summary": "；".join(signal["reasons"]) or "没有足够的数据",
        "qa": qa, "means": means, "when": when, "date": breadth.get("date") or limits.get("date") or "",
        "stage": emotion.get("stage") if emotion.get("ok") else None, "volume": volume, "theme": theme_view,
        "stages": [{"date": row["date"], "stage": row["stage"], "up": row["up"], "down": row["down"]} for row in emotion.get("days") or []],
        "indexes": [
            {"name": row["name"], "close": row.get("close"), "pct": row.get("pct"), "ret20": row.get("ret20"),
             "ma20": row.get("above_ma20"), "ma60": row.get("above_ma60"), "broke_low": row.get("broke_low")}
            for row in indexes
        ],
        "scope": "全 A 不含 ST · 指数日线 · 涨停按 limit_list_d · 收盘后更新",
        "asof_note": "涨跌家数、涨跌停、指数日线收盘后一般当天就有",
    }


def _mood_attach_charts(qa: list[dict], parts: dict, emotion: dict) -> None:
    """给好不好做每条指标挂近约 30 日走势，首屏也能看，不只依赖「看依据」。"""
    turn = parts.get("turnover") or {}
    breadth = parts.get("breadth") or {}
    index_by = _index_by_day(turn.get("series"))
    sh = {"t": "上证", "c": "#5B6372"}
    vol_pts = (turn.get("series") or [])[-30:]
    trend_pts = [
        {"date": row["date"], "value": row["index"], "index": row["index"]}
        for row in vol_pts if row.get("index") is not None
    ]
    breadth_pts = _with_index(list(breadth.get("series") or [])[-30:], index_by)
    days = emotion.get("days") or []
    limit_pts = _with_index(
        [{"date": row["date"], "up": row.get("up") or 0, "down": row.get("down") or 0} for row in days],
        index_by,
    )
    stage_pts = _with_index(
        [{"date": row["date"], "stage": row.get("stage"), "up": row.get("up"), "down": row.get("down")} for row in days],
        index_by,
    )
    follow_pts = [
        {"date": row["date"], "value": row["follow"]}
        for row in days if row.get("follow") is not None
    ]
    theme_pts = [
        {"date": row["date"], "value": round(row["theme_share"] * 100, 1)}
        for row in days if row.get("theme_share") is not None
    ]
    # 首页「好不好做」这几条只出结论卡、不画线（与板块今日关注同款）；走势放到「看依据」里。
    no_home_chart = {
        "大盘趋势怎么样？",
        "短线追涨能赚钱吗？",
        "涨停集中在哪几个行业？",
    }
    charts = {
        "大盘趋势怎么样？": (
            {"kind": "line", "unit": "点", "points": trend_pts, "legend": [sh],
             "note": f"近 {len(trend_pts)} 日 · 上证收盘"} if len(trend_pts) >= 3 else None
        ),
        "多数股票在涨吗？": (
            {"kind": "limits", "unit": "家", "points": breadth_pts,
             "legend": [{"t": "上涨", "c": "#D9483F"}, {"t": "下跌", "c": "#2E8B57"}, sh],
             "note": f"近 {len(breadth_pts)} 日 · 上涨 / 下跌家数"} if len(breadth_pts) >= 3 else None
        ),
        "短线追涨能赚钱吗？": (
            {"kind": "line", "unit": "%", "points": follow_pts,
             "legend": [{"t": "昨涨停今日涨跌", "c": "#5B6372"}],
             "note": f"近 {len(follow_pts)} 日 · 昨涨停今日平均 %"} if len(follow_pts) >= 3
            else ({"kind": "limits", "unit": "家", "points": limit_pts,
                   "legend": [{"t": "涨停", "c": "#D9483F"}, {"t": "跌停", "c": "#2E8B57"}, sh],
                   "note": f"近 {len(limit_pts)} 日 · 涨停 / 跌停"} if len(limit_pts) >= 3 else None)
        ),
        "短线情绪走到哪了？": (
            {"kind": "stages", "points": stage_pts, "legend": [sh]} if len(stage_pts) >= 3 else None
        ),
        "成交量怎么样？": (
            {"kind": "bars", "unit": "亿元", "points": vol_pts,
             "legend": [{"t": "上涨日", "c": "#D9483F"}, {"t": "下跌日", "c": "#2E8B57"}, sh],
             "note": f"近 {len(vol_pts)} 日 · 亿元"} if len(vol_pts) >= 3 else None
        ),
        "涨停集中在哪几个行业？": (
            {"kind": "line", "unit": "%", "points": theme_pts,
             "legend": [{"t": "前三行业占比", "c": "#5B6372"}],
             "note": f"近 {len(theme_pts)} 日 · 涨停前三占比"} if len(theme_pts) >= 3 else None
        ),
    }
    for key in no_home_chart:
        charts[key] = None
    for row in qa:
        chart = charts.get(row.get("q") or "")
        if chart:
            row["chart"] = chart


def _tone3(word: str, good: str, bad: str) -> str:
    return "good" if word == good else "bad" if word == bad else "flat"


def _better_worse(parts: dict, mood: dict) -> tuple[list[dict], list[dict]]:
    """完整清单常显：钱脉冲 + 好不好做；勾选表示该项已成立。国家队/板块不进勾选。"""
    turn, etf, margin = parts["turnover"], parts["etf"], parts["margin"]
    indexes = (parts["index"] or {}).get("rows") or []
    etf_label, turn_label, margin_label = etf.get("label"), turn.get("label"), margin.get("label")
    etf_met = etf_label == "净流入"
    turn_ratio = _pct(_stored(turn.get("windows"), 3) if turn.get("windows") else turn.get("ratio5"))
    margin_now = _pct(_stored(margin.get("windows"), 30) if margin.get("windows") else margin.get("ratio4"))
    up, down = (parts.get("breadth") or {}).get("up"), (parts.get("breadth") or {}).get("down")
    share = up / (up + down) if up is not None and down is not None and (up + down) else None
    vote = rules.trend_vote(indexes) or {}
    follow = (parts.get("limits") or {}).get("follow")
    broken_rate = (parts.get("limits") or {}).get("broken_rate")
    chase_ok = follow is not None and broken_rate is not None and follow > 0 and broken_rate < 0.30
    chase_hard = follow is not None and broken_rate is not None and (follow < -0.5 or broken_rate > 0.40)
    chase_now = (
        "现在没有" if follow is None or broken_rate is None
        else f"现在昨涨停 {follow:+.2f}%，炸板率 {round(broken_rate * 100)}%"
    )
    share_now = "现在没有" if share is None else f"现在上涨占 {round(share * 100)}%"
    trend_now = f"现在 {vote['word']}" if vote.get("word") else "现在没有"
    diverged = turn_label in ("正常", "平时") and margin_label == "加速下降"
    broken = [row["name"] for row in indexes if row.get("broke_low")]

    better = [
        {
            "short": "宽基净流入",
            "text": "3 日偏离超近 60 日常态，且 30/60 日同向",
            "met": etf_met,
            "now": f"现在 {_pct(etf.get('ratio'), 2)}",
        },
        {
            "short": "成交回暖",
            "text": "成交离开「冷清」",
            "met": turn_label not in (None, "没有", "冷清"),
            "now": f"现在 {turn_label or '没有'}",
        },
        {
            "short": "解开背离",
            "text": "非「成交正常 + 两融加速下降」",
            "met": not diverged,
            "now": "现在卡住" if diverged else "现在未背离",
        },
        {
            "short": "上涨过半",
            "text": "上涨家数占比 ≥55%",
            "met": share is not None and share >= 0.55,
            "now": share_now,
        },
        {
            "short": "趋势向上",
            "text": "多数主要指数站上 60 日线",
            "met": vote.get("word") == "向上",
            "now": trend_now,
        },
        {
            "short": "追涨赚钱",
            "text": "昨涨停今日为正，且炸板率 <30%",
            "met": chase_ok,
            "now": chase_now,
        },
    ]
    worse = [
        {
            "short": "两融加速",
            "text": "两融 3 日深跌，且 30 日也在降",
            "met": margin_label == "加速下降",
            "now": f"现在 {margin_label or '没有'}",
        },
        {
            "short": "跌破前低",
            "text": "主要指数收在此前 20 日最低之下",
            "met": bool(broken),
            "now": "现在 " + ("、".join(broken) + "已破" if broken else "都没破"),
        },
        {
            "short": "成交冷清",
            "text": "成交判为「冷清」",
            "met": turn_label == "冷清",
            "now": f"现在 {turn_label or '没有'}",
        },
        {
            "short": "广度偏弱",
            "text": "上涨家数占比 ≤40%",
            "met": share is not None and share <= 0.40,
            "now": share_now,
        },
        {
            "short": "趋势向下",
            "text": "指数趋势判为向下",
            "met": vote.get("word") == "向下",
            "now": trend_now,
        },
        {
            "short": "价钱同弱",
            "text": "宽基净流出，且大盘趋势向下",
            "met": etf_label == "净流出" and vote.get("word") == "向下",
            "now": (
                f"现在宽基 {etf_label or '没有'}，趋势 {vote.get('word') or '没有'}"
            ),
        },
        {
            "short": "追涨变难",
            "text": "昨涨停今日 <−0.5%，或炸板率 >40%",
            "met": chase_hard,
            "now": chase_now,
        },
    ]
    return better, worse


# ---------------------------------------------------------------- 宏观
def _macro_card(parts: dict) -> dict:
    macro, valuation = parts["macro"], parts["valuation"]
    history = macro.get("history") or []
    liquidity, liquidity_text = rules.liquidity_score(history)
    profit, profit_text = rules.profit_score(history)
    value, value_text = rules.value_score(valuation.get("pctile"))
    hit = market_team.policy_hit((parts.get("announce") or {}).get("items") or [], datetime.now())
    policy = 5 if hit else 3
    policy_text = (f"{hit['date']} {hit['title']}" if hit else
                   ("近 7 天没有筛到降准降息或汇金增持的官方标题" if (parts.get("announce") or {}).get("ok") else "公告新闻没取到，暂按 3 分"))
    scores = {"policy": policy, "liquidity": liquidity, "profit": profit, "value": value}
    direction = rules.macro_direction(scores)
    names = {"liquidity": "钱活不活", "profit": "企业赚钱", "value": "估值"}
    strong = [names[key] for key in ("profit", "value") if scores[key] >= 5]
    weak = [names[key] for key in ("liquidity", "profit", "value") if scores[key] <= 1]
    head = ("、".join(strong) + "有支撑" if strong else "") + ("，但" if strong and weak else "") + ("、".join(weak) + "偏弱" if weak else "")
    fx, us, hk = parts["fx"], parts["us"], parts["hk"]
    external = [
        {"name": "人民币", "word": rules.trend_word(fx.get("change5"), "走弱", "走强"), "detail": f"美元兑离岸人民币 {_plain(fx.get('close'), 4)}"},
        {"name": "美债 10Y", "word": rules.trend_word(us.get("change5"), "上行", "下行", 0.02), "detail": f"收益率 {_plain(us.get('y10'))}%"},
        {"name": "恒生科技", "word": rules.trend_word(hk.get("change5"), "走强", "走弱", 0.005), "detail": f"{_plain(hk.get('close'))}，近 5 日 {_pct(hk.get('change5'))}"},
    ]
    month = macro.get("month") or ""
    rows = [
        {"key": "policy", "name": "政策", "text": policy_text, "age": "近 7 天", "score": policy, "weight": 0.35,
         "url": (hit or {}).get("url", "")},
        {"key": "liquidity", "name": "流动性", "text": liquidity_text, "age": "上月", "score": liquidity, "weight": 0.25},
        {"key": "profit", "name": "盈利", "text": profit_text, "age": "上月", "score": profit, "weight": 0.20},
        {"key": "value", "name": "估值", "text": value_text, "age": "近 3 年" if (valuation.get("days") or 0) >= 700 else "样本内", "score": value, "weight": 0.20},
    ]
    return {
        "tier": direction["tier"], "label": {"顺": "顺", "平": "平", "逆": "逆"}[direction["tier"]],
        "tone": {"顺": "good", "平": "warn", "逆": "bad"}[direction["tier"]],
        "score": direction["score"], "headline": head or "各项没有明显偏向",
        "rows": rows, "external": external, "month": month,
        "formula": "政策 {p} × 35% + 流动性 {a} × 25% + 盈利 {b} × 20% + 估值 {c} × 20% = {t:.2f}".format(
            p=policy, a=liquidity, b=profit, c=value, t=direction["score"]),
        "next": "下次发布：下月的 PPI 和 M1 / M2（通常次月上中旬）",
        "scope": "1 / 3 / 5 分按规则速查；方向分是位置不是预测；政策看新闻搜索里的官方标题，可能有遗漏；估值用沪深 300 盈利收益率的 3 年分位，不是 ERP",
        "asof_note": "宏观是月频（M1/M2/PPI 等），标的是数据所属月份，不是交易日",
        "metrics": {
            "m1": macro.get("m1_yoy"), "m2": macro.get("m2_yoy"), "ppi": macro.get("ppi_yoy"), "sf": macro.get("sf_month"),
            "pe": valuation.get("pe"), "earn_yield": valuation.get("earn_yield"), "value_pctile": valuation.get("pctile"),
        },
    }


# ---------------------------------------------------------------- 日历
def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def _calendar_card(parts: dict, watch: list[dict], today: date) -> dict:
    window_end = today + timedelta(days=14)
    rows: list[dict] = []
    for item in (parts["calendar"] or {}).get("events") or []:
        tag = "数据" if "中国" in item["name"][:2] else "外部"
        rows.append({"date": item["date"], "text": item["name"], "tag": tag})
    for item in (parts["ipo"] or {}).get("days") or []:
        rows.append({"date": item["date"], "text": f"{item['count']} 只新股申购", "tag": "IPO"})
    for month in (today.month, today.month % 12 + 1):
        year = today.year + (1 if month < today.month else 0)
        delivery = _third_friday(year, month)
        if today <= delivery <= window_end:
            rows.append({"date": delivery.isoformat(), "text": "股指期货交割（遇休市顺延）", "tag": "交割"})
    unlock = parts.get("unlock") or {}
    if unlock.get("count"):
        rows.append({
            "date": today.isoformat(),
            "text": f"未来 10 天解禁 {unlock['count']} 条，合计 {unlock['total']:.2f} 亿股" + ("（单次最多 6000 条，没算完）" if unlock.get("capped") else ""),
            "tag": "解禁",
        })
    mine = list(parts["personal"]) if "personal" in parts else _personal(watch, today)
    rows.extend(mine)
    rows.sort(key=lambda row: (row["date"], row["tag"] != "涉及自选"))
    for row in rows:
        day = date.fromisoformat(row["date"])
        row["weekday"] = "周" + _WEEKDAY[day.weekday()]
        row["day"] = row["date"][5:]
    dense = len(rows) >= 6
    return {
        "label": "事件偏密" if dense else "事件正常", "tone": "warn" if dense else "flat",
        "rows": rows[:8], "mine_count": len(mine), "unlock": unlock,
        "scope": "解禁按股数，没按市值；交割日按每月第三个周五，遇休市顺延；只提示，不改档位",
        "date": today.isoformat(),
        "asof_note": "按今天日历筛未来两周事件，日期就是打开页面的当天",
    }


_PERIOD_NAME = {"0331": "一季报", "0630": "半年报", "0930": "三季报", "1231": "年报"}


def _personal(watch: list[dict], today: date) -> list[dict]:
    return _my_unlocks(watch, today) + _my_disclosures(watch, today)


def _my_disclosures(watch: list[dict], today: date) -> list[dict]:
    """自选股未来两周的定期报告披露日（预约日期）。"""
    if not watch:
        return []
    from app.services import market_tape

    periods = [f"{today.year - 1}1231", f"{today.year}0331", f"{today.year}0630", f"{today.year}0930", f"{today.year}1231"]
    end = (today + timedelta(days=14)).isoformat()
    jobs = [(row, period) for row in watch for period in periods]

    def one(job: tuple[dict, str]) -> list[dict]:
        row, period = job
        try:
            rows = market_tape.cached(
                f"discl:{row['ts_code']}:{period}",
                lambda c=row["ts_code"], p=period: market_tape.pro().disclosure_date(end_date=p, ts_code=c),
            )
        except Exception:
            return []
        found = []
        for item in rows:
            when = market_tape.iso(item.get("actual_date") or item.get("pre_date"))
            if when and today.isoformat() <= when <= end:
                found.append({"date": when, "text": f"{row['name']} {_PERIOD_NAME.get(period[4:], '定期报告')}披露", "tag": "涉及自选"})
        return found

    out = []
    for found in market_tape._parallel(jobs, one):
        out.extend(found)
    return out


def _my_unlocks(watch: list[dict], today: date) -> list[dict]:
    if not watch:
        return []
    from app.services import market_tape

    names = {row["ts_code"]: row["name"] for row in watch}
    out = []
    try:
        rows = market_tape.unlock_rows(today)
    except Exception:
        return []
    merged: dict[tuple[str, str], float] = {}
    for row in rows:
        code = str(row.get("ts_code") or "")
        if code not in names:
            continue
        key = (code, market_tape.iso(row.get("float_date")))
        merged[key] = merged.get(key, 0.0) + (market_tape.num(row.get("float_ratio")) or 0.0)
    for (code, day), ratio in merged.items():
        out.append({"date": day, "text": f"{names[code]} 解禁，占总股本 {ratio:.2f}%", "tag": "涉及自选"})
    return out


# ---------------------------------------------------------------- 板块
BUCKET_ORDER = [item[0] for item in rules.BUCKETS]


BOARD_CAP = 8


def _mz_plain(value, kind: str = "money") -> str:
    """Mz 白话：差不多 / 偏进·偏出·偏强·偏弱 / 明显…，并带上数字。"""
    if value is None:
        return "—"
    num = float(value)
    tip = _signed(num, 1, "")
    abs_n = abs(num)
    if kind == "price":
        if abs_n < 1:
            return f"跟大盘差不多 · {tip}"
        if abs_n > 3:
            return f"比大盘明显{'强' if num > 0 else '弱'} · {tip}"
        return f"比大盘{'强' if num > 0 else '弱'} · {tip}"
    if abs_n < 1:
        return f"资金进出一般 · {tip}"
    if abs_n > 3:
        return f"资金明显在{'进' if num > 0 else '出'} · {tip}"
    return f"资金偏{'进' if num > 0 else '出'} · {tip}"


def _heat_plain(heat) -> str:
    if heat is None:
        return "热度 —"
    text = f"{float(heat):.2f}×"
    if float(heat) >= rules.SECTOR_HOT:
        return f"放量 {text}"
    if float(heat) <= rules.SECTOR_COLD:
        return f"偏冷 {text}"
    return f"不热 {text}"


def _money_enter_of(row: dict) -> float:
    return rules.MONEY_ENTER if row.get("source", "etf") == "etf" else rules.MONEY_ENTER_BASE


def _sector_board_item(row: dict) -> dict:
    """双榜一行：结论字段 + Mz/热度白话，前端直接展示。"""
    up = int(row.get("up_days") or 0)
    ret = row.get("ret5")
    weak = ret is not None and float(ret) < 0
    hot = row.get("heat") is not None and float(row["heat"]) >= rules.SECTOR_HOT
    return {
        "name": row["name"],
        "source": row.get("source", "etf"),
        "up_days": up,
        "up_text": f"连涨 {up} 日" if up >= 2 else (f"连涨 {up} 日" if up == 1 else "未连涨"),
        "ret5": ret,
        "weak_price": weak,
        "money_z": row.get("money_z"),
        "price_z": row.get("price_z"),
        "flow": row.get("flow"),
        "flow_text": row.get("flow_text") or _yi(row.get("flow"), True),
        "heat": row.get("heat"),
        "heat_text": _heat_plain(row.get("heat")),
        "hot": hot,
        "money_tip": _mz_plain(row.get("money_z"), "money"),
        "price_tip": _mz_plain(row.get("price_z"), "price"),
        "phase": row.get("phase") or "",
        "started": bool(row.get("started")),
        "fading": bool(row.get("fading")),
    }


def _sector_rising_ok(row: dict, enter: float) -> bool:
    """在攻：资金进，且连涨≥2 / 相对大盘偏强 / 放量且近几日不明显跌。"""
    mz = row.get("money_z")
    if mz is None or float(mz) < enter:
        return False
    up = int(row.get("up_days") or 0)
    if up >= 2:
        return True
    pz = row.get("price_z")
    if pz is not None and float(pz) >= rules.PRICE_ENTER:
        return True
    heat = row.get("heat")
    ret = row.get("ret5")
    hot = heat is not None and float(heat) >= rules.SECTOR_HOT
    not_down = ret is None or float(ret) >= PICK_RET_FLOOR
    return hot and not_down


def _sector_boards(rows: list[dict]) -> dict:
    """在攻 / 在出（钱），以及涨 / 跌（自身近窗涨跌）。不凑数补候补。"""
    rising, exiting, up, down = [], [], [], []
    for row in rows:
        mz = row.get("money_z")
        if mz is not None:
            enter = _money_enter_of(row)
            if _sector_rising_ok(row, enter):
                rising.append(row)
            if float(mz) <= -enter:
                exiting.append(row)
        ret = row.get("ret5")
        if ret is None:
            continue
        if float(ret) > 0:
            up.append(row)
        elif float(ret) < 0:
            down.append(row)
    rising.sort(key=lambda row: (
        0 if (row.get("heat") is not None and float(row["heat"]) >= rules.SECTOR_HOT) else 1,
        -(float(row["money_z"])),
        -int(row.get("up_days") or 0),
    ))
    exiting.sort(key=lambda row: float(row["money_z"]))
    up.sort(key=lambda row: -float(row["ret5"]))
    down.sort(key=lambda row: float(row["ret5"]))
    return {
        "rising": [_sector_board_item(row) for row in rising[:BOARD_CAP]],
        "exiting": [_sector_board_item(row) for row in exiting[:BOARD_CAP]],
        "up": [_sector_board_item(row) for row in up[:BOARD_CAP]],
        "down": [_sector_board_item(row) for row in down[:BOARD_CAP]],
    }


def _sector_rows(sectors: dict, catalysts: dict | None = None) -> list[dict]:
    """每个板块一行，按「钱 → 价 → 热度」的分组排序：同一组里钱进得越异常越靠前。"""
    rows = []
    for row in sectors.get("rows") or []:
        flow = row["flow"].get(sectors.get("conclusion_days", 5))
        phase = str(row.get("phase") or "")
        rows.append({
            "name": row["name"], "code": row["code"], "industry": row["industry"], "group": row.get("group", ""), "source": row.get("source", "etf"),
            "flow": flow, "flow_text": _yi(flow, True), "flows": {str(key): value for key, value in row["flow"].items()},
            "flow_rate": row.get("flow_rate"), "size": row.get("size"), "money_z": row.get("money_z"),
            "price_z": row.get("price_z"), "up_days": int(row.get("up_days") or 0),
            "mz_hist": row.get("mz_hist") or [], "price_z_hist": row.get("price_z_hist") or [],
            "ret5": row.get("ret5"), "ret20": row.get("ret20"), "ex5": row.get("ex5"), "ex20": row.get("ex20"),
            "heat": row["heat"], "heat_text": "—" if row["heat"] is None else f"{row['heat']:.2f}×", "position": row.get("position"),
            "lever": row["lever"], "lever_word": row["lever_word"],
            "verdict": row["verdict"], "tone": row["tone"], "phase": phase, "bucket": row.get("bucket", "flat"), "why": row.get("why", ""),
            "weather": row.get("weather", ""), "started": bool(row.get("started")), "fading": bool(row.get("fading")),
            "meaning": row.get("meaning") or rules.MEANING.get(phase.split("·")[0] if phase != "主升·待资金" else "主升·待资金", ""),
            "catalyst": (catalysts or {}).get(row["name"]),
            "money_tip": _mz_plain(row.get("money_z"), "money"),
            "price_tip": _mz_plain(row.get("price_z"), "price"),
        })
    rows.sort(key=lambda item: (BUCKET_ORDER.index(item["bucket"]) if item["bucket"] in BUCKET_ORDER else 99, -(item["money_z"] if item["money_z"] is not None else -99)))
    return rows


def _sector_buckets(rows: list[dict]) -> list[dict]:
    return [{"key": key, "title": title, "tone": tone, "names": [row["name"] for row in rows if row["bucket"] == key]} for key, title, tone in rules.BUCKETS
            if any(row["bucket"] == key for row in rows)]


# 近几日涨跌（ret5 为小数）低于此不进「今日关注」；dip 永不进推荐。
# 另拦：近几日净额在出、钱 Mz 弱于维持门槛——避免「相位还叫净申购先到、账上已在赎回」。
PICK_RET_FLOOR = -0.01
PICK_MONEY_Z_FLOOR = rules.MONEY_HOLD  # 0.5
PICKS_CAP = 5
DIP_WATCH_CAP = 3


def _sector_pick_ok(row: dict) -> bool:
    """可进推荐：价不明显跌、近几日净额不在出、钱 Mz 够维持门槛。"""
    ret = row.get("ret5")
    if ret is not None and float(ret) < PICK_RET_FLOOR:
        return False
    flow = row.get("flow")
    if flow is not None and float(flow) < 0:
        return False
    mz = row.get("money_z")
    if mz is not None and float(mz) < PICK_MONEY_Z_FLOOR:
        return False
    return True


def _pick_item(row: dict, tier: str, label: str, reason: str) -> dict:
    return {
        "name": row["name"],
        "tier": tier,
        "label": label,
        "reason": reason,
        "bucket": row.get("bucket"),
        "phase": row.get("phase") or "",
        "money_z": row.get("money_z"),
        "ret5": row.get("ret5"),
        "flow": row.get("flow"),
        "heat": row.get("heat"),
    }


def _sector_picks(rows: list[dict]) -> list[dict]:
    """今日关注：可跟(main) → 可盯(next)；过滤价跌/净流出/弱 Mz；最多 PICKS_CAP 个。"""
    follow = [row for row in rows if row.get("bucket") == "main" and _sector_pick_ok(row)]
    watch = [row for row in rows if row.get("bucket") == "next" and _sector_pick_ok(row)]
    out: list[dict] = []
    for tier, label, reason, pool in (
        ("follow", "可跟", "价热已确认", follow),
        ("watch", "可盯", "净申购先到，等确认", watch),
    ):
        for row in pool:
            if len(out) >= PICKS_CAP:
                return out
            out.append(_pick_item(row, tier, label, reason))
    return out


def _sector_dip_watch(rows: list[dict]) -> list[dict]:
    """左侧观察：dip（价跌钱进），最多 DIP_WATCH_CAP 个；不进推荐。"""
    dips = [row for row in rows if row.get("bucket") == "dip"]
    return [
        _pick_item(row, "dip", "左侧", "价跌钱进，等止跌")
        for row in dips[:DIP_WATCH_CAP]
    ]


def _join_names(items: list, n: int = 3) -> str:
    names = []
    for item in items[:n]:
        names.append(item["name"] if isinstance(item, dict) else str(item))
    return "、".join(names)


def _sector_headline(rows: list[dict]) -> str:
    """板块摘要：值得跟踪(=今日关注) / 在攻未进推荐 / 偏危险 / 可能启动或在退。"""
    if not rows:
        return "没有取到板块数据"
    boards = _sector_boards(rows)
    picks = _sector_picks(rows)
    pick_names = {item["name"] for item in picks}
    started = sorted(
        [row for row in rows if row.get("started")],
        key=lambda row: -(row.get("money_z") if row.get("money_z") is not None else -99),
    )
    fading = sorted(
        [row for row in rows if row.get("fading")],
        key=lambda row: (row.get("money_z") if row.get("money_z") is not None else 99),
    )
    # 在攻但不在今日关注：相位往往是高位分歧/换手等，量价齐了仍不推荐追
    rising_only = [item for item in boards["rising"] if item["name"] not in pick_names]
    danger = [item for item in boards["exiting"] if item.get("weak_price") or item.get("hot")]
    if not danger:
        danger = boards["exiting"]
    bits = []
    if picks:
        bits.append("值得跟踪：" + _join_names(picks, 3))
    if rising_only:
        bits.append("在攻未进推荐：" + _join_names(rising_only, 3))
    if danger:
        bits.append("偏危险：" + _join_names(danger, 3))
    if started:
        bits.append("可能启动：" + _join_names(started, 3))
    elif fading:
        bits.append("可能在退：" + _join_names(fading, 3))
    if bits:
        return "；".join(bits)
    return "今天没有明显可跟的强势板块，也没有极端撤出；轮动不鲜明，先别急着追。"


def _sector_card_fields(rows: list[dict], parts: dict) -> dict:
    """板块卡共用字段：在攻/在出双榜 + 今日关注 + 左侧观察。"""
    boards = _sector_boards(rows)
    return {
        "rows": rows,
        "date": parts["sectors"].get("date") or "",
        "headline": _sector_headline(rows),
        "boards": boards,
        "buckets": _sector_buckets(rows),
        "picks": _sector_picks(rows),
        "dip_watch": _sector_dip_watch(rows),
        "coverage": parts["sectors"].get("coverage"),
        "conclusion_days": parts["sectors"].get("conclusion_days") or market_sector.CONCLUSION_DAYS,
        "scope": SECTOR_SCOPE,
    }


# ---------------------------------------------------------------- 国家队
# 出手、钱还在、回落、可能在撤、已表态，都只展示，不进总档
_TEAM_TONE = {
    "托市中": "good", "异动中": "warn", "钱还在": "flat", "部分回落": "warn", "可能在撤": "bad",
    "没托市": "flat", "已表态": "warn", "还没回补": "flat", "部分回补": "warn", "明显回补": "good",
}


def _team_ready(team: dict, announce: dict | None) -> dict:
    if not team.get("ok"):
        return team
    return market_team.apply_stance(team, (announce or {}).get("items") or [])


def _team_signal(team: dict, announce: dict | None = None) -> dict:
    if not team.get("ok"):
        return {"key": "team", "n": "②", "name": "国家队", "tier": None, "label": "没取到", "tone": "flat", "text": team.get("text") or "份额数据没取到，不参与总档位"}
    team = _team_ready(team, announce)
    return {"key": "team", "n": "②", "name": "国家队", "tier": "平", "label": team["label"], "tone": _TEAM_TONE.get(team["label"], "flat"), "text": _team_headline(team)}


def _signed(value: float, digits: int = 1, suffix: str = "%") -> str:
    return f"{value:+.{digits}f}{suffix}".replace("-", "−")


def _md(day: str | None) -> str:
    return (day or "")[5:]


def _wave_when(last: dict, name: str) -> str:
    """单日写成「10-15 托市」；并在一起的写成「10-15 到 11-06 这一波托市」。"""
    start, until = last.get("day"), last.get("until") or last.get("day")
    if until and until != start and (last.get("span") or 1) > 1:
        return f"{_md(start)} 到 {_md(until)} 这一波{name}"
    return f"{_md(start)} {name}"


def _wave_peak(last: dict) -> str:
    peak_day, peak_delta = last.get("peak_day"), last.get("peak_delta")
    if not peak_day or peak_delta is None or (last.get("span") or 1) <= 1:
        return ""
    way = "赎回" if peak_delta < 0 else "申购"
    return f"{way}最多的一天是 {_md(peak_day)}，约 {abs(peak_delta):.0f} 亿元。"


def _team_start_bit(last: dict) -> str:
    """起点行情：把「大跌/连跌」和上证涨跌合成一句，避免重复。"""
    move = last.get("index_pct")
    kind = last.get("kind") or ""
    if move is None:
        return {"大跌": "起点当天大跌。", "连跌": "起点处在连续下跌中。",
                "承接": "前两天刚在跌。", "位置": "距高点回撤较深。"}.get(kind, "")
    move_txt = f"{'跌' if move < 0 else '涨'} {abs(move):.1f}%"
    if kind == "大跌":
        return f"起点当天上证大跌 {abs(move):.1f}%。"
    if kind == "连跌":
        return f"起点处在连续下跌中，当天上证{move_txt}。"
    if kind == "承接":
        return f"前两天刚在跌，起点当天上证{move_txt}。"
    if kind == "位置":
        return f"距高点回撤较深，起点当天上证{move_txt}。"
    return f"起点当天上证{move_txt}。"


def _team_event(last: dict) -> str:
    """一次托市或异动：哪一波、确认日、起点行情、最大一天、涉及哪些宽基。"""
    groups = "、".join(last.get("groups") or [])
    found = f"{_md(last['found'])} 份额确认。" if last.get("found") else ""
    name = "托市" if last.get("state") == "托市" else ("买入异动" if last.get("side") != "sell" else "卖出异动")
    peak = _wave_peak(last)
    if peak:
        money = peak
    elif last.get("delta") is not None:
        money = f"约 {abs(last['delta']):.0f} 亿元。"
    else:
        money = ""
    group_bit = f"涉及 {groups}。" if groups else ""
    return f"{_wave_when(last, name)}。{found}{_team_start_bit(last)}{money}{group_bit}"


def _team_headline(team: dict) -> str:
    last, hold, sell = team.get("last"), team.get("hold"), team.get("sell")
    word = team.get("confirm_word") or "疑似托市"
    if team["label"] == "托市中" and last:
        peak = last.get("peak_delta") if (last.get("span") or 1) > 1 else last.get("delta")
        day_bit = _wave_when(last, word)
        money = f"{len(last.get('groups') or [])} 组宽基明显申购，最大一天约 {abs(peak or 0):.0f} 亿元。" if (last.get("span") or 1) > 1 else f"{len(last.get('groups') or [])} 组宽基明显申购，合计 {last['delta']:.0f} 亿元。"
        return f"{day_bit}：{money}之后 5 个交易日标「托市中」，今天是第 {last['since'] + 1} 天"
    if team["label"] == "异动中" and last:
        way = "卖出" if last.get("side") == "sell" else "买入"
        peak = last.get("peak_delta") if (last.get("span") or 1) > 1 else last.get("delta")
        day_bit = _wave_when(last, word)
        money = f"市场没有承压，{len(last.get('groups') or [])} 组宽基出现{way}异动，最大一天约 {abs(peak or 0):.0f} 亿元。" if (last.get("span") or 1) > 1 else f"市场没有承压，{len(last.get('groups') or [])} 组宽基出现{way}异动，合计 {last['delta']:.0f} 亿元。"
        return f"{day_bit}：{money}之后 5 个交易日标「异动中」，今天是第 {last['since'] + 1} 天"
    if team["label"] == "可能在撤":
        if sell and sell.get("sign1"):
            bits = []
            pos = sell.get("pos_pctile")
            if pos is not None:
                bits.append(f"指数近 60 日位置约 {pos * 100:.0f}%")
            bits.append("宽基近 5 日份额显著净减")
            if sell.get("confirm_gone") and hold and hold.get("gone") is not None:
                bits.append(f"托市后份额回落 {hold['gone'] * 100:.0f}% 作确认")
            return "；".join(bits) + "。可能是国家队在减，也可能是散户赎回"
        if sell and sell.get("sign2"):
            return "不跌的日子里，宽基被广泛赎回。可能是国家队在减，也可能是散户赎回"
        return "可能在撤。只看份额，分不清是国家队还是别的资金"
    if team["label"] == "钱还在" and hold and hold.get("gone") is not None:
        if hold["gone"] <= 0:
            return "上次托市之后，份额没有回落"
        return f"上次托市的份额只回落 {hold['gone'] * 100:.0f}%，还在"
    if team["label"] == "部分回落" and hold and hold.get("gone") is not None:
        gap = hold.get("gap_yi")
        tail = "" if gap is None else f"，离警戒线还差 {gap:.0f} 亿份"
        return f"已回落 {hold['gone'] * 100:.0f}%{tail}"
    if team["label"] == "还没回补":
        return "上次卖出之后，份额还没买回来"
    if team["label"] == "部分回补" and hold and hold.get("gone") is not None:
        return f"上次卖出的份额回来了 {hold['gone'] * 100:.0f}%"
    if team["label"] == "明显回补" and hold and hold.get("gone") is not None:
        return f"上次卖出的份额已经回来 {hold['gone'] * 100:.0f}%"
    if team["label"] == "已表态":
        return "汇金已发公告，尚未看到资金进场"
    if last:
        name = "托市" if last.get("state") == "托市" else "异动"
        return f"近一段没有新的托市。上一次是 {_wave_when(last, name)}"
    return "近一年没有出现承压托市"


def _team_brief(team: dict) -> str:
    last = team.get("last")
    if team["label"] in ("托市中", "异动中") and last:
        word = team.get("confirm_word") or team["label"]
        return f"{_wave_when(last, word)}，今天是第 {last['since'] + 1} 天"
    if team["label"] == "已表态":
        return "已表态，尚未看到资金进场"
    if last and team["label"] == "没托市":
        name = "托市" if last.get("state") == "托市" else "异动"
        return f"最近没托市。上一次是 {_wave_when(last, name)}"
    return team["label"]


def _team_lead(team: dict) -> str:
    last, hold = team.get("last"), team.get("hold")
    if team["label"] in ("托市中", "异动中") and last:
        return _team_event(last) + "。不进总档。"
    if team["label"] == "可能在撤":
        return _team_headline(team) + "。这一条只展示，不进总档。"
    if hold and hold.get("gone") is not None and team["label"] in ("钱还在", "部分回落", "还没回补", "部分回补", "明显回补"):
        return _team_headline(team) + "。"
    return ""


def _window_facts(windows: dict | None) -> list[dict]:
    """1/3/5/30/60 日宽基偏离。超过 3 偏买，低于 −3 偏卖，中间是和平时差不多。"""
    windows = windows or {}
    facts = []
    for n in (1, 3, 5, 30, 60):
        value = windows.get(n)
        if value is None:
            value = windows.get(str(n))
        if value is None:
            facts.append({"k": f"{n} 日", "v": "—", "tone": "flat"})
        else:
            tone = "good" if value > 3 else "bad" if value < -3 else "flat"
            facts.append({"k": f"{n} 日", "v": _signed(value, 1, ""), "tone": tone})
    return facts


def _last_facts(last: dict | None) -> list[dict]:
    if not last:
        return []
    span = last.get("span") or 1
    until = last.get("until") or last.get("day")
    if span > 1 and until and until != last.get("day"):
        facts = [{"k": "这一波", "v": f"{_md(last['day'])}～{_md(until)}", "tone": "flat"}]
        if last.get("peak_day"):
            facts.append({"k": "最大一天", "v": _md(last["peak_day"]), "tone": "flat"})
    else:
        day_name = "托市日" if last.get("state") == "托市" else "异动日"
        facts = [{"k": day_name, "v": _md(last["day"]), "tone": "flat"}]
    if last.get("found"):
        facts.append({"k": "确认日", "v": _md(last["found"]), "tone": "flat"})
    if span <= 1 and last.get("index_pct") is not None:
        facts.append({"k": "上证", "v": _signed(last["index_pct"]), "tone": "bad" if last["index_pct"] < 0 else "good"})
    if last.get("groups"):
        facts.append({"k": "达标", "v": f"{len(last['groups'])} 组", "tone": "good" if last.get("side") != "sell" else "bad"})
    money = last.get("peak_delta") if span > 1 and last.get("peak_delta") is not None else last.get("delta")
    if money is not None:
        facts.append({"k": "净申购", "v": f"{money:+.0f} 亿".replace("-", "−"), "tone": "good" if money > 0 else "bad"})
    return facts


def _hold_facts(hold: dict | None, compact: bool = False) -> list[dict]:
    if not hold or hold.get("gone") is None:
        return []
    gap = hold.get("gap_yi")
    line, now = hold.get("line_yi"), hold.get("now_yi")
    change = "回补" if hold.get("side") == "sell" else "回落"
    hard = hold.get("hard") if hold.get("hard") is not None else 0.3
    head = {"k": change, "v": f"{hold['gone'] * 100:.0f}%", "tone": "bad" if hold["gone"] > hard and change == "回落" else "flat"}
    if compact:
        # 主卡一行摘要：回落/回补 + 现在；警戒线/还差留在 detail
        facts = [head]
        if now is not None:
            facts.append({"k": "现在", "v": f"{now:.0f} 亿份", "tone": "flat"})
        return facts
    return [
        head,
        {"k": "警戒线", "v": "—" if line is None else f"{line:.0f} 亿份", "tone": "flat"},
        {"k": "现在", "v": "—" if now is None else f"{now:.0f} 亿份", "tone": "flat"},
        {"k": "还差", "v": "已过线" if gap is not None and gap < 0 else ("" if gap is None else f"{gap:.0f} 亿份"), "tone": "flat"},
    ]


def _sell_facts(sell: dict | None, compact: bool = False) -> list[dict]:
    if not sell:
        return []
    facts = []
    if sell.get("sign1"):
        pos = sell.get("pos_pctile")
        tip = "高位+近 5 日短减" if pos is None else f"高位 {pos * 100:.0f}%+近 5 日短减"
        facts.append({"k": "迹象一", "v": tip, "tone": "bad"})
    if sell.get("sign2"):
        facts.append({"k": "迹象二", "v": f"近 3 日 {sell.get('days') or 0} 天大额赎回", "tone": "bad"})
    if compact and len(facts) > 1:
        return [{"k": "迹象", "v": "高位短减 · 近 3 日大额赎回", "tone": "bad"}]
    return facts


def _out_summary_facts(team: dict) -> list[dict]:
    """有没有在撤：每日口径（宽基份额），即使暂时没有也展示。"""
    pulse = team.get("out_pulse") or {}
    sell = team.get("sell")
    hold = team.get("hold")
    if sell:
        return _sell_facts(sell, compact=True)
    facts = []
    today = team.get("today_broad")
    if today is None and pulse.get("today_delta") is not None:
        today = pulse["today_delta"]
    if today is not None:
        facts.append({
            "k": "当日宽基净申购",
            "v": f"{today:+.0f} 亿".replace("-", "−"),
            "tone": "bad" if today < 0 else "good" if today > 0 else "flat",
        })
    hit = pulse.get("hit_days")
    need = pulse.get("need_days") or 2
    if hit is not None:
        # 避免 0/2 难读：写成「0 天（要满 2 天）」
        facts.append({
            "k": "近 3 日赎回日",
            "v": f"{hit} 天（要满 {need} 天）",
            "tone": "bad" if hit >= need else "flat",
        })
    elif hold and hold.get("gone") is not None and hold.get("side") != "sell":
        hard = hold.get("hard") if hold.get("hard") is not None else 0.3
        facts.append({
            "k": "相对高点回落",
            "v": f"{hold['gone'] * 100:.0f}%",
            "tone": "bad" if hold["gone"] > hard else "flat",
        })
    return facts[:2]


def _act_summary_facts(last: dict | None, team: dict) -> list[dict]:
    """主卡托市行只留净申购；波段细节进答案文案 / 依据页。"""
    money = None
    if last:
        span = last.get("span") or 1
        money = last.get("peak_delta") if span > 1 and last.get("peak_delta") is not None else last.get("delta")
    if money is None and team.get("today_broad") is not None:
        money = team["today_broad"]
    if money is None:
        return []
    return [{"k": "净申购", "v": f"{money:+.0f} 亿".replace("-", "−"), "tone": "good" if money > 0 else "bad" if money < 0 else "flat"}]


# 依据页折叠块里的「怎么判定」：只留普通人能扫完的短句；细则进 RULES.team 问号。
_TEAM_ACT_RULE = (
    "大盘在跌、钱主要进宽基、至少两组买得不像平时，同一天碰上 → 疑似托市。"
    "大盘没在跌、但宽基突然大买或大卖 → 异动（不是托市）。"
    "同方向且隔不超过 3 个交易日的算同一波。都不改市场总档。更细规则点标题问号。"
)
_TEAM_HOLD_RULE = (
    "看上次出手后，那几组宽基的份额从高点掉了多少（若是卖出异动，则看从低点回来了多少）。"
    "档位按该组自身日常波动定：掉得少＝钱还在；掉过自身尺度＝部分回落。"
    "累计回落只作确认，单独再大也不直接判「可能在撤」。这是所有人申赎相抵后的净额。"
)
_TEAM_OUT_RULE = (
    "两种迹象：① 指数近 60 日处在相对高位，且宽基近 5 日份额相对自身常态显著净减；"
    "② 近 3 天里至少 2 天，大盘没大跌（跌幅不超过 1%），却有至少 3 组宽基明显赎回。"
    "托市后份额回落只作交叉确认。两种主迹象都有 → 迹象较强。只展示，不改市场总档。"
)


def _window_num(value) -> float | None:
    try:
        return float(str(value).replace("−", "-").replace("+", ""))
    except (TypeError, ValueError):
        return None


def _market_down_long(series: list[dict] | None, pressure: str = "") -> bool:
    """大盘跌得较久：连跌 ≥3 日，或近 10 日里至少 6 日下跌，或深回撤承压（位置）。"""
    if pressure == "位置":
        return True
    streak = 0
    for row in reversed(series or []):
        pct = row.get("index_pct")
        if pct is None:
            break
        if pct < 0:
            streak += 1
        else:
            break
    if streak >= 3:
        return True
    recent = [row.get("index_pct") for row in (series or [])[-10:] if row.get("index_pct") is not None]
    return len(recent) >= 8 and sum(1 for x in recent if x < 0) >= 6


def _cum_ret_from_pct_chg(pcts: list[float], n: int) -> float | None:
    """近 n 日复利累计（小数）。pcts 为 Tushare pct_chg 百分数，如 -1.2 表示 −1.2%。"""
    if n <= 0 or len(pcts) < n:
        return None
    acc = 1.0
    for p in pcts[-n:]:
        acc *= 1.0 + p / 100.0
    return acc - 1.0


def _index_streak_bits(series: list[dict] | None) -> dict:
    """上证连涨/连跌天数 + 近 3/5/10 日累计；缺序列不编造。

    series[].index_pct 与成交额切片一致，单位是 pct_chg 百分数（−1.2 = −1.2%）。
    """
    pcts: list[float] = []
    for row in series or []:
        pct = row.get("index_pct")
        if pct is not None:
            pcts.append(float(pct))
    if not pcts:
        return {"streak": 0, "side": "", "text": "", "window": 5, "cum": {}}
    last = pcts[-1]
    if last > 0:
        side, streak = "up", 0
        for p in reversed(pcts):
            if p > 0:
                streak += 1
            else:
                break
    elif last < 0:
        side, streak = "down", 0
        for p in reversed(pcts):
            if p < 0:
                streak += 1
            else:
                break
    else:
        side, streak = "flat", 0
    cums: dict[int, float] = {}
    for n in (3, 5, 10):
        got = _cum_ret_from_pct_chg(pcts, n)
        if got is not None:
            cums[n] = got
    if streak >= 2:
        got = _cum_ret_from_pct_chg(pcts, streak)
        if got is not None:
            cums[streak] = got
    bits: list[str] = []
    if side == "down" and streak:
        bits.append(f"上证连跌 {streak} 日")
    elif side == "up" and streak:
        bits.append(f"上证连涨 {streak} 日")
    elif side == "flat":
        bits.append("上证今日收平")
    for n in (5, 3, 10, streak if streak >= 2 else 0):
        if n and n in cums:
            bits.append(f"近 {n} 日累计 {_pct(cums[n])}".replace("-", "−"))
            break
    window = streak if streak >= 3 else 5
    window = min(max(window, 3), 10)
    return {"streak": streak, "side": side, "text": "，".join(bits), "window": window, "cum": cums}


def _money_with_tape_text(etf: dict, window: int) -> str:
    """与指数轨迹同一窗口的宽基申赎白话；有金额用亿元，否则用份额。"""
    series = etf.get("series") or []
    n = min(max(int(window or 5), 3), 10)
    recent = series[-n:] if series else []
    deltas = [float(row["delta"]) for row in recent if row.get("delta") is not None]
    buy = sum(1 for d in deltas if d > 0) if deltas else None
    # 3 日窗且有净申购金额：对齐结论窗用亿元
    if n == 3 and etf.get("money") is not None:
        money = float(etf["money"])
        buy_n = buy if buy is not None else etf.get("buy_days")
        if buy_n is None:
            return ""
        if abs(money) < 0.05:
            return f"同期宽基基本没变，近 {n} 日净申购 {buy_n} 天"
        word = "净申购" if money > 0 else "净赎回"
        return f"同期宽基{word}约 {abs(money):.0f} 亿 / 净申购 {buy_n} 天"
    if not deltas:
        # 没有日序列时，退回脉冲窗已知字段，并标明窗口
        if etf.get("money") is not None and etf.get("buy_days") is not None:
            money = float(etf["money"])
            buy_n = int(etf["buy_days"])
            if abs(money) < 0.05:
                return f"同期宽基基本没变，近 3 日净申购 {buy_n} 天"
            word = "净申购" if money > 0 else "净赎回"
            return f"同期宽基{word}约 {abs(money):.0f} 亿 / 净申购 {buy_n} 天"
        return ""
    share_sum = sum(deltas)
    if abs(share_sum) < 0.05:
        return f"同期近 {n} 日宽基份额基本没变，净申购 {buy} 天"
    word = "净增" if share_sum > 0 else "净减"
    return f"同期近 {n} 日宽基份额合计 {word} {abs(share_sum):.1f} 亿份，净申购 {buy} 天"


def _mood_stage_text(parts: dict, mood: dict) -> str:
    """情绪阶段 + 上涨占比/炸板率要点；缺什么写什么，不编造。"""
    bits: list[str] = []
    stage = mood.get("stage")
    if stage:
        bits.append(f"短线情绪处在{stage}")
    breadth = parts.get("breadth") or {}
    up, down = breadth.get("up"), breadth.get("down")
    if up is not None and down is not None and (up + down):
        share = up / (up + down)
        bits.append(f"上涨占 {round(share * 100)}%")
    broken = (parts.get("limits") or {}).get("broken_rate")
    if broken is not None:
        bits.append(f"炸板率 {round(broken * 100)}%")
    limits = parts.get("limits") or {}
    if limits.get("up") is not None or limits.get("down") is not None:
        bits.append(f"涨停 {limits.get('up', '—')} / 跌停 {limits.get('down', '—')}")
    if limits.get("height") is not None:
        bits.append(f"最高 {limits['height']} 板")
    return "；".join(bits)


def _index_by_code(parts: dict) -> dict[str, dict]:
    return {
        str(row.get("code") or row.get("ts_code") or ""): row
        for row in ((parts.get("index") or {}).get("rows") or [])
        if isinstance(row, dict) and (row.get("code") or row.get("ts_code"))
    }


def _trend_structure_text(parts: dict, streak: dict) -> str:
    """多周期趋势 + 均线结构；缺数不编。"""
    bits: list[str] = []
    if streak.get("text"):
        bits.append(streak["text"])
    by = _index_by_code(parts)
    sh = by.get("000001.SH") or {}
    if sh.get("ret5") is not None:
        bits.append(f"上证近 5 日 {_pct(sh['ret5'])}".replace("-", "−"))
    if sh.get("ret20") is not None:
        bits.append(f"近 20 日 {_pct(sh['ret20'])}".replace("-", "−"))
    rows = list(by.values())
    known60 = [row for row in rows if row.get("above_ma60") is not None]
    known20 = [row for row in rows if row.get("above_ma20") is not None]
    if known60:
        n60 = sum(1 for row in known60 if row["above_ma60"])
        bits.append(f"主要指数 {n60}/{len(known60)} 在 60 日线上")
    if known20:
        n20 = sum(1 for row in known20 if row["above_ma20"])
        bits.append(f"{n20}/{len(known20)} 在 20 日线上")
    return "；".join(bits)


def _rebound_position_text(parts: dict, streak: dict) -> str:
    """超跌/反弹性质：20 日回撤、破位、连跌语境。"""
    bits: list[str] = []
    by = _index_by_code(parts)
    for code, label in (("000001.SH", "上证"), ("399006.SZ", "创业板")):
        row = by.get(code) or {}
        if row.get("ret20") is None:
            continue
        bits.append(f"{label}近 20 日 {_pct(row['ret20'])}".replace("-", "−"))
    broke = [row.get("name") for row in by.values() if row.get("broke_low")]
    if broke:
        bits.append("破近低：" + "、".join(broke[:3]))
    if streak.get("side") == "down" and (streak.get("streak") or 0) >= 3:
        bits.append("连跌中反弹需先看到宽基回暖")
    elif streak.get("side") == "up" and (streak.get("streak") or 0) >= 3:
        bits.append("连涨后看能否站稳均线")
    return "；".join(bits)


def _breadth_structure_text(parts: dict) -> str:
    """宽度与内部结构：涨跌家数、全 A 均线占比、大小盘相对强弱。"""
    bits: list[str] = []
    breadth = parts.get("breadth") or {}
    up, down = breadth.get("up"), breadth.get("down")
    if up is not None and down is not None and (up + down):
        bits.append(f"上涨 {up} / 下跌 {down}（占 {round(up / (up + down) * 100)}%）")
    share20 = breadth.get("above_ma20_share")
    share60 = breadth.get("above_ma60_share")
    if share20 is not None:
        bits.append(f"全 A 约 {round(share20 * 100)}% 站上 20 日线")
    if share60 is not None:
        bits.append(f"约 {round(share60 * 100)}% 站上 60 日线")
    by = _index_by_code(parts)
    big, small = by.get("000300.SH") or {}, by.get("000852.SH") or {}
    if big.get("ret5") is not None and small.get("ret5") is not None:
        diff = float(small["ret5"]) - float(big["ret5"])
        if abs(diff) >= 0.005:
            bits.append(
                ("小盘相对大盘更强" if diff > 0 else "小盘相对大盘更弱")
                + f"（1000 近 5 日 {_pct(small['ret5'])}，300 {_pct(big['ret5'])}）".replace("-", "−")
            )
        else:
            bits.append("大小盘近 5 日差不多")
    # 无真正等权指数序列时，用中证1000相对沪深300作代理，文案标明
    if big.get("ret20") is not None and small.get("ret20") is not None:
        bits.append(
            f"小盘代理相对大盘近 20 日 {_pct(float(small['ret20']) - float(big['ret20']))}".replace("-", "−")
            + "（非等权指数）"
        )
    return "；".join(bits)


def _style_rotation_text(sector_rows: list[dict] | None = None, sectors_card: dict | None = None) -> str:
    """风格与轮动：优先用双榜，其次今日关注/危险。"""
    boards = (sectors_card or {}).get("boards") if isinstance(sectors_card, dict) else None
    if not boards and sector_rows:
        boards = _sector_boards(sector_rows)
    if not boards:
        headline = (sectors_card or {}).get("headline") if isinstance(sectors_card, dict) else ""
        return str(headline or "")
    bits: list[str] = []
    for key, label in (("rising", "在攻"), ("up", "偏强"), ("exiting", "在出"), ("down", "偏弱")):
        names = [row.get("name") for row in (boards.get(key) or [])[:3] if row.get("name")]
        if names:
            bits.append(f"{label}：" + "、".join(names))
    return "；".join(bits)


def _analyst_tape_facts(parts: dict, mood: dict, sectors_card: dict | None = None, sector_rows: list[dict] | None = None) -> dict:
    """大盘分析师开场用：六维可核对事实。"""
    turn = parts.get("turnover") or {}
    etf = parts.get("etf") or {}
    streak = _index_streak_bits(turn.get("series"))
    money_text = _money_with_tape_text(etf, streak.get("window") or 5)
    return {
        "index_streak": streak.get("text") or "",
        "trend": _trend_structure_text(parts, streak),
        "rebound": _rebound_position_text(parts, streak),
        "money_with_tape": money_text,
        "mood_stage": _mood_stage_text(parts, mood),
        "breadth_structure": _breadth_structure_text(parts),
        "style_rotation": _style_rotation_text(sector_rows, sectors_card),
        "window": streak.get("window") or 5,
        "streak": streak.get("streak") or 0,
        "side": streak.get("side") or "",
    }


def _team_window_bit(
    windows: list[dict] | None,
    *,
    pressure: str = "",
    series: list[dict] | None = None,
    today_broad=None,
) -> str:
    """眼下宽基冷热：白话讲申赎方向；|偏离| > 1.5 点明向上/向下波动；大盘跌久仍在卖则加重提示。"""
    short = [f for f in (windows or []) if f.get("k") in ("1 日", "3 日", "5 日") and f.get("v") not in (None, "—", "")]
    vals: list[float] = []
    for f in short:
        num = _window_num(f.get("v"))
        if num is not None:
            vals.append(num)
    if not vals:
        return ""
    extreme = max(vals, key=abs)
    abs_max = abs(extreme)
    still_selling = extreme < 0 or (today_broad is not None and today_broad < 0)
    long_down = _market_down_long(series, pressure)

    if abs_max <= 1.5:
        bit = "眼下宽基申赎和平时差不多。"
        if long_down and still_selling:
            return bit + "不过大盘已经跌了一阵，宽基仍略偏卖，要重点留意。"
        return bit
    if extreme < 0:
        if abs_max > 3:
            bit = "眼下宽基明显在卖，向下波动已经不像平时。"
        else:
            bit = "眼下宽基偏在卖出，有向下波动。"
        if long_down:
            return bit + "大盘已经跌了一阵还在卖，要重点留意。"
        return bit
    if extreme > 0:
        if abs_max > 3:
            return "眼下宽基明显在买，向上波动已经不像平时。"
        return "眼下宽基偏在买入，有向上波动。"
    return "眼下宽基申赎和平时差不多。"


def _team_act_story(last: dict | None, windows: list[dict] | None = None, team: dict | None = None) -> str:
    """托市块正文：先讲发生了什么，再补眼下宽基冷热；归并规则留给问号，不塞进结论。"""
    if not last:
        return "近一年没有承压托市，也没有非承压的宽基异动。"
    team = team or {}
    return _team_event(last) + _team_window_bit(
        windows,
        pressure=team.get("pressure") or "",
        series=team.get("series"),
        today_broad=team.get("today_broad"),
    )


def _team_card(team: dict, announce: dict) -> dict:
    if not team.get("ok"):
        return {"pending": True, "text": team.get("text") or "份额数据没取到，这一项暂时判不了。"}
    team = _team_ready(team, announce)
    last, hold = team.get("last"), team.get("hold")
    tone = _TEAM_TONE.get(team["label"], "flat")
    gone = None if not hold else hold.get("gone")
    windows = _window_facts(team.get("windows"))
    active = team["label"] in ("托市中", "异动中")
    if team.get("confirm") == "L1" and active:
        now_word = "确认汇金托市"
    elif active:
        now_word = team.get("confirm_word") or ("疑似托市" if team["label"] == "托市中" else "疑似异动")
    elif team["label"] == "已表态":
        now_word = "已表态"
    elif last and last.get("state") == "托市":
        now_word = _wave_when(last, "托市")
    elif last and last.get("state") == "异动":
        now_word = _wave_when(last, "买入异动" if last.get("side") != "sell" else "卖出异动")
    else:
        now_word = "无异动"
    # 主卡与资金列同款：一行摘要 + 日度柱；波段格子 / 窗口 Mz 进依据页
    act_bars = team.get("mz_daily") or []
    hold_bars = team.get("hold_mz_daily") or []
    if not hold or gone is None:
        hold_detail = "没有可追踪的托市或异动。"
    else:
        hold_detail = (
            ("卖出后 10 个交易日里的最低份额，现在" if hold.get("side") == "sell" else "托市后 10 个交易日里的最高份额，现在")
            + ("没有变化。" if gone <= 0 else f"{'回来了' if hold.get('side') == 'sell' else '回落了'} {gone * 100:.0f}%。")
        )
        line_yi, now_yi, gap_yi = hold.get("line_yi"), hold.get("now_yi"), hold.get("gap_yi")
        if line_yi is not None and now_yi is not None:
            gap_txt = "已过线" if gap_yi is not None and gap_yi < 0 else ("—" if gap_yi is None else f"{gap_yi:.0f} 亿份")
            hold_detail += f"按自身波动定的警戒线大约在 {line_yi:.0f} 亿份，现在 {now_yi:.0f} 亿份，还差 {gap_txt}。"
        hold_detail += "这是整组份额的净额，散户赎回也会让它下降。累计回落只作确认，不单独判「可能在撤」。"
    mild = hold.get("mild") if hold and hold.get("mild") is not None else 0.1
    hard = hold.get("hard") if hold and hold.get("hard") is not None else 0.3
    if not hold:
        hold_answer, hold_tone = "没有记录", "flat"
    elif hold.get("side") == "sell":
        if gone is not None and gone > hard:
            hold_answer, hold_tone = "明显回补", "flat"
        elif gone is not None and gone >= mild:
            hold_answer, hold_tone = "部分回补", "warn"
        else:
            hold_answer, hold_tone = "还没回补", "flat"
    elif team["label"] == "可能在撤" or (gone is not None and gone > hard):
        hold_answer, hold_tone = "明显回落", "bad"
    elif gone is not None and gone >= mild:
        hold_answer, hold_tone = "部分回落", "warn"
    else:
        hold_answer, hold_tone = "钱还在", "flat"
    rows = [
        {"q": "最近有没有托市？", "a": now_word,
         "tone": "good" if team["label"] == "托市中" else "warn" if team["label"] == "异动中" else "flat",
         "facts": _act_summary_facts(last, team),
         "bars": act_bars,
         "detail": _team_act_story(last, windows, team)},
        {"q": "份额还在吗？", "a": hold_answer, "tone": hold_tone,
         "facts": _hold_facts(hold, compact=True),
         "bars": hold_bars,
         "bars_kind": "gone",
         "detail": hold_detail},
        {"q": "有没有在撤？", "a": "迹象较强" if "迹象较强" in (team.get("marks") or []) else ("可能在撤" if team["label"] == "可能在撤" or (team.get("sell") or {}).get("sign2") else "暂时没有"),
         "tone": "bad" if team["label"] == "可能在撤" else ("warn" if "迹象较强" in (team.get("marks") or []) else "flat"),
         "facts": _out_summary_facts(team),
         "bars": act_bars,
         "detail": (
             "看宽基份额有没有往外走，不是股价。"
             "主迹象：指数相对高位，且近 5 日宽基份额相对自身常态显著净减；"
             "或是不跌的日子里宽基被广泛赎回。托市后份额回落只作确认。"
             "只展示，不改市场总档。"
         )},
    ]
    if team["label"] == "已表态":
        rows[0]["a"] = "已表态"
        rows[0]["detail"] = "汇金说了要增持，份额上还没有看到对应的钱。"
    means = {
        "托市中": "市场承压时有大钱在买宽基。这是疑似托市，汇金公告对上才叫确认。",
        "异动中": "市场没有承压，宽基自己出现了大额异常申购或赎回。这不是托市。",
        "钱还在": "上次买进去的份额大部分还在。份额是所有人申赎相抵后的净额。",
        "部分回落": "上次买进去的份额回落了一部分，按自身波动看过了「还在」线，但还没到确认线。",
        "可能在撤": "指数相对高位且近 5 日宽基份额显著净减，或不跌日里宽基被广泛赎回。可能是国家队，也可能是别人。",
        "没托市": "最近没有承压托市，也没有非承压的宽基异动。",
        "已表态": "公告已经出来，份额上还没看到对应的资金。",
        "还没回补": "上次卖出之后，份额还没买回来。",
        "部分回补": "上次卖出的份额回来了一部分。",
        "明显回补": "上次卖出的份额大部分已经回来。",
    }[team["label"]] if team["label"] in {
        "托市中", "异动中", "钱还在", "部分回落", "可能在撤", "没托市", "已表态", "还没回补", "部分回补", "明显回补",
    } else "看的是宽基份额有没有被集中买卖。不进总档。"
    return {
        "pending": False, "tier": "平", "label": team["label"], "tone": tone, "summary": _team_headline(team),
        "marks": team.get("marks") or [], "qa": rows, "means": means,
        "when": "市场在跌、钱进宽基、至少两组不像平时，同一天都碰上，才是疑似托市。市场没承压、但至少一组宽基明显偏离，是疑似异动。汇金公告对上才写成确认汇金托市。这张卡不改市场总档。",
        "scope": "看按指数合并后的宽基份额。行业和跨境只用来看钱去了哪，半导体、医药、有色黄金、软件的异常只提示。份额第二天早上才到。",
        "date": team.get("latest", ""),
        "asof_note": "宽基份额 T+1，收盘后通常要到第二天早上才有今天的数；有「待更新」就是指数已到今天、份额还停在昨天",
        "announce": [row for row in (team.get("stance") or [])[:1] or announce.get("items", [])[:1]],
    }


def _book_rows() -> list[dict]:
    rows = []
    for kind, groups in market_team.POOL:
        for name, codes in groups:
            for code, label in codes:
                rows.append({"left": label, "mid": code[:6], "right": f"{kind} · {name}", "code": code})
    return rows


def _team_tab(parts: dict, day: date) -> dict:
    team, announce = parts.get("team") or {}, parts.get("announce") or {}
    if not team.get("ok"):
        return {"title": "国家队", "label": "没取到", "tone": "flat", "pending": True, "sections": [],
                "summary": team.get("text") or "份额数据没取到，这一项暂时判不了。", "footnote": ""}
    card = _team_card(team, announce)
    team = _team_ready(team, announce)
    last, hold, counts = team.get("last"), team.get("hold"), team["counts"]
    previous = _previous_open(parts, day)
    age = _age(team["latest"], day, previous)
    act = _block(
        "act", "① 最近有没有托市", "大跌时有没有人猛买宽基",
        f"近 5 天 {_stored(counts, 5) or 0} 次", age, card["qa"][0]["a"], card["qa"][0]["tone"],
        _span_stats(counts, lambda value: f"{0 if value is None else value} 次", 5),
        _TEAM_ACT_RULE, story=card["qa"][0]["detail"], open=True,
        chart={"kind": "team", "unit": "亿元", "points": team["series"],
               "legend": [
                   {"t": "净申购", "c": "#D9483F"}, {"t": "净赎回", "c": "#2E8B57"},
                   {"t": "托市或异动", "c": "#2457D6"}, {"t": "上证指数", "c": "#5B6372"},
               ],
               "note": "柱＝当天宽基合计净申购（亿元）。灰线＝上证。蓝点＝触发托市或异动的那天。"},
    )
    hold_gone = None if not hold else hold.get("gone")
    hold_big = "没有记录" if hold_gone is None else (
        f"{'回补' if hold.get('side') == 'sell' else '回落'} {hold_gone * 100:.0f}%"
    )
    holdblock = _block(
        "hold", "② 份额还在吗", "买进去的还在不在",
        hold_big, age, card["qa"][1]["a"], card["qa"][1]["tone"],
        [_stat("回落" if not hold or hold.get("side") != "sell" else "回补",
               "—" if hold_gone is None else f"{hold_gone * 100:.0f}%"),
         _stat("警戒线", "—" if not hold or hold.get("line_yi") is None else f"{hold['line_yi']:.0f} 亿份"),
         _stat("现在", "—" if not hold or hold.get("now_yi") is None else f"{hold['now_yi']:.0f} 亿份")],
        _TEAM_HOLD_RULE, story=card["qa"][1]["detail"],
    )
    outblock = _block(
        "out", "③ 有没有在撤", "份额有没有往外走",
        card["qa"][2]["a"], age, card["qa"][2]["a"], card["qa"][2]["tone"],
        [_stat(f["k"], f["v"]) for f in (card["qa"][2].get("facts") or [])],
        _TEAM_OUT_RULE, story=card["qa"][2]["detail"],
    )
    live = _block(
        "live", "今天盘中", "份额次日才到",
        "待份额" if team.get("stale") else "看份额", _age(team.get("latest") or "", day),
        "待更新" if team.get("stale") else "已更新", "warn" if team.get("stale") else "flat", [],
        "盘中看不到份额。成交突然放大、溢价偏高，只作提醒；结论等第二天份额出来再定。",
    )
    items = [{"left": f"{row['who']}：{row['title']}", "mid": (row.get("date") or "")[5:], "right": "强信号" if row.get("strong") else row.get("who") or "", "url": row.get("url") or ""} for row in (announce.get("items") or [])]
    for row in team.get("others") or []:
        items.append({"left": f"{row['who']}：{row['title']}", "mid": row["date"][5:], "right": "单独展示", "url": row.get("url") or ""})
    ann = _block(
        "ann", "官方说了什么", "汇金对上才叫确认",
        f"近 20 天 {len(items)} 条" if items else "没有筛到", "近 20 天",
        "确认是汇金" if team.get("confirm") == "L1" else "只做确认", "flat", [],
        "汇金公开说增持、且份额对得上，才从疑似升级为确认。只有公告、份额上没看到 →「已表态」。诚通、国新单独展示。",
        listing={"title": "近期相关新闻", "rows": items} if items else None,
    )
    hist = team.get("history") or []

    def _hist_left(row: dict) -> str:
        start, until = row.get("day") or "", row.get("until") or row.get("day") or ""
        if until and until != start and (row.get("span") or 1) > 1:
            return f"{start[5:]}～{until[5:]}"
        return start

    history_block = _block(
        "hist", "历次出手", "出手后 20 日上证", f"{len(hist)} 次", "近一年", "样本少", "flat", [],
        "同一波只记一次。样本很少，不能用来推测会涨到哪。",
        listing={"title": "这一波 · 最大一天净申购 · 之后 20 日上证", "rows": [
            {"left": _hist_left(row), "mid": f"{row['delta']:+.0f} 亿元".replace("-", "−"), "right": ("进行中 · 第 %d 天" % row["progress"]) if row["ret20"] is None else _pct(row["ret20"])}
            for row in reversed(hist)
        ]} if hist else None,
    )
    # 头区只留：标题 + 状态胶囊 + 时间 + 一句人话结论。
    # 不再把 label 再写进 summary，也不塞「什么时候会变」这种元说明（脚注已有）。
    return {
        "title": "国家队", "label": team["label"], "tone": card["tone"], "age": age,
        "summary": _team_headline(team),
        "lead": "",
        "means": "",
        "when": "",
        "sections": [
            {"title": "三件事", "hint": "先看结论，点开看图", "blocks": [act, holdblock, outblock]},
            {"title": "辅助", "hint": "公告 · 盘中 · 历次", "blocks": [ann, live, history_block]},
        ],
        "footnote": "只看宽基 ETF 份额（次日才到）。不走申购、直接二级市场买的，份额里看不到。这张卡不改市场总档。",
    }




# ---------------------------------------------------------------- 摘要
def build_summary(today: date | None = None, record: bool = True) -> dict:
    day = today or datetime.now(_ZONE).date()
    parts = bundle(day)
    if "personal" not in parts and parts.get("_day") == day.isoformat() and day.isoformat() not in _REFRESHING:
        parts["personal"] = _personal(parts.get("watch") or [], day)
        cache.set(_bundle_key(day), parts, _BUNDLE_TTL)
        market_disk.save_bundle(day.isoformat(), parts)
    summary = summarize(parts, day)
    # 后台正在用今天的数据重算时，这一份是旧结果，快照等重算完再记一次。
    if record and parts.get("_day") == day.isoformat() and day.isoformat() not in _REFRESHING:
        try:
            market_notify.record(summary, parts)
        except Exception:
            pass   # 存档失败不能让页面打不开
    # money / team / sectors 仍随 summary 返回：前端首屏依赖它们；
    # /market/card/* 留给单独刷新或并行预取，不再从 summary 里剥掉。
    return summary


def build_workbench(today: date | None = None) -> dict:
    """今天工作台：结论精简版 + 会话态 + 分时，不打开依据栏。计划待办由 harness 拼。"""
    summary = build_summary(today)
    keep = (
        "generated_at", "today", "indexes", "dates", "time_basis",
        "session", "phase", "phase_label", "trading_day", "quote_delay_sec",
        "unchanged_since", "conclusion", "mood", "watch", "ages",
    )
    slim = {key: summary[key] for key in keep if key in summary}
    try:
        intraday = market_intraday.build()
    except Exception:
        intraday = {"ok": False, "live": False}
    return {"status": "ok", "view": "today", **slim, "intraday": intraday}


def build_card(name: str, today: date | None = None) -> dict:
    """单张卡：fund / team / sector（money 等同 fund）。"""
    key = _CARD_ALIASES.get(name, name)
    if key not in CARDS:
        raise ValueError(f"未知 card {name}")
    day = today or datetime.now(_ZONE).date()
    parts = bundle(day)
    previous = _previous_open(parts, day)
    if key == "fund":
        card = _money_card(parts, _money_signal(parts))
    elif key == "team":
        card = _team_card(parts.get("team") or {}, parts.get("announce") or {})
    else:
        sector_rows = _sector_rows(parts["sectors"], parts.get("catalysts"))
        card = _sector_card_fields(sector_rows, parts)
    card["key"] = key
    card["updated"] = _updated_label(card.get("date") or "", day)
    card["age"] = _age(card.get("date") or "", day, previous)
    if key == "fund" and not card.get("asof_note"):
        dates = card.get("dates") or {}
        card["asof_note"] = _money_asof_note(dates.get("turnover") or "", dates.get("etf") or "", dates.get("margin") or "")
    elif key == "team":
        card["asof_note"] = card.get("asof_note") or "宽基份额 T+1，收盘后通常要到第二天早上才有今天的数；有「待更新」就是指数已到今天、份额还停在昨天"
    elif key == "sector":
        card["asof_note"] = card.get("asof_note") or "板块「钱」按 ETF 份额算，份额末日是哪天卡就标到哪天；价/热度也裁到同一天，避免和份额错位"
    return card


def refresh(today: date | None = None) -> None:
    """丢掉今天的市场包并同步重算。go-harness 盘前/收盘定时会先调这里再取 summary。"""
    global _GENERATION
    from app.services import market_tape

    day = today or datetime.now(_ZONE).date()
    with _REFRESH_LOCK:
        _GENERATION += 1
        generation = _GENERATION
    cache.set(_bundle_key(day), None, 0)
    market_disk.drop(f"bundle:{day.isoformat()}")
    market_tape.invalidate_today_series(day)
    try:
        _store_bundle(_compute(day), day, generation)
    except Exception:
        _kick(day)


def changes_view(mode: str, since_ts: float | None = None, today: date | None = None) -> dict:
    """变了什么：mode 取 last / today / week。"""
    day = today or datetime.now(_ZONE).date()
    build_summary(day)
    snaps = get_store().snapshots()
    out = market_changes.view(mode, snaps, since_ts, market_notify.settings()["etf_n"])
    if mode == "today":
        out["items"] = out["items"] + _intraday_items(market_intraday.build())
    return out


def _intraday_items(data: dict) -> list[dict]:
    items = []
    market = data.get("market")
    if data.get("live") and market and market.get("vs_yesterday") is not None:
        rate = market["vs_yesterday"]
        items.append({"kind": "intraday", "title": f"成交比昨天同一时间{'多' if rate > 0 else '少'} {abs(rate) * 100:.0f}%", "tag": "盘中",
                      "detail": "市场" + ("更热了" if rate > 0 else "更冷了") + "；盘中数据只提醒，不改结论", "tone": "good" if rate > 0 else "warn", "tab": "fund"})
    etf = data.get("etf")
    if data.get("live") and etf and etf.get("alert"):
        items.append({"kind": "intraday", "title": "宽基 ETF 盘中成交放大", "tag": "盘中预警", "detail": "大盘跌超 1%，宽基 ETF 成交是前几天同一时刻的 3 倍以上；这只是提示，要等第二天的份额才算数", "tone": "good", "tab": "team"})
    return items


def history_view() -> list[dict]:
    return market_changes.history(get_store().snapshots())


def notify_payload(today: date | None = None, inbox: list[dict] | None = None) -> dict:
    day = today or datetime.now(_ZONE).date()
    parts = bundle(day)
    card = _calendar_card(parts, parts["watch"], day)
    upcoming = [
        {"tag": "提醒", "title": row["text"], "when": row["date"][5:], "weekday": row["weekday"], "tab": "cal"}
        for row in card["rows"] if row["tag"] == "涉及自选" and row["date"] >= day.isoformat()
    ]
    return market_notify.notifications(upcoming=upcoming, today=day, inbox=inbox)


def _stamp_card(card: dict, day: date, date_key: str = "date") -> dict:
    """给卡打上首行用的 updated 标签。"""
    raw = card.get(date_key) or card.get("date") or card.get("month") or ""
    card["updated"] = _updated_label(str(raw), day)
    return card


def summarize(parts: dict, day: date) -> dict:
    money = _money_signal(parts)
    mood = _stamp_card(_mood_card(parts), day)
    macro = _stamp_card(_macro_card(parts), day, "month")
    calendar = _stamp_card(_calendar_card(parts, parts["watch"], day), day)
    calendar["updated"] = calendar.get("updated") or _updated_label(day.isoformat(), day)
    sector_rows = _sector_rows(parts["sectors"], parts.get("catalysts"))
    sector_picks = _sector_picks(sector_rows)
    good_sectors = [item["name"] for item in sector_picks]
    signals = [
        {"key": "money", "n": "①", "name": "钱", "tier": money["tier"], "label": _money_word(money["tier"]),
         "tone": money["tone"], "text": money["head"]["summary"]},
        _team_signal(parts.get("team") or {}, parts.get("announce") or {}),
        {"key": "mood", "n": "③", "name": "好不好做", "tier": mood["tier"], "label": mood["label"], "tone": mood["tone"],
         "text": mood["summary"]},
    ]
    total = rules.overall(signals)
    better, worse = _better_worse(parts, mood)
    indexes = (parts["index"] or {}).get("rows") or []
    previous = _previous_open(parts, day)
    money_card = _stamp_card(_money_card(parts, money), day)
    team_card = _stamp_card(_team_card(parts.get("team") or {}, parts.get("announce") or {}), day)
    sectors = _stamp_card(_sector_card_fields(sector_rows, parts), day)
    now = datetime.now(_ZONE)
    session = _session_phase(now)
    quote_asof = indexes[0]["date"] if indexes else ""
    funds_asof = (parts["turnover"] or {}).get("date") or (parts["margin"] or {}).get("date") or ""
    macro_asof = (parts["macro"] or {}).get("month") or ""
    unchanged = _unchanged_since(day)
    index_rows = [
        {
            "name": row["name"],
            "code": row.get("code") or row.get("ts_code") or "",
            "ts_code": row.get("code") or row.get("ts_code") or "",
            "close": row.get("close"),
            "pct": row.get("pct"),
            "date": row.get("date"),
            "ret3": row.get("ret3"),
            "ret5": row.get("ret5"),
            "ret20": row.get("ret20"),
            "above_ma20": row.get("above_ma20"),
            "above_ma60": row.get("above_ma60"),
            "broke_low": row.get("broke_low"),
        }
        for row in indexes
    ]
    analyst = _analyst_tape_facts(parts, mood, sectors_card=sectors, sector_rows=sector_rows)
    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "today": day.isoformat(),
        "indexes": index_rows,
        "analyst": analyst,
        "dates": {
            "quote": quote_asof,
            "money": funds_asof,
            "macro": macro_asof,
        },
        "time_basis": {
            "quotes": quote_asof,
            "funds": funds_asof,
            "macro": macro_asof,
        },
        "session": session["session"],
        "phase": session["phase"],
        "phase_label": session["phase_label"],
        "trading_day": session["trading_day"],
        "quote_delay_sec": session["quote_delay_sec"],
        "unchanged_since": unchanged,
        "conclusion": {
            "level": total["level"], "tilt": total["tilt"], "count": total["count"],
            "gate": money["head"]["gate"], "gate_text": money["head"]["gate_text"],
            "headline": "、".join(item["text"] for item in signals if (item["tier"] and item["tier"] != "平") or item.get("label") in ("托市中", "异动中", "可能在撤", "已表态")) or "成交、宽基、两融和指数没有一起指向一边",
            "action": rules.action_text(total["level"], total["tilt"], good_sectors),
            "signals": signals, "better": better, "worse": worse,
            "phase_label": session["phase_label"],
            "unchanged_since": unchanged,
            "note": "按框架规则得出，不构成投资建议。" + ("" if (parts.get("team") or {}).get("ok") else "国家队没取到，总档位只用已经取到的信号。"),
        },
        "money": money_card,
        "team": team_card,
        "mood": mood,
        "macro": macro,
        "calendar": calendar,
        "sectors": sectors,
        "watch": {"rows": parts["watch"], "scope": "动作词由「板块结论 × 个股判断」按规则生成，不是买卖指令；只读已有判断，不在这里批量发起诊断"},
        "ages": {"turnover": _age((parts["turnover"] or {}).get("date", ""), day, previous), "etf": _age((parts["etf"] or {}).get("latest_date", ""), day, previous)},
    }


def _band(value, low: float, high: float, below: str, mid: str, above: str) -> str:
    if value is None:
        return "没有"
    if value < low:
        return below
    if value > high:
        return above
    return mid


def _span_words(bucket, days: tuple[int, ...], low: float, high: float, below: str, mid: str, above: str, digits: int = 1) -> str:
    bits = []
    for n in days:
        value = _stored(bucket, n)
        bits.append(f"{n} 日 {_pct(value, digits)}（{_band(value, low, high, below, mid, above)}）")
    return "，".join(bits)


def _span_facts(bucket, days: tuple[int, ...], low: float, high: float, below: str, mid: str, above: str, digits: int = 1) -> list[dict]:
    tone_of = {below: "bad", above: "good"}
    facts = []
    for n in days:
        value = _stored(bucket, n)
        word = _band(value, low, high, below, mid, above)
        facts.append({"k": f"{n} 日", "v": f"{_pct(value, digits)} {word}", "tone": tone_of.get(word, "flat")})
    return facts


def _span_facts(bucket, days: tuple[int, ...], low: float, high: float, below: str, mid: str, above: str, digits: int = 1) -> list[dict]:
    tone_of = {below: "bad", above: "good"}
    facts = []
    for n in days:
        value = _stored(bucket, n)
        word = _band(value, low, high, below, mid, above)
        facts.append({"k": f"{n} 日", "v": f"{_pct(value, digits)} {word}", "tone": tone_of.get(word, "flat")})
    return facts


def _mz_text(value) -> str:
    if value is None:
        return "基线不足"
    rounded = round(float(value), 1)
    if rounded == 0:
        return "Mz 0.0"
    sign = "+" if rounded > 0 else "−"
    return f"Mz {sign}{abs(rounded):.1f}"


def _mz_fact(mz, n: int, hot: float, cold: float) -> dict:
    value = _stored(mz, n)
    tone = "flat" if value is None else "good" if value > hot else "bad" if value < cold else "flat"
    return {"k": f"{n} 日", "v": _mz_text(value), "tone": tone}


def _money_card(parts: dict, money: dict) -> dict:
    turn, etf, margin = parts["turnover"], parts["etf"], parts["margin"]
    etf_word = {"净流入": "有", "净流出": "没有，在流出", "基本没变": "基本没有"}.get(etf.get("label"), "没有")
    taking = margin.get("label") in ("在降", "加速下降")
    turn_bars = turn.get("mz_daily") or []
    etf_bars = etf.get("mz_daily") or []
    margin_bars = margin.get("mz_daily") or []
    # 有日度柱图时，主卡只留金额类摘要；窗口 Mz 格子让位给图
    turn_facts = [{"k": "今天", "v": _yi(turn.get("amount")), "tone": "flat"}]
    if not turn_bars:
        if turn.get("mz"):
            turn_facts = [_mz_fact(turn.get("mz"), n, 1.5, -1.0) for n in (1, 3, 5, 30)] + turn_facts
        else:
            turn_facts = _span_facts(turn.get("windows"), (1, 3, 5, 30), -0.05, 0.05, "低于平时", "接近平时", "高于平时") + turn_facts
    etf_facts = [{"k": "净申购", "v": _yi(etf.get("money"), True), "tone": "good" if (etf.get("money") or 0) > 0 else "bad" if (etf.get("money") or 0) < 0 else "flat"}]
    if not etf_bars:
        if etf.get("mz"):
            etf_facts = [_mz_fact(etf.get("mz"), n, 1.5, -1.5) for n in (1, 3, 5, 30, 60)] + etf_facts
        else:
            etf_facts = _span_facts(etf.get("windows"), (1, 3, 5, 30, 60), -0.01, 0.01, "在减", "基本没变", "在增", 2) + etf_facts
    net = margin.get("net")
    margin_facts = [{"k": "融资净买入", "v": _yi(net, True), "tone": "good" if (net or 0) > 0 else "bad" if (net or 0) < 0 else "flat"}]
    if not margin_bars:
        if margin.get("mz"):
            margin_facts = [_mz_fact(margin.get("mz"), n, 1.5, -1.5) for n in (1, 3, 5)] + [_mz_fact(margin.get("mz"), 30, 0, -1.0)] + margin_facts
        else:
            margin_facts = _span_facts(margin.get("windows"), (5, 30), -0.02, 0.02, "在降", "持平", "在升") + margin_facts
    marks = list(money["head"].get("notes") or [])
    indexes = (parts.get("index") or {}).get("rows") or []
    risk = _price_flow_risk(etf, indexes)
    if risk:
        marks = [risk] + [m for m in marks if m != risk]
    return {
        "tier": money["tier"], "label": _money_word(money["tier"]), "tone": money["tone"],
        "summary": money["head"]["summary"],
        "qa": [
            {"q": "最近交易活跃吗？", "a": {"冷清": "比平时冷清", "活跃": "比平时活跃", "正常": "接近自身常态", "平时": "接近自身常态"}.get(turn.get("label"), "没有"),
             "tone": {"冷清": "bad", "活跃": "good"}.get(turn.get("label"), "flat"),
             "facts": turn_facts, "bars": turn_bars,
             "detail": "对数成交额的均值直接算 Mz。活跃要 Mz > +1.5，冷清要 Mz < −1.0，对数偏离还要至少 5%。30 日要和另外至少一档同向，最近两天也要同向。图是逐日 1 日 Mz，正红负绿。"},
            {"q": "有新钱进场吗？", "a": etf_word, "tone": {"净流入": "good", "净流出": "bad"}.get(etf.get("label"), "flat"),
             "facts": etf_facts, "bars": etf_bars,
             "top": etf.get("top_in") or [],
             "detail": "每只单独算 Mz。累计变化对照 n × M，MAD 按 √n 放大。净流入要 3 日 Mz > +1.5，30 日和 60 日同向，3 日累计至少 0.3%，且没有连续两天净流出。净流出要 3 日 Mz < −1.5，至少 2 天在卖。图是宽基合计日变化的 1 日 Mz。"},
            {"q": "有人在拿钱走吗？", "a": "有" if taking else "暂时没有", "tone": "bad" if taking else "good",
             "facts": margin_facts, "bars": margin_bars,
             "detail": "累计变化算 Mz，对照 n × M，MAD 按 √n 放大。加速下降要 5 日 Mz < −2.0 且 30 日 Mz < −1.0。在降要 5 日 Mz < −1.5，或 5 日 Mz < −1.0 且 30 日 Mz < −1.5。在升要 5 日 Mz > +1.5 且 30 日 Mz > −1.0。图是融资余额日变化率的逐日 1 日 Mz。「融资净买入」= 当日融资买入 − 融资偿还，只作金额对照，和余额变化、判定不是同一口径。"},
        ],
        "means": "",
        "when": _money_when(turn.get("label") or "", etf.get("label") or "", margin.get("label") or "", money["head"].get("label")),
        "scope": "成交、宽基、两融都按自身近 60 个交易日的常态偏离来判；回购按公司只留最新一条实施公告",
        "marks": marks,
        # 首行时效跟「今天成交额」对齐；两融/份额常晚一天，不拿它们把日期拖旧
        "date": turn.get("date") or margin.get("date") or "",
        "dates": {
            "turnover": turn.get("date") or "",
            "margin": margin.get("date") or "",
            "etf": etf.get("latest_date") or etf.get("date") or "",
        },
        "asof_note": _money_asof_note(turn.get("date") or "", etf.get("latest_date") or etf.get("date") or "", margin.get("date") or ""),
    }


def _money_asof_note(turn_day: str, etf_day: str, margin_day: str) -> str:
    """资金卡首行日期后的取数说明。"""
    bits = []
    if turn_day:
        bits.append(f"成交额到 {turn_day[5:]}")
    if etf_day:
        bits.append(f"宽基份额到 {etf_day[5:]}（份额 T+1）")
    if margin_day:
        bits.append(f"两融到 {margin_day[5:]}")
    return "；".join(bits) if bits else "成交额收盘后当天有；宽基份额、两融常晚一天"


def _money_when(turnover: str, etf: str, margin: str, lean: str | None = None) -> str:
    """依据头「什么时候会变」：只说离开当前档要什么，不重复总规则说明书。"""
    lean = lean or judge_pulse(turnover, etf, margin)
    if lean == "在进":
        return "宽基不再净流入，或成交变冷清，就会离开「在进」。"
    if lean == "偏弱":
        if etf == "净流入":
            return "成交不再冷清，就会改成「在进」；若宽基也不再净流入，会先回到观望。"
        return "成交不再冷清，或两融止跌，才会离开「偏弱」。"
    # 观望：按卡在哪一步，给一句出口
    if etf == "净流入" and turnover in ("正常", "平时") and margin == "加速下降":
        return "现在是「钱在进、杠杆在退」。成交转活跃，或两融不再加速下降，才会改成「在进」。"
    if etf == "净流入" and turnover == "冷清":
        return "宽基已经净流入，等成交不再冷清，才会改成「在进」。"
    if etf == "净流入":
        return "宽基已在净流入；成交不冷清就会改成「在进」。"
    if etf == "净流出":
        return "要改成「在进」，得先看到宽基重新净流入，且成交不冷清。"
    return "要改成「在进」：宽基净流入，且成交不冷清。要改成「偏弱」：成交冷清，且两融在降。"


# ---------------------------------------------------------------- 依据 tab
def build_tab(name: str, today: date | None = None) -> dict:
    if name not in TABS:
        raise ValueError(f"未知 tab {name}")
    day = today or datetime.now(_ZONE).date()
    return tab_of(name, bundle(day), day)


def tab_of(name: str, parts: dict, day: date) -> dict:
    if name == "fund":
        return _fund_tab(parts, day)
    if name == "team":
        return _team_tab(parts, day)
    if name == "sector":
        return _sector_tab(parts, day)
    if name == "mood":
        return _mood_tab(parts)
    if name == "cal":
        return _cal_tab(parts, day)
    return _macro_tab(parts)


def _block(key, title, sub, big, age, judge, tone, stats, rule, **extra) -> dict:
    return {"key": key, "title": title, "sub": sub, "big": big, "age": age, "judge": judge, "tone": tone,
            "stats": stats, "rule": rule, **extra}


def _index_by_day(points: list[dict] | None) -> dict[str, float]:
    out = {}
    for row in points or []:
        if row.get("date") and row.get("index") is not None:
            out[row["date"]] = row["index"]
    return out


def _with_index(points: list[dict] | None, index_by_day: dict[str, float]) -> list[dict]:
    if not points:
        return []
    if not index_by_day:
        return list(points)
    return [{**row, "index": index_by_day.get(row.get("date"))} for row in points]


def _fund_tab(parts: dict, day: date) -> dict:
    ensure(parts, ("south", "shibor", "funds"), day)
    previous = _previous_open(parts, day)
    turn, etf, margin = parts["turnover"], parts["etf"], parts["margin"]
    south, shibor, supply, funds = parts.get("south") or {}, parts.get("shibor") or {}, parts["supply"], parts.get("funds") or {}
    money = _money_signal(parts)
    groups = etf.get("groups") or {}
    index_by_day = _index_by_day(turn.get("series"))
    index_legend = {"t": "上证指数", "c": "#5B6372"}
    core = [
        _block(
            "turnover", "成交额", "市场热不热", _yi(turn.get("amount")), _age(turn.get("date", ""), day, previous),
            turn.get("label") or "没有", {"冷清": "bad", "活跃": "good"}.get(turn.get("label"), "flat"),
            _mz_stats(turn.get("mz"), (1, 3, 5, 30), turn.get("votes") or []),
            "Modified Z-Score。对数成交额的均值直接算 Mz = 0.6745 × (均值 − M) / MAD。活跃要 Mz > +1.5，冷清要 Mz < −1.0，对数偏离还要至少 5%。30 日要和另外至少一档同向，最近两天也同向。基线不足时退回 ±10%。",
            story=(f"近 5 天里 {turn['down_days']} 天指数下跌，成交" + ("一直比平时少" if (turn.get("ratio5") or 0) < 0 else "比平时多") + "。") if turn.get("down_days") else "",
            pctile=_pctile_info("成交额处在一年里", turn.get("pctile")),
            chart={"kind": "bars", "unit": "亿元", "points": turn.get("series") or [],
                   "legend": [{"t": "上涨日", "c": "#D9483F"}, {"t": "下跌日", "c": "#2E8B57"}, index_legend],
                   "note": "柱高是沪深成交额。灰线是上证指数收盘价。"}, open=True,
        ),
        _block(
            "etf", "宽基 ETF 份额", "有没有新钱买入", f"{_plain(etf.get('total'), 0)} 亿份", _age(etf.get("latest_date", ""), day, previous),
            etf.get("label") or "没有", {"净流入": "good", "净流出": "bad"}.get(etf.get("label"), "flat"),
            _mz_stats(etf.get("mz"), (1, 3, 5, 30, 60), _etf_marks(etf)),
            "每只单独估 Modified Z-Score。累计变化对照 n × M，MAD 按 √n 放大。净流入要 3 日 Mz > +1.5，30 日和 60 日同向或没有数，3 日累计至少 0.3%，且没有连续两天净流出。净流出要 3 日 Mz < −1.5，至少 2 天在卖。基线不足时，3 日超过 +0.6% 算流入、低于 −0.6% 算流出。拆分合并日不进计算。",
            story=f"最近一天份额 {_delta(etf.get('day_change'))} 亿份，3 日 {_pct(etf.get('ratio'), 2)}，净申购 {etf.get('buy_days', '—')} 天。大盘 {_yi(groups.get('大盘'), True)}，中小盘 {_yi(groups.get('中小盘'), True)}，科创创业 {_yi(groups.get('科创创业'), True)}。",
            pctile=_pctile_info("宽基 ETF 总份额处在一年里", etf.get("pctile")),
            chart={"kind": "area", "unit": "亿份", "points": _with_index(etf.get("series"), index_by_day),
                   "legend": [{"t": "当天净买入", "c": "#D9483F"}, {"t": "当天净卖出", "c": "#2E8B57"},
                              {"t": "总份额", "c": "#2457D6"}, index_legend],
                   "note": "蓝线是宽基总份额，底柱是当天净申购/赎回。灰线是上证指数收盘价。"},
            items=etf.get("items") or [], open=True,
        ),
        _block(
            "margin", "两融余额", "借钱炒股的人多不多", _yi(margin.get("balance")), _age(margin.get("date", ""), day, previous),
            margin.get("label") or "没有", {"在降": "bad", "加速下降": "bad", "在升": "good"}.get(margin.get("label"), "flat"),
            _mz_stats(margin.get("mz"), (1, 3, 5, 30, 60), _margin_marks(margin)),
            "Modified Z-Score，累计变化对照 n × M，MAD 按 √n 放大。加速下降要 3 日 Mz < −2.0 且 30 日 Mz < −1.0。在降要 3 日 Mz < −1.5，或 3 日 Mz < −1.0 且 30 日 Mz < −1.5。在升要 3 日 Mz > +1.5，且 30 日 Mz > −1.0。样本不足时不判升也不判降。1 日、5 日只展示。",
            story=f"融资买入占成交 {_pct(margin.get('share'))}；当日融资净买入 {_yi(margin.get('net'), True)}。",
            pctile=_pctile_info("两融余额处在一年里", margin.get("pctile")),
            chart={"kind": "area", "unit": "亿元", "points": _with_index(margin.get("series"), index_by_day),
                   "legend": [{"t": "当天净借入", "c": "#D9483F"}, {"t": "当天净还款", "c": "#2E8B57"},
                              {"t": "两融余额", "c": "#2457D6"}, index_legend],
                   "note": "蓝线是两融余额，底柱是当天净借入/还款。灰线是上证指数收盘价。"},
            open=True,
        ),
    ]
    where = [
        _block(
            "south", "南向资金", "A 股的钱有没有去港股", "没有" if south.get("ggt_net") is None else _delta(south["ggt_net"], 1), _age(south.get("ggt_date", ""), day, previous),
            "不改档位", "flat",
            _span_stats(south.get("windows"), _delta, 5),
            "大数字是港股通买入额 − 卖出额，单位沿用 ggt_daily 接口。北向资金已不按日披露，所以不看。不改上面的档位。",
            pctile=_pctile_info("近 20 日南下规模处在一年里", south.get("pctile20")),
            chart={"kind": "bars", "unit": "", "points": south.get("series") or [], "legend": ["当天净买入港股", "当天净卖出"], "signed": True},
            open=True,
        ),
        _block(
            "shibor", "银行间的钱", "钱贵不贵（Shibor 1 周）", "没有" if shibor.get("rate") is None else f"{shibor['rate']:.3f}%", _age(shibor.get("date", ""), day, previous),
            shibor.get("label") or "没有", {"偏松": "good", "偏紧": "bad"}.get(shibor.get("label"), "flat"),
            [_stat("1 周", "—" if shibor.get("rate") is None else f"{shibor['rate']:.3f}%", True), _stat("60 日均值", "—" if shibor.get("avg") is None else f"{shibor['avg']:.3f}%"), _stat("比较", shibor.get("side") or "—")],
            "这是 Shibor 1 周，当作 DR007 的近似，不是 DR007，也不是政策利率。央行净投放没接。",
        ),
    ]
    slow = [
        _block(
            "supply", "大股东增减持和回购", "增持 − 减持", _yi(supply.get("net"), True), "近 28 天",
            supply.get("label") or "没有", {"在拿走": "bad", "有人在接": "good"}.get(supply.get("label"), "flat"),
            [_stat("增持", _yi(supply.get("buy")), True), _stat("减持", _yi(supply.get("sell"))), _stat("回购累计", _yi(supply.get("repo")))],
            "回购按公司只保留最新一条实施公告，是累计金额，不是这 28 天的增量，所以图里只画增持和减持。定增和再融资没有全市场日接口。",
            chart={"kind": "weeks", "unit": "亿元", "points": supply.get("weeks") or [], "legend": ["增持", "减持"]},
            open=True,
        ),
        _block(
            "funds", "新发基金", "老百姓有没有买基金入市", "没有" if funds.get("count") is None else f"{funds['count']} 只", "月频",
            "只看方向", "flat",
            [_stat("发行份额", "多数没公布" if funds.get("amount") is None else f"{funds['amount']:.2f} 亿份", True), _stat("范围", funds.get("scope") or "—"), _stat("只数", "—" if funds.get("count") is None else str(funds["count"]))],
            "月度数据，只看方向，不参与短期判断。新开户数没有接口，不放。",
            chart={"kind": "months", "unit": "只", "points": funds.get("months") or [], "legend": ["新发偏股基金只数"]},
            open=True,
        ),
    ]
    return {
        "title": "钱有没有进来", "label": _money_word(money["tier"]), "tone": money["tone"],
        "age": _age(margin.get("date", ""), day, previous),
        "summary": money["head"]["summary"],
        "lead": "",
        "means": "",
        "when": _money_when(turn.get("label") or "", etf.get("label") or "", margin.get("label") or "", money["head"].get("label")),
        "sections": [
            {"title": "三个数看懂资金", "hint": "第一块是判断用的，点标题可收起", "blocks": core},
            {"title": "钱去哪了 · 钱贵不贵", "hint": "", "blocks": where},
            {"title": "变化慢一些的 · 周度 / 月度", "hint": "", "blocks": slow},
        ],
        "footnote": "数据没按时更新时（节假日、交易所延迟），对应的数会标日期并沿用上一次。",
    }


def _mood_tab(parts: dict) -> dict:
    from app.services import market_tape as tape

    mood = _mood_card(parts)
    breadth, limits = parts["breadth"], parts["limits"]
    age = _age(breadth.get("date") or limits.get("date") or "")
    stages = mood.get("stages") or []
    index_by = _index_by_day((parts.get("turnover") or {}).get("series"))
    sh_legend = {"t": "上证", "c": "#5B6372"}
    limit_pts = _with_index(
        [{"date": row["date"], "up": row.get("up") or 0, "down": row.get("down") or 0} for row in stages],
        index_by,
    )
    asof = (parts.get("_day") or breadth.get("date") or datetime.now(_ZONE).date().isoformat())[:10]
    breadth_pts = _with_index(
        list(breadth.get("series") or tape.advance_decline_series(date.fromisoformat(asof), 30)),
        index_by,
    )
    vol_pts = ((parts.get("turnover") or {}).get("series") or [])[-30:]
    stage_pts = _with_index(
        [{"date": row["date"], "stage": row.get("stage"), "up": row.get("up"), "down": row.get("down")} for row in stages],
        index_by,
    )
    trend_pts = [{"date": row["date"], "value": row["index"], "index": row["index"]} for row in vol_pts if row.get("index") is not None]
    return {
        "title": "好不好做", "label": mood["label"], "tone": mood["tone"], "age": age, "summary": mood["summary"],
        "means": mood["means"], "when": mood["when"], "indexes": mood["indexes"],
        "sections": [{"title": "看懂好不好做", "hint": "", "blocks": [
            _block(
                "trend", "大盘趋势", "指数在均线上还是下", mood["qa"][0]["a"], age, mood["qa"][0]["a"], mood["qa"][0]["tone"], [],
                "看 60 日线定方向，前低 = 今天之前 20 个交易日的最低收盘价，今天收在它下面算破位。",
                story=mood["qa"][0]["detail"], open=True,
                chart={"kind": "line", "unit": "点", "points": trend_pts, "legend": [sh_legend],
                       "note": f"近 {len(trend_pts)} 个交易日 · 上证收盘"} if trend_pts else None,
            ),
            _block(
                "breadth", "多数股票", "涨的多还是跌的多", f"{breadth.get('up', '—')} / {breadth.get('down', '—')}", age,
                mood["qa"][1]["a"], mood["qa"][1]["tone"],
                [_stat("上涨", str(breadth.get("up", "—")), True), _stat("下跌", str(breadth.get("down", "—"))),
                 _stat("新高 / 新低", f"{breadth.get('highs', '—')} / {breadth.get('lows', '—')}")],
                "全 A 不含 ST；新高、新低按近 25 个交易日。灰线是上证收盘。",
                story=mood["qa"][1]["detail"], open=True,
                chart={"kind": "limits", "unit": "家", "points": breadth_pts,
                       "legend": [{"t": "上涨", "c": "#D9483F"}, {"t": "下跌", "c": "#2E8B57"}, sh_legend],
                       "note": f"近 {len(breadth_pts) or 30} 个交易日 · 上涨 / 下跌家数"} if breadth_pts else None,
            ),
            _block(
                "limits", "涨停 / 跌停", "家数怎么走", f"涨停 {limits.get('up', '—')}", age, mood["qa"][2]["a"], mood["qa"][2]["tone"],
                [_stat("跌停", str(limits.get("down", "—"))), _stat("炸板", str(limits.get("broken", "—"))),
                 _stat("最高连板", str(limits.get("height", "—")), True)],
                "不含 ST、北交所；20cm（创业板、科创板）的涨跌幅折成 10cm 再平均。连板高度来自 limit_times。"
                "上图是近 30 个交易日每天的涨停、跌停家数；灰线是上证。追涨能不能赚钱看下面的判定。",
                story=mood["qa"][2]["detail"], open=True,
                chart={"kind": "limits", "unit": "家", "points": limit_pts,
                       "legend": [{"t": "涨停", "c": "#D9483F"}, {"t": "跌停", "c": "#2E8B57"}, sh_legend],
                       "note": f"近 {len(limit_pts) or 30} 个交易日 · 家数"} if limit_pts else None,
            ),
        ] + ([_block(
            "volume", "成交量", "放量还是缩量", mood["volume"]["word"], age, mood["volume"]["word"], "flat",
            [_stat("今天", _yi(mood["volume"]["amount"]), True), _stat("前 20 日均", _yi(mood["volume"]["base"])),
             _stat("近一年分位", "—" if mood["volume"]["pctile"] is None else f"{round(mood['volume']['pctile'] * 100)}%")],
            "今天成交额是前 20 个交易日均值的 1.2 倍以上算放量，0.8 倍以下算缩量；近一年分位在最低 10% 算地量，最高 10% 算天量。"
            "放量、缩量要和涨跌一起看。灰线是上证。不单独改「好不好做」的档位。",
            story=next((row["detail"] for row in mood["qa"] if row["q"] == "成交量怎么样？"), ""), open=True,
            chart={"kind": "bars", "unit": "亿元", "points": vol_pts,
                   "legend": [{"t": "上涨日", "c": "#D9483F"}, {"t": "下跌日", "c": "#2E8B57"}, sh_legend],
                   "note": f"近 {len(vol_pts)} 个交易日 · 亿元"},
        )] if mood.get("volume") else [])
          + ([_block(
              "theme", "涨停主线", "涨停集中在哪几个行业", mood["theme"]["word"], age, mood["theme"]["word"], mood["theme"]["tone"], [],
              "看今天涨停最集中的前三个行业，占全部涨停的多少；再看昨天的主线、以及昨涨停整体今天赚不赚钱。"
              "前三占 35% 以上且追涨没在亏钱，才算有主线；前三够集中但昨涨停整体或主线今天翻绿，只写有集中；"
              "20% 以下算没主线；昨天的主线今天平均亏过 1.5%，算主线在退，接力常在这时断掉。",
              story=mood["theme"]["detail"], open=True,
          )] if mood.get("theme") else [])
          + ([_block(
              "stage", "短线情绪", f"近 {len(stages) or 30} 个交易日每天所处阶段", mood["stage"], age, mood["stage"],
              mood["qa"][3]["tone"] if len(mood.get("qa") or []) > 3 else "flat", [],
              "冰点 → 修复 → 发酵 → 高潮 → 退潮，一圈通常几周。灰线是上证收盘。"
              "按当天的涨停数、跌停数、昨涨停今日平均涨跌（20cm 折成 10cm）、炸板率、最高连板和昨天主线今天的表现判，并且接着前一天走；"
              "高标断板、主线大面也算退潮的信号。",
              story=mood["qa"][3]["detail"] if len(mood.get("qa") or []) > 3 else "", open=True,
              chart={"kind": "stages", "points": stage_pts, "legend": [sh_legend]},
          )] if mood.get("stage") else [])}],
        "footnote": "「好不好做」看的是买进去容不容易赚钱，不看钱从哪来（那是「市场资金」和「国家队」）。",
    }


def _index_rows(parts: dict) -> list[dict]:
    rows = (parts.get("index") or {}).get("rows") or []
    return [
        {"name": row.get("name"), "close": row.get("close"), "pct": row.get("pct"),
         "ma60": row.get("above_ma60"), "broke_low": row.get("broke_low")}
        for row in rows
    ]


SECTOR_SCOPE = (
    f"覆盖申万 31 个一级行业：{len(market_sector.UNIVERSE)} 个有代表 ETF 的主题（钱 = 日度净流入÷规模的 {market_sector.CONCLUSION_DAYS} 日累计 Mz），"
    f"{len(market_sector.BASE)} 个没有合适 ETF 的行业按主力净流入÷成交额的同口径 Mz 估算；"
    f"另加 {len(market_sector.CONCEPTS)} 个常看题材（CPO / PCB / 光模块等，东财概念板，钱同估算口径，分组「题材」）；"
    "在攻=钱进且（连涨≥2、价相对大盘偏强、或放量且近几日不明显跌），放量优先排序；在出=钱出；"
    "行上露出钱/价 Mz 白话与净额、热度；今日关注仍是可跟/可盯短名单；左侧观察=dip；"
    "列表状态先价×热再钱、连续 2 日确认。份额晚一天公布。"
)


def _sector_tab(parts: dict, day: date) -> dict:
    rows = _sector_rows(parts["sectors"], parts.get("catalysts"))
    coverage = parts["sectors"].get("coverage") or {}
    boards = _sector_boards(rows)
    return {
        "title": "板块", "label": "", "tone": "flat", "age": _age(parts["sectors"].get("date") or "", day), "summary": _sector_headline(rows),
        "rows": rows, "boards": boards, "buckets": _sector_buckets(rows),
        "picks": _sector_picks(rows), "dip_watch": _sector_dip_watch(rows),
        "indexes": _index_rows(parts),
        "windows": parts["sectors"].get("windows"), "conclusion_days": parts["sectors"].get("conclusion_days"),
        "coverage": coverage, "coverage_l1": [{"l1": name, **item} for name, item in market_sector.coverage().items()],
        "thresholds": {"money_enter": rules.MONEY_ENTER, "money_hold": rules.MONEY_HOLD, "money_enter_base": rules.MONEY_ENTER_BASE,
                       "price_enter": rules.PRICE_ENTER, "hot": rules.SECTOR_HOT, "cold": rules.SECTOR_COLD,
                       "pos_high": rules.POS_HIGH, "pos_low": rules.POS_LOW, "confirm": rules.PHASE_CONFIRM,
                       "money_z": rules.MONEY_ENTER, "price_ex": rules.PRICE_ENTER,
                       "conclusion_days": market_sector.CONCLUSION_DAYS},
        "footnote": SECTOR_SCOPE + " 首页双榜看在攻/在出；今日关注才是可跟/可盯。不改大盘档位，也不推荐具体的票。",
    }


def sector_popup(name: str, today: date | None = None) -> dict:
    """首页点板块名：钱/热度 + 代表 ETF + 领涨/龙头个股。"""
    day = today or datetime.now(_ZONE).date()
    if market_sector._meta(name) is None:
        raise KeyError(name)
    summary = build_summary(day, record=False)
    rows = (summary.get("sectors") or {}).get("rows") or []
    row = next((item for item in rows if item.get("name") == name), None)
    return market_sector.popup(name, day, row)


def _cal_tab(parts: dict, day: date) -> dict:
    card = _calendar_card(parts, parts["watch"], day)
    return {
        "title": "日历", "label": card["label"], "tone": card["tone"], "age": _age(day.isoformat(), day),
        "summary": f"未来两周{card['label']}" + (f"，有 {card['mine_count']} 件涉及你的自选" if card["mine_count"] else ""),
        "rows": card["rows"], "unlock": card["unlock"], "days": (parts["calendar"] or {}).get("days") or [],
        "footnote": card["scope"],
    }


def _macro_tab(parts: dict) -> dict:
    card = _macro_card(parts)
    return {
        "title": "宏观", "label": card["label"], "tone": card["tone"], "age": card["month"],
        "summary": card["headline"], "card": card, "formula": card["formula"],
        "footnote": "两个容易误读的地方：估值便宜但钱没来，便宜决定能放多少，钱决定什么时候加；盈利好但货币没活，等 M1 拐头。",
    }


def display_amount(value) -> str:
    return _yi(value)


def display_pct(value) -> str:
    return _pct(value, 2)
