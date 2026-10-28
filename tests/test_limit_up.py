import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import date, datetime

import sync_limit_up as app


class LimitUpTests(unittest.TestCase):
    def row(self, streak=1):
        return {"c": "000001", "n": "测试股票", "p": 12340, "lbc": streak,
                "fbt": 93001, "lbt": 145959, "zttj": {"days": 4, "ct": 3}}

    def test_consecutive_classification_not_multi_day_pattern(self):
        for streak, group in [(1, "1"), (2, "2"), (3, "3"), (5, "4+"), (None, "unknown"), (1.5, "unknown")]:
            with self.subTest(streak=streak):
                result = app.normalize(self.row(streak), {"000001": {"high_days": "4天3板"}}, "")
                self.assertEqual(result["group"], group)
                self.assertEqual(result["price"], 12.34)
                self.assertEqual(result["first_limit_time"], "09:30:01")
                self.assertEqual(result["reason_status"], "missing")

    def test_reason_join_by_code_and_missing_not_inferred(self):
        reasons = {"000001": {"reason_type": "业绩增长+机器人"}}
        result = app.normalize(self.row(), reasons, "https://example.com")
        self.assertEqual(result["reason"], "业绩增长+机器人")
        self.assertEqual(result["reason_status"], "available")
        self.assertEqual(app.normalize({**self.row(), "c": "000002"}, reasons, "")["reason_status"], "missing")

    def test_pool_date_and_count_validation(self):
        for data in [{"qdate": 20261027, "tc": 1, "pool": [self.row()]},
                     {"qdate": 20261028, "tc": 2, "pool": [self.row()]}]:
            with patch.object(app, "fetch_json", return_value={"rc": 0, "data": data}):
                with self.assertRaises(ValueError):
                    app.fetch_pool(date(2026, 10, 28))

    def test_reason_date_mismatch_rejected(self):
        with patch.object(app, "fetch_json", return_value={"status_code": 0, "data": {"date": "20261027"}}):
            with self.assertRaises(ValueError):
                app.fetch_reasons(date(2026, 10, 28))

    def test_reason_pagination(self):
        responses = [{"status_code": 0, "data": {"date": "20261028", "page": {"total": 2, "limit": 1}, "info": [{"code": code, "reason_type": "业绩增长"}]}} for code in ["000001", "000002"]]
        with patch.object(app, "fetch_json", side_effect=responses) as fetch:
            result, _ = app.fetch_reasons(date(2026, 10, 28))
            self.assertEqual(len(result), 2)
            self.assertIn("page=2", fetch.call_args.args[0])

    def test_reason_failure_preserves_pool_and_warns(self):
        with patch.object(app, "fetch_pool", return_value=([self.row()], "https://example.com")), patch.object(app, "fetch_reasons", side_effect=ValueError("拒绝访问")):
            result = app.collect(datetime.now(app.CHINA).date(), False)
            self.assertTrue(result["warnings"])
            self.assertEqual(result["stocks"][0]["reason_status"], "missing")

    def test_date_fallback_and_explicit_no_fallback(self):
        with patch.object(app, "fetch_pool", side_effect=[(None, ""), ([self.row()], "source")]), patch.object(app, "fetch_reasons", return_value=({"000001": {"latest": 12.34}}, "")):
            result = app.collect(date(2026, 10, 28), True)
            self.assertEqual(result["date"], "2026-10-27")
            self.assertEqual(result["requested_date"], "2026-10-28")
        with patch.object(app, "fetch_pool", return_value=(None, "")) as pool:
            with self.assertRaises(ValueError):
                app.collect(date(2026, 10, 28), False)
            self.assertEqual(pool.call_count, 1)

    def test_historical_pool_requires_same_day_crosscheck(self):
        with patch.object(app, "fetch_pool", return_value=([self.row()], "source")), patch.object(app, "fetch_reasons", return_value=({}, "")):
            with self.assertRaises(ValueError):
                app.collect(date(2026, 10, 27), False)
        with patch.object(app, "fetch_json", return_value={"rc": 0, "data": {"qdate": 20261028, "tc": 1, "pool": [self.row()]}}):
            rows, _ = app.fetch_pool(date(2026, 10, 27))
            self.assertEqual(len(rows), 1)

    def test_failed_fetch_does_not_publish(self):
        with patch.object(app, "collect", side_effect=OSError("network unavailable")), patch.object(app, "publish") as publish:
            self.assertEqual(app.main([]), 1)
            publish.assert_not_called()

    def test_history_and_script_injection_escaping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "limit-up.template.html").write_text('<script type="application/json">__LIMIT_UP_DATA__</script>', encoding="utf-8")
            first = {"date": "2026-10-27", "stocks": [], "text": "</script><script>alert(1)</script>"}
            second = {"date": "2026-10-28", "stocks": []}
            app.publish(first, root)
            result = app.publish(second, root)
            self.assertEqual(len(result["days"]), 2)
            self.assertEqual(result["latest_date"], "2026-10-28")
            content = (root / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>alert", content)
            self.assertEqual(content, (root / "股票分析工作台.html").read_text(encoding="utf-8"))
            saved = json.loads((root / "data/limit-up/2026-10-27.json").read_text(encoding="utf-8"))
            self.assertEqual(saved, first)


if __name__ == "__main__":
    unittest.main()
