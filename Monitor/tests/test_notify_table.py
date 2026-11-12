import os
import tempfile
import unittest
from unittest.mock import patch

import tennis_court_monitor_all as monitor

TABLE = """[sungnam]

FAC101(구미)
주중
~09:00
18:00~ (T)
주말 (T)
All

[yongin]

기흥구
주중 (T)
~09:00
주말
All (T)
"""


class NotifyTableHeaderTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(TABLE)
        patcher = patch.object(monitor, "NOTIFY_TABLE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(os.remove, self.path)

    def test_sungnam_header_with_telegram_flag_keeps_sections_separate(self):
        fac = monitor.sn_load_notify_table()[0]
        self.assertEqual(fac["weekday_times"], ["~09:00", "18:00~"])
        self.assertEqual(fac["weekend_times"], ["ALL"])

    def test_yongin_header_with_telegram_flag_is_recognized(self):
        rule = monitor.yn_load_notify_table()["기흥구"]
        self.assertEqual(rule["weekday"], ["~09:00"])
        self.assertTrue(rule["weekend_all"])


if __name__ == "__main__":
    unittest.main()
