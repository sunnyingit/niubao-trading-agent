"""板块双榜：连涨天数、入选门槛、Mz 白话。"""
from __future__ import annotations

import unittest


class UpDaysTest(unittest.TestCase):
    def test_counts_strict_consecutive_up_closes(self):
        from app.services.market_sector import up_days

        closes = [
            ("2026-01-01", 10.0),
            ("2026-01-02", 10.5),
            ("2026-01-03", 10.8),
            ("2026-01-04", 10.8),  # flat breaks
            ("2026-01-05", 11.0),
            ("2026-01-06", 11.2),
        ]
        self.assertEqual(up_days(closes), 2)
        self.assertEqual(up_days([("2026-01-01", 10.0)]), 0)
        self.assertEqual(up_days([]), 0)


class BoardWordingTest(unittest.TestCase):
    def test_mz_plain_ladders(self):
        from app.services.market_page import _mz_plain, _heat_plain

        self.assertIn("资金进出一般", _mz_plain(0.4, "money"))
        self.assertIn("资金偏进", _mz_plain(1.8, "money"))
        self.assertIn("+1.8", _mz_plain(1.8, "money"))
        self.assertIn("资金明显在出", _mz_plain(-3.2, "money"))
        self.assertIn("比大盘强", _mz_plain(1.2, "price"))
        self.assertIn("放量", _heat_plain(1.4))
        self.assertIn("不热", _heat_plain(0.9))

    def test_rising_and_exiting_membership(self):
        from app.services.market_page import _sector_boards

        rows = [
            {"name": "半导体", "source": "etf", "money_z": 2.0, "price_z": 0.2, "up_days": 3, "heat": 1.5, "ret5": 0.02, "flow": 12.0},
            {"name": "仅价强", "source": "etf", "money_z": 1.2, "price_z": 1.1, "up_days": 0, "heat": 0.9, "ret5": 0.01, "flow": 3.0},
            {"name": "放量未连涨", "source": "etf", "money_z": 1.5, "price_z": 0.2, "up_days": 0, "heat": 1.4, "ret5": 0.0, "flow": 5.0},
            {"name": "放量但在跌", "source": "etf", "money_z": 1.5, "price_z": 0.2, "up_days": 0, "heat": 1.4, "ret5": -0.02, "flow": 5.0},
            {"name": "煤炭", "source": "etf", "money_z": -2.1, "price_z": -1.0, "up_days": 0, "heat": 1.0, "ret5": -0.02, "flow": -8.0},
            {"name": "估算严", "source": "industry", "money_z": 1.1, "price_z": 1.5, "up_days": 2, "heat": 1.0, "ret5": 0.01, "flow": 1.0},
        ]
        boards = _sector_boards(rows)
        rising = [item["name"] for item in boards["rising"]]
        exiting = [item["name"] for item in boards["exiting"]]
        self.assertEqual(rising[0], "半导体")  # 放量优先
        self.assertIn("仅价强", rising)
        self.assertIn("放量未连涨", rising)
        self.assertNotIn("放量但在跌", rising)  # 价未齐不补进在攻
        self.assertNotIn("估算严", rising)  # industry 门槛 1.3
        self.assertEqual(exiting[0], "煤炭")
        tip = boards["rising"][0]["money_tip"]
        self.assertIn("资金偏进", tip)
        self.assertIn("+2.0", tip)
        self.assertTrue(boards["exiting"][0]["weak_price"])
        # 涨 / 跌：只看自身近窗涨跌，和在攻/在出可不完全重合
        up = [item["name"] for item in boards["up"]]
        down = [item["name"] for item in boards["down"]]
        self.assertEqual(up[0], "半导体")
        self.assertIn("仅价强", up)
        self.assertNotIn("放量未连涨", up)  # ret5==0 不算涨
        self.assertEqual(down[:2], ["放量但在跌", "煤炭"])  # 同为 -2%，保持原序
        self.assertNotIn("放量未连涨", down)


if __name__ == "__main__":
    unittest.main()
