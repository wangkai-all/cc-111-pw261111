"""Regression tests for rule preparation, matching and the HTTP API."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app import keys as K
from app import matching
from app import samples as S
from app import service
from app import server
from app import store


def load_side(side: str, encoding: str | None = None) -> dict:
    info = S.sample_text(side, encoding)
    sheet = service.parse_table(str(info["text"]), str(info["filename"]),
                                str(info["read_encoding"]))
    return service.sheet_to_side(side, str(info["filename"]),
                                 str(info["read_encoding"]), sheet)


BASE_RULES = dict(K.DEFAULT_RULES)
CLEANING = {
    **BASE_RULES,
    "invisible": "strip",
    "whitespace": "trim_unicode",
    "width": "fullwidth_ascii",
    "normalization": "NFC",
}


LOWER = {**CLEANING, "case": "unicode_lower"}


def analyze(rules=None, decisions=None, secondary=("size",)):
    return service.run_analysis({
        "sides": {"A": load_side("A"), "B": load_side("B")},
        "rules": rules or CLEANING,
        "key_columns": {"A": 0, "B": 0},
        "secondary_fields": list(secondary),
        "decisions": decisions or {},
    })


class PrepareKeyTests(unittest.TestCase):
    def test_default_rules_never_change_values(self):
        for raw in S.A_ROWS + S.B_ROWS:
            for cell in raw:
                prepared = K.prepare_key(cell, K.DEFAULT_RULES)
                self.assertEqual(prepared["clean"], cell)
                self.assertEqual(prepared["steps"], [])

    def test_each_rule_is_independent(self):
        raw = "　ＡＢＣ－００１"
        width_only = K.prepare_key(raw, {**BASE_RULES, "width": "fullwidth_ascii"})
        self.assertEqual(width_only["clean"], " ABC-001")
        self.assertEqual(width_only["steps"][0]["rule"], "width")

        trim_only = K.prepare_key(raw, {**BASE_RULES, "whitespace": "trim_unicode"})
        self.assertEqual(trim_only["clean"], "ＡＢＣ－００１")
        self.assertEqual(trim_only["steps"][0]["rule"], "whitespace")

    def test_invisible_characters_kept_by_default(self):
        raw = "​TAB-009"
        self.assertEqual(K.prepare_key(raw, BASE_RULES)["clean"], raw)
        stripped = K.prepare_key(raw, {**BASE_RULES, "invisible": "strip"})
        self.assertEqual(stripped["clean"], "TAB-009")
        self.assertIn("U+200B", stripped["steps"][0]["detail"])

    def test_nbsp_needs_unicode_trim(self):
        raw = "SKU 007\xa0"
        self.assertEqual(K.prepare_key(raw, {**BASE_RULES, "whitespace": "trim"})["clean"], raw)
        self.assertEqual(
            K.prepare_key(raw, {**BASE_RULES, "whitespace": "trim_unicode"})["clean"],
            "SKU 007")

    def test_turkish_case_differs_from_default_lower(self):
        turkish = {**BASE_RULES, "case": "turkish_lower"}
        unicode_lower = {**BASE_RULES, "case": "unicode_lower"}
        self.assertEqual(K.prepare_key("I", turkish)["clean"], "ı")
        self.assertEqual(K.prepare_key("İ", turkish)["clean"], "i")
        self.assertEqual(K.prepare_key("I", unicode_lower)["clean"], "i")
        # Default Turkish lowercase keeps the combining dot from İ.
        self.assertEqual(K.prepare_key("İ", unicode_lower)["clean"], "i̇")

    def test_casefold_can_merge_meanings(self):
        prepared = K.prepare_key("ß", {**BASE_RULES, "case": "casefold"})
        self.assertEqual(prepared["clean"], "ss")

    def test_leading_zeros(self):
        self.assertEqual(K.prepare_key("00123", BASE_RULES)["clean"], "00123")
        self.assertEqual(
            K.prepare_key("00123", {**BASE_RULES, "leading_zeros": "strip_numeric"})["clean"],
            "123")
        self.assertEqual(
            K.prepare_key("SKU-007A",
                          {**BASE_RULES, "leading_zeros": "strip_tokens"})["clean"],
            "SKU-7A")
        # A mixed key is untouched by strip_numeric.
        self.assertEqual(
            K.prepare_key("A001", {**BASE_RULES, "leading_zeros": "strip_numeric"})["clean"],
            "A001")

    def test_encoding_damage_can_skip_cells(self):
        prepared = K.prepare_key("CAF�0‡7-01", BASE_RULES)
        self.assertTrue(prepared["excluded"])
        self.assertEqual(prepared["exclude_reason"], "encoding_invalid")
        kept = K.prepare_key("CAF�-01",
                             {**BASE_RULES, "encoding_invalid": "keep"})
        self.assertFalse(kept["excluded"])
        self.assertTrue(kept["warnings"])

    def test_blank_keys_excluded_and_flagged(self):
        for raw in ("", " ", "　", "​"):
            prepared = K.prepare_key(raw, CLEANING)
            self.assertTrue(prepared["blank"], repr(raw))
            self.assertTrue(prepared["excluded"])

    def test_rules_validated(self):
        with self.assertRaises(ValueError):
            K.validate_rules({"bogus": "x"})
        with self.assertRaises(ValueError):
            K.validate_rules({"case": "upper"})
        self.assertEqual(K.validate_rules(None), K.DEFAULT_RULES)


class MatchingTests(unittest.TestCase):
    def test_pending_collisions_block_pairing(self):
        analysis = analyze()
        self.assertGreater(analysis["summary"]["collisions_pending"], 0)
        self.assertGreater(analysis["summary"]["blocked_pairs"], 0)
        for match in analysis["matches"]:
            self.assertNotIn(match["a"], [
                row["row_no"] for row in analysis["sides"]["A"]["rows"]
                if row["status"] == "blocked"])
        collision = next(c for c in analysis["collisions"]
                         if "ＡＢＣ－００１" in [v["raw"] for v in c["variants"]])
        self.assertEqual(collision["decision"], "pending")
        self.assertEqual(collision["possible_matches"], len(collision["row_nos"]) *
                         len(collision["counterpart_row_nos"]))

    def test_merge_then_requires_secondary_field(self):
        analysis = analyze(rules=LOWER)
        group = next(c for c in analysis["collisions"]
                     if c["side"] == "A" and c["clean_key"] == "abc-001")
        other = next(c for c in analysis["collisions"]
                     if c["side"] == "B" and c["clean_key"] == "abc-001")
        merged = analyze(rules=LOWER,
                         decisions={group["id"]: "merge", other["id"]: "merge"})
        card = next(g for g in merged["duplicate_groups"] if g["key"] == "abc-001")
        self.assertEqual((card["a_count"], card["b_count"]), (2, 3))
        self.assertEqual(card["possible"], 6)
        self.assertEqual(card["secondary_matches"], 4)
        self.assertEqual(card["status"], "ambiguous")
        # Preview lists only pairs whose secondary fields all agree.
        self.assertEqual(len(card["candidates"]), card["secondary_matches"])
        self.assertEqual(len(card["candidates"]), 4)

    def test_secondary_field_can_confirm_single_pair(self):
        analysis = analyze(rules=LOWER, secondary=["size"])
        tab = next(m for m in analysis["matches"] if m["key"] == "tab-009")
        self.assertEqual(tab["mode"], "secondary")

    def test_separate_decision_uses_raw_key(self):
        analysis = analyze(rules=LOWER)
        group = next(c for c in analysis["collisions"]
                     if c["side"] == "A" and c["clean_key"] == "abc-001")
        separated = analyze(rules=LOWER, decisions={group["id"]: "separate"})
        rows = {row["row_no"]: row for row in separated["sides"]["A"]["rows"]}
        self.assertEqual(rows[1]["match_key_kind"], "raw")
        self.assertEqual(rows[1]["match_key"], "ＡＢＣ－００１")

    def test_skip_decision_excludes_rows(self):
        analysis = analyze(rules=LOWER)
        group = next(c for c in analysis["collisions"] if c["side"] == "B"
                     and c["clean_key"] == "sku 007")
        skipped = analyze(rules=LOWER, decisions={group["id"]: "skip"})
        for row_no in group["row_nos"]:
            item = next(row for row in skipped["sides"]["B"]["rows"]
                        if row["row_no"] == row_no)
            self.assertTrue(item["excluded"])
            self.assertEqual(item["exclude_reason"], "collision_skip")

    def test_collision_id_is_stable_across_clean_key_changes(self):
        first = analyze()
        second = analyze(rules={**CLEANING, "diacritics": "strip"})
        ids_first = {c["id"] for c in first["collisions"]}
        ids_second = {c["id"] for c in second["collisions"]}
        # ＡＢＣ－００１ / ABC-001 collide under both, so the decision id holds.
        self.assertTrue(ids_first & ids_second)
        self.assertTrue(all("|" in cid for cid in ids_first))

    def test_no_duplicate_statuses(self):
        analysis = analyze(rules={**CLEANING, "leading_zeros": "strip_numeric"})
        for side in ("A", "B"):
            statuses = {row["status"] for row in analysis["sides"][side]["rows"]}
            self.assertTrue(statuses)
            self.assertTrue(statuses <= {"matched_unique", "matched_secondary",
                                         "ambiguous", "blocked", "unmatched",
                                         "excluded"}, statuses)
            if side == "A":
                self.assertIn("excluded", statuses)

    def test_empty_secondary_keeps_duplicates_ambiguous(self):
        analysis = analyze(rules=LOWER, secondary=[])
        card = next(g for g in analysis["duplicate_groups"] if g["key"] == "ABC-002" or g["key"] == "abc-002")
        self.assertEqual(card["status"], "ambiguous")
        self.assertEqual(card["reason"], "duplicate_keys")

    def test_export_covers_every_row(self):
        analysis = analyze()
        filename, text = service.build_export(analysis)
        self.assertTrue(filename.endswith(".csv"))
        lines = text.splitlines()
        self.assertGreater(len(lines), 1)
        header = lines[0].split(",")
        self.assertIn("rules_fingerprint", header)
        self.assertIn("a_rule_steps", header)
        rows = [line for line in lines[1:] if line]
        self.assertTrue(rows)
        for needle in ("matched", "collision_pending", "unmatched", "excluded"):
            self.assertTrue(any(line.startswith(needle + ",") for line in rows),
                            needle)

    def test_export_neutralises_formula_prefix(self):
        sides = {"A": {"label": "A", "filename": "a.csv", "encoding": "utf-8",
                       "columns": [{"index": 0, "name": "code"}],
                       "rows": [{"row_no": 1, "cells": ["=HYPERLINK(1)"]}]},
                 "B": {"label": "B", "filename": "b.csv", "encoding": "utf-8",
                       "columns": [{"index": 0, "name": "code"}],
                       "rows": [{"row_no": 1, "cells": ["=1+1"]}]}}
        analysis = matching.analyze(sides, K.DEFAULT_RULES,
                                    {"key_columns": {"A": 0, "B": 0}},
                                    {})
        _, text = service.build_export(analysis)
        body = [line for line in text.splitlines()[1:] if line]
        for line in body:
            self.assertFalse(line.startswith("matched,="), line)

    def test_diff_reports_added_and_removed(self):
        base_analysis = analyze(rules={**CLEANING, "case": "none"})
        version = {
            "id": "vtest",
            "label": "baseline",
            "created_at": "2026-01-01T00:00:00Z",
            "rules_fingerprint": base_analysis["rules_fingerprint"],
            "data_signature": {side: base_analysis["sides"][side]["data_signature"]
                               for side in ("A", "B")},
            "pairs": [{"a": m["a"], "b": m["b"], "key": m["key"],
                       "mode": m["mode"]} for m in base_analysis["matches"]],
        }
        changed = service.matching.analyze(
            {"A": load_side("A"), "B": load_side("B")},
            {**CLEANING, "case": "unicode_lower", "diacritics": "strip"},
            {"key_columns": {"A": 0, "B": 0}, "secondary_fields": ["size"]},
            {}, version)
        diff = changed["diff"]
        self.assertFalse(diff["data_changed"])
        self.assertGreater(diff["added_count"] + diff["removed_count"], 0)
        for record in diff["added"]:
            self.assertEqual(record["status"], "matched")
        for record in diff["removed"]:
            self.assertEqual(record["status"], "removed")

    def test_diff_flags_conflicts(self):
        base_analysis = analyze()
        version = {
            "id": "vconflict",
            "label": "with secondary",
            "created_at": "2026-01-01T00:00:00Z",
            "rules_fingerprint": base_analysis["rules_fingerprint"],
            "data_signature": {},
            "pairs": [{"a": m["a"], "b": m["b"], "key": m["key"],
                       "mode": m["mode"]} for m in base_analysis["matches"]],
        }
        conflict_analysis = analyze(rules={**CLEANING, "case": "unicode_lower"})
        merged = service.matching.analyze(
            {"A": load_side("A"), "B": load_side("B")},
            {**CLEANING, "case": "unicode_lower", "diacritics": "strip"},
            {"key_columns": {"A": 0, "B": 0}, "secondary_fields": ["size"]},
            {}, version)
        self.assertLessEqual(merged["diff"]["conflict_count"],
                             conflict_analysis["summary"]["confirmed_pairs"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "test.sqlite3"
        store.configure(self.tmp)
        store.init_db()

    def tearDown(self):
        store.configure(store.Path(store.__file__).resolve().parent.parent
                        / "data" / "linker.sqlite3")

    def test_version_roundtrip(self):
        analysis = analyze()
        version = store.create_version(
            "tester", "v1", analysis["rules"], analysis["config"], {},
            {side: analysis["sides"][side]["data_signature"] for side in "AB"},
            None, fingerprint=analysis["rules_fingerprint"])
        store.save_version_pairs(version["id"], analysis["matches"])
        loaded = store.get_version(version["id"])
        self.assertEqual(loaded["label"], "v1")
        self.assertEqual(loaded["rules_fingerprint"],
                         analysis["rules_fingerprint"])
        pairs = store.get_version_pairs(version["id"])
        self.assertEqual(len(pairs), len(analysis["matches"]))
        self.assertEqual(store.list_versions()[0]["run_count"], 0)

    def test_run_snapshot_and_audit(self):
        analysis = analyze()
        run_id = store.save_run("tester", "vmissing", analysis)
        run = store.get_run(run_id)
        self.assertEqual(run["summary"]["confirmed_pairs"],
                         analysis["summary"]["confirmed_pairs"])
        actions = [entry["action"] for entry in store.list_audit()]
        self.assertIn("run.save", actions)
        self.assertIn("init", actions)

    def test_delete_version_cascades(self):
        version = store.create_version("tester", "temp", {}, {}, {}, {})
        store.save_version_pairs(version["id"], [{"a": 1, "b": 2, "key": "k"}])
        self.assertTrue(store.delete_version(version["id"], "tester"))
        self.assertIsNone(store.get_version(version["id"]))
        self.assertEqual(store.get_version_pairs(version["id"]), [])


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp()) / "http.sqlite3"
        store.configure(cls.tmp)
        store.init_db()
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def request(self, path, payload=None, method=None):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        request = urllib.request.Request(self.base + path, data=data,
                                         headers=headers, method=method)
        return urllib.request.urlopen(request)

    def test_meta_and_index(self):
        meta = json.load(self.request("/api/meta"))
        self.assertEqual(len(meta["rules"]), 9)
        self.assertTrue(meta["defaults"])
        page = self.request("/").read().decode()
        self.assertIn("商品编码关联规则台", page)

    def test_static_traversal_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/static/..%2Fserver.py")
        self.assertEqual(caught.exception.code, 404)

    def test_analysis_rejects_bad_input(self):
        payload = {"sides": {}, "rules": {}, "key_columns": {"A": 0, "B": 0}}
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/analyze", payload)
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("error", json.load(caught.exception))

    def test_full_version_flow(self):
        a = json.load(self.request("/api/sample/load", {"side": "A"}))
        b = json.load(self.request("/api/sample/load", {"side": "B"}))
        payload = {
            "sides": {"A": a["side_data"], "B": b["side_data"]},
            "rules": CLEANING,
            "key_columns": {"A": 0, "B": 0},
            "secondary_fields": ["size"],
            "decisions": {},
        }
        analysis = json.load(self.request("/api/analyze", payload))["analysis"]
        self.assertGreater(analysis["summary"]["rows_a"], 0)
        version = json.load(self.request(
            "/api/versions", {**payload, "label": "http-v1"}))["version"]
        revision = json.load(self.request("/api/analyze",
                                          {**payload,
                                           "baseline_version_id": version["id"]}))
        self.assertIn("diff", revision["analysis"])
        run = json.load(self.request("/api/runs", payload))
        self.assertTrue(run["run_id"])
        listing = json.load(self.request("/api/versions"))["versions"]
        self.assertTrue(any(item["label"] == "http-v1" for item in listing))
        export = self.request("/api/export/csv", {**payload,
                                                  "version_id": version["id"]})
        self.assertIn("attachment", export.headers["Content-Disposition"])
        self.assertIn("rules_fingerprint", export.read().decode())

    def test_csrf_style_guard_rejects_other_content_types(self):
        request = urllib.request.Request(
            self.base + "/api/analyze", data=b"{}",
            headers={"Content-Type": "text/plain"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
