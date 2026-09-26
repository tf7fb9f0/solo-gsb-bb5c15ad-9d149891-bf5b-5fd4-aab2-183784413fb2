"""Tests for CSV parsing and audit export."""
import csv
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.csv_io import CSVParseError, build_audit_csv, parse_scores
from app.scoring import analyze

HEADER = "选手,评委,项目,分数\n"


def with_ids(records):
    for i, r in enumerate(records, 1):
        r["id"] = i
    return records


class TestParse(unittest.TestCase):
    def test_chinese_header(self):
        recs = parse_scores(HEADER + "张三,甲,独唱,9.5\n李四,甲,独唱,8.0\n")
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["contestant"], "张三")
        self.assertAlmostEqual(recs[0]["score"], 9.5)

    def test_english_header_and_bom(self):
        content = ("﻿" "contestant,judge,item,score\n"
                   "A,J1,P1,7\nB,J1,P1,6\n")
        recs = parse_scores(content.encode("utf-8"))
        self.assertEqual({r["judge"] for r in recs}, {"J1"})
        self.assertEqual(recs[1]["contestant"], "B")

    def test_missing_column(self):
        with self.assertRaises(CSVParseError) as ctx:
            parse_scores("选手,评委,分数\nA,J,9\n")
        self.assertIn("项目", str(ctx.exception))

    def test_bad_number(self):
        with self.assertRaises(CSVParseError) as ctx:
            parse_scores(HEADER + "A,J,P,好\n")
        self.assertIn("第 2 行", str(ctx.exception))

    def test_duplicate_triple(self):
        with self.assertRaises(CSVParseError) as ctx:
            parse_scores(HEADER + "A,J,P,9\nA,J,P,8\n")
        self.assertIn("重复", str(ctx.exception))

    def test_empty_value(self):
        with self.assertRaises(CSVParseError):
            parse_scores(HEADER + "A,J,P,\n")

    def test_blank_lines_ignored(self):
        recs = parse_scores(HEADER + "\nA,J,P,9\n\n  \nB,J,P,7\n")
        self.assertEqual(len(recs), 2)


class TestExport(unittest.TestCase):
    def test_export_has_sections_and_choices(self):
        records = with_ids(parse_scores(
            HEADER +
            "C1,JA,P1,9\nC2,JA,P1,1\n"
            "C1,JB,P1,4\nC2,JB,P1,5\n"
            "C1,JA,P2,9\nC2,JA,P2,1\n"
            "C1,JB,P2,4\nC2,JB,P2,5\n"
        ))
        decisions = {1: (True, "该评委现场核实")}
        analysis = analyze(records, decisions, "judge")
        payload = build_audit_csv(analysis)
        self.assertTrue(payload.startswith(b"\xef\xbb\xbf"))
        text = payload.decode("utf-8-sig")
        for section in ("【明细】", "【汇总】", "【反转】", "【校准参数】"):
            self.assertIn(section, text)
        self.assertIn("保留原分", text)
        self.assertIn("采用校准分", text)
        self.assertIn("该评委现场核实", text)

        # Must itself be parseable CSV
        rows = list(csv.reader(io.StringIO(text)))
        self.assertGreater(len(rows), 10)


if __name__ == "__main__":
    unittest.main()
