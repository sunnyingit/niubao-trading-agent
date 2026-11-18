"""大盘维度 facts 投影。"""

from __future__ import annotations

from app.services.market_detail import PARTS, project_facts, slim_tab
from app.services.market_page import (
    _analyst_tape_facts,
    _index_streak_bits,
    _money_with_tape_text,
)
from app.services.market_tape import format_breadth


def test_project_facts_from_summary():
    summary = {
        "today": "2026-11-08",
        "generated_at": "2026-11-08 15:00",
        "indexes": [{"name": "上证指数", "pct": 1.2}],
        "conclusion": {
            "level": "顺风",
            "tilt": "",
            "headline": "钱在进、好不好做偏强",
            "action": "可以顺着做",
            "gate_text": "成交不冷清",
            "signals": [
                {"key": "money", "name": "钱", "label": "在进", "text": "宽基净流入"},
                {"key": "team", "name": "国家队", "label": "没托市", "text": "没有集中申购"},
                {"key": "mood", "name": "好不好做", "label": "偏强", "text": "多数股票上涨"},
            ],
        },
        "money": {"label": "在进", "summary": "宽基净流入且成交不冷清"},
        "team": {"label": "没托市", "summary": "没有集中申购"},
        "mood": {"label": "偏强", "summary": "多数股票上涨"},
        "sectors": {"headline": "电子、通信在主升"},
    }
    facts = project_facts(summary)
    ids = {f["id"] for f in facts}
    assert "mk.conclusion.level" in ids
    assert "mk.signal.money" in ids
    assert "mk.sector.headline" in ids
    assert any("顺风" in f["text"] for f in facts)
    assert any("+1.2%" in f["text"] for f in facts)


def test_project_facts_tape_streak_and_money():
    summary = {
        "indexes": [{"name": "上证指数", "pct": -1.2}],
        "conclusion": {"level": "逆风", "signals": []},
        "analyst": {
            "index_streak": "上证连跌 4 日，近 5 日累计 −3.2%",
            "trend": "上证连跌 4 日；主要指数 0/4 在 60 日线上",
            "rebound": "上证近 20 日 −3.4%；连跌中反弹需先看到宽基回暖",
            "money_with_tape": "同期宽基净赎回约 12 亿 / 净申购 1 天",
            "mood_stage": "短线情绪处在退潮；上涨占 21%；炸板率 33%",
            "breadth_structure": "上涨 1000 / 下跌 4000（占 20%）；全 A 约 18% 站上 20 日线",
            "style_rotation": "在攻：银行；在出：房地产、化工",
        },
        "mood": {"label": "偏逆", "summary": "上涨家数偏少"},
        "sectors": {"headline": "在攻未进推荐：银行"},
    }
    facts = project_facts(summary)
    by_id = {f["id"]: f["text"] for f in facts}
    assert by_id["mk.index.streak"] == "上证连跌 4 日，近 5 日累计 −3.2%"
    assert "−1.2%" in by_id["mk.index.tape"]
    assert "60 日线" in by_id["mk.index.trend"]
    assert "20 日" in by_id["mk.index.rebound"]
    assert by_id["mk.money.with_tape"].startswith("同期宽基")
    assert "退潮" in by_id["mk.mood.stage"]
    assert "站上 20 日线" in by_id["mk.breadth.structure"]
    assert "银行" in by_id["mk.style.rotation"]


def test_project_facts_skips_blank_analyst():
    facts = project_facts({
        "conclusion": {"level": "中性", "signals": []},
        "analyst": {
            "index_streak": "", "money_with_tape": "", "mood_stage": "",
            "trend": "", "rebound": "", "breadth_structure": "", "style_rotation": "",
        },
    })
    ids = {f["id"] for f in facts}
    assert "mk.index.streak" not in ids
    assert "mk.money.with_tape" not in ids
    assert "mk.mood.stage" not in ids
    assert "mk.index.trend" not in ids
    assert "mk.breadth.structure" not in ids


def test_index_streak_bits_down_window():
    # index_pct 与成交额切片一致：pct_chg 百分数（-1.0 = −1%）
    series = [
        {"index_pct": 1.0},
        {"index_pct": -1.0},
        {"index_pct": -0.8},
        {"index_pct": -1.2},
        {"index_pct": -0.5},
    ]
    got = _index_streak_bits(series)
    assert got["side"] == "down"
    assert got["streak"] == 4
    assert "连跌 4 日" in got["text"]
    assert got["window"] == 4
    assert 5 in got["cum"]
    # 累计应是小数，再用 _pct 显示，约 −2%~−4% 量级，绝不是 −97%
    assert -0.05 < got["cum"][5] < -0.02
    assert "累计 −2." in got["text"] or "累计 -2." in got["text"]


def test_index_streak_bits_empty_no_invent():
    assert _index_streak_bits([])["text"] == ""
    assert _index_streak_bits(None)["text"] == ""


def test_money_with_tape_aligns_to_window():
    etf = {
        "money": -12.3,
        "buy_days": 1,
        "series": [
            {"delta": -1.0},
            {"delta": -2.0},
            {"delta": 0.5},
            {"delta": -3.0},
        ],
    }
    # 窗口 3：优先用金额
    text3 = _money_with_tape_text(etf, 3)
    assert "净赎回约 12 亿" in text3
    assert "净申购 1 天" in text3
    # 窗口 4：用份额合计
    text4 = _money_with_tape_text(etf, 4)
    assert "近 4 日" in text4
    assert "亿份" in text4


def test_analyst_tape_facts_from_parts():
    parts = {
        "turnover": {
            "series": [
                {"index_pct": 1.0},
                {"index_pct": -1.0},
                {"index_pct": -1.0},
                {"index_pct": -1.0},
            ],
        },
        "etf": {
            "money": -8.0,
            "buy_days": 0,
            "series": [{"delta": -1}, {"delta": -2}, {"delta": -1.5}],
        },
        "breadth": {
            "up": 1000, "down": 4000,
            "above_ma20_share": 0.18, "above_ma60_share": 0.22,
        },
        "limits": {"broken_rate": 0.36, "up": 40, "down": 12, "height": 4},
        "index": {"rows": [
            {"code": "000001.SH", "name": "上证指数", "ret5": -0.02, "ret20": -0.034, "above_ma20": False, "above_ma60": False},
            {"code": "000300.SH", "name": "沪深300", "ret5": -0.015, "ret20": -0.04, "above_ma20": False, "above_ma60": False},
            {"code": "000852.SH", "name": "中证1000", "ret5": -0.05, "ret20": -0.08, "above_ma20": False, "above_ma60": False},
            {"code": "399006.SZ", "name": "创业板指", "ret5": -0.06, "ret20": -0.15, "above_ma20": False, "above_ma60": False, "broke_low": True},
        ]},
    }
    mood = {"stage": "退潮"}
    sectors = {"boards": {
        "rising": [{"name": "银行"}],
        "exiting": [{"name": "房地产"}],
        "up": [{"name": "银行"}],
        "down": [{"name": "通信"}],
    }}
    got = _analyst_tape_facts(parts, mood, sectors_card=sectors)
    assert "连跌" in got["index_streak"]
    assert "累计" in got["index_streak"]
    assert got["money_with_tape"]
    assert "退潮" in got["mood_stage"]
    assert "上涨占" in got["mood_stage"]
    assert "炸板率" in got["mood_stage"]
    assert "60 日线" in got["trend"]
    assert "创业板" in got["rebound"] or "破近低" in got["rebound"]
    assert "站上 20 日线" in got["breadth_structure"]
    assert "小盘" in got["breadth_structure"]
    assert "银行" in got["style_rotation"]


def test_format_breadth_ma_share():
    today = [{"ts_code": "1.SZ", "pct_chg": 1, "close": 12}]
    # 60 日历史收于 10，今天 12 → 站上 20/60
    hist = {"1.SZ": [10.0] * 60}
    got = format_breadth(today, hist)["metrics"]
    assert got["above_ma20"] == 1 and got["ma20_n"] == 1
    assert got["above_ma60"] == 1 and got["ma60_n"] == 1
    assert got["above_ma20_share"] == 1.0


def test_slim_tab_fund_from_sections():
    tab = {
        "title": "钱有没有进来",
        "label": "在进",
        "summary": "宽基净流入",
        "sections": [{
            "title": "三个数",
            "blocks": [
                {"key": "turnover", "title": "成交额", "judge": "活跃", "big": "1.2 万亿", "story": "比平时热"},
            ],
        }],
    }
    facts, blocks, text = slim_tab("fund", tab)["facts"], slim_tab("fund", tab)["blocks"], slim_tab("fund", tab)["text"]
    assert facts[0]["id"] == "mk.fund.summary"
    assert any(b.get("title") == "成交额" for b in blocks)
    assert "宽基净流入" in text


def test_parts_cover_market_tabs():
    assert PARTS == ("fund", "team", "sector", "mood", "cal", "macro")
