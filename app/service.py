"""Input validation, orchestration and export for the key linker."""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import math
import re
from typing import Any

from . import keys as K
from . import matching
from . import samples as S
from . import store

MAX_ROWS = 20000
MAX_COLUMNS = 100
MAX_CELL = 2000
MAX_SECONDARY = 2


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _bad(message: str, status: int = 400) -> ApiError:
    return ApiError(message, status)


def _int_param(value: Any, name: str) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool):
        raise _bad(f"{name} 必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise _bad(f"{name} 必须是整数")
    if number < 0 or number > MAX_COLUMNS:
        raise _bad(f"{name} 超出范围")
    return number


def _filename(value: Any, fallback: str = "upload") -> str:
    name = str(value or fallback).strip() or fallback
    if len(name) > 160:
        raise _bad("文件名过长")
    if any(ord(ch) < 0x20 for ch in name):
        raise _bad("文件名包含控制字符")
    return name


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def decode_bytes(raw: bytes, encoding: str) -> tuple[str, int]:
    """Decode with the selected codec; wrong guesses become U+FFFD."""

    try:
        text = raw.decode(encoding, errors="replace")
    except LookupError:
        raise _bad(f"未知编码: {encoding}")
    return text, text.count("�")


def import_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Decode + parse one uploaded side.

    Body: {filename, encoding, content_base64} or {filename, encoding, text}
    The encoding rule is applied later; here we only surface damage.
    """

    encoding = str(payload.get("encoding") or "utf-8")
    filename = _filename(payload.get("filename"))
    b64 = payload.get("content_base64")
    if isinstance(b64, str) and b64:
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise _bad("文件内容不是有效的 base64")
        if len(raw) > 8 * 1024 * 1024:
            raise _bad("文件超过 8MB 上限", 413)
        text, replacements = decode_bytes(raw, encoding)
    else:
        text = payload.get("text")
        if not isinstance(text, str):
            raise _bad("缺少文件内容")
        text, replacements = text, text.count("�")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text:
        raise _bad("文件包含空字符")

    sheet = parse_table(text, filename, encoding)
    return {
        "side_data": sheet_to_side(filename, filename, encoding, sheet),
        "replacement_chars": replacements,
        "row_count": len(sheet.rows),
        "column_count": len(sheet.columns),
    }


class Sheet:
    def __init__(self, columns: list[dict[str, Any]], rows: list[dict[str, Any]]):
        self.columns = columns
        self.rows = rows


def parse_table(text: str, filename: str, encoding: str) -> Sheet:
    kind = "json" if filename.lower().endswith((".json", ".jsonl")) else "csv"
    if not text.strip():
        raise _bad("文件为空或只有空白")
    return _parse_json(text) if kind == "json" else _parse_csv(text)


def _parse_json(text: str) -> Sheet:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _bad(f"JSON 解析失败: {exc.msg}（第 {exc.lineno} 行）")
    if isinstance(payload, dict):
        for key in ("rows", "data", "items"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise _bad("JSON 需要对象数组，或含 rows 数组的对象")
    if not payload:
        raise _bad("JSON 没有数据行")
    if len(payload) > MAX_ROWS:
        raise _bad(f"行数超过 {MAX_ROWS} 上限")

    names: list[str] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise _bad("JSON 数组的每一项必须是对象")
        for key in item:
            if not isinstance(key, str) or not key.strip():
                raise _bad("JSON 字段名必须是非空字符串")
            if key not in seen:
                if len(names) >= MAX_COLUMNS:
                    raise _bad(f"列数超过 {MAX_COLUMNS} 上限")
                seen.add(key)
                names.append(key)
    if not names:
        raise _bad("JSON 对象没有任何字段")

    rows = []
    for row_no, item in enumerate(payload, start=1):
        cells = []
        for name in names:
            value = item.get(name, "")
            if value is None:
                value = ""
            elif isinstance(value, bool):
                value = "true" if value else "false"
            elif isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            else:
                value = str(value)
            if len(value) > MAX_CELL:
                raise _bad(f"第 {row_no} 行的 {name} 单元格过长")
            cells.append(value)
        rows.append({"row_no": row_no, "cells": cells})
    return Sheet([{"index": i, "name": n} for i, n in enumerate(names)], rows)


def _parse_csv(text: str) -> Sheet:
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise _bad("CSV 没有表头")
    if not header or all(not cell.strip() for cell in header):
        raise _bad("CSV 表头为空")
    if len(header) > MAX_COLUMNS:
        raise _bad(f"列数超过 {MAX_COLUMNS} 上限")
    names: list[str] = []
    for raw_name in header:
        base = raw_name.strip() or f"列{len(names) + 1}"
        name = base
        suffix = 2
        while name in names:
            name = f"{base} ({suffix})"
            suffix += 1
        names.append(name)
    rows = []
    try:
        for row_no, record in enumerate(reader, start=1):
            if row_no > MAX_ROWS:
                raise _bad(f"行数超过 {MAX_ROWS} 上限")
            cells = list(record[:len(names)])
            if len(cells) > MAX_COLUMNS:
                raise _bad("单元格数量异常")
            cells = [cell if len(cell) <= MAX_CELL else cell[:MAX_CELL]
                     for cell in cells]
            rows.append({"row_no": row_no,
                         "cells": cells + [""] * (len(names) - len(cells))})
    except csv.Error as exc:
        raise _bad(f"CSV 解析失败: {exc}")
    if not rows:
        raise _bad("CSV 没有数据行")
    return Sheet([{"index": i, "name": n} for i, n in enumerate(names)], rows)


def sheet_to_side(side: str, label: str, encoding: str, sheet: Sheet) -> dict[str, Any]:
    return {
        "side": side,
        "label": label,
        "filename": label,
        "encoding": encoding,
        "columns": sheet.columns,
        "rows": sheet.rows,
    }


# ---------------------------------------------------------------------------
# Side / config normalisation
# ---------------------------------------------------------------------------


def normalize_side(side: str, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise _bad(f"缺少 {side} 侧数据")
    columns = data.get("columns")
    rows = data.get("rows")
    if not isinstance(columns, list) or not columns:
        raise _bad(f"{side} 侧列定义无效")
    if len(columns) > MAX_COLUMNS:
        raise _bad(f"{side} 侧列数过多")
    names: list[str] = []
    for column in columns:
        if not isinstance(column, dict):
            raise _bad(f"{side} 侧列定义无效")
        name = column.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _bad(f"{side} 侧列名无效")
        name = name.strip()
        if name in names:
            raise _bad(f"{side} 侧列名重复: {name}")
        names.append(name)
    if not isinstance(rows, list) or not rows:
        raise _bad(f"{side} 侧没有数据行")
    if len(rows) > MAX_ROWS:
        raise _bad(f"{side} 侧行数超过 {MAX_ROWS} 上限")
    out_rows = []
    seen_rows: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise _bad(f"{side} 侧数据行无效")
        if isinstance(row.get("row_no"), bool):
            raise _bad(f"{side} 侧行号无效")
        try:
            row_no = int(row.get("row_no"))
        except (TypeError, ValueError):
            raise _bad(f"{side} 侧行号无效")
        if not 1 <= row_no <= MAX_ROWS or row_no in seen_rows:
            raise _bad(f"{side} 侧行号重复或越界: {row_no}")
        seen_rows.add(row_no)
        cells = row.get("cells")
        if not isinstance(cells, list) or len(cells) > len(names):
            raise _bad(f"{side} 侧第 {row_no} 行单元格无效")
        values: list[str] = []
        for cell in cells:
            if cell is None:
                values.append("")
            elif isinstance(cell, bool):
                values.append("true" if cell else "false")
            elif isinstance(cell, (int, float)):
                if isinstance(cell, float) and not math.isfinite(cell):
                    raise _bad(f"{side} 侧第 {row_no} 行包含非有限数字")
                values.append(str(cell))
            elif isinstance(cell, str):
                if len(cell) > MAX_CELL:
                    raise _bad(f"{side} 侧第 {row_no} 行单元格过长")
                values.append(cell)
            else:
                raise _bad(f"{side} 侧单元格必须是文本")
        out_rows.append({"row_no": row_no,
                         "cells": values + [""] * (len(names) - len(values))})
    label = str(data.get("label") or side).strip()[:160] or side
    return {
        "side": side,
        "label": label,
        "filename": _filename(data.get("filename"), label),
        "encoding": str(data.get("encoding") or "utf-8")[:40],
        "columns": [{"index": i, "name": n} for i, n in enumerate(names)],
        "rows": out_rows,
    }


def normalize_sides(payload: dict[str, Any]) -> dict[str, Any]:
    sides = payload.get("sides")
    if not isinstance(sides, dict):
        raise _bad("缺少 sides")
    return {side: normalize_side(side, sides.get(side)) for side in matching.SIDES}


def normalize_config(payload: dict[str, Any]) -> dict[str, Any]:
    raw_keys = payload.get("key_columns")
    if raw_keys is None:
        raw_keys = {}
    if not isinstance(raw_keys, dict):
        raise _bad("key_columns 必须是对象")
    key_columns = {
        side: _int_param(raw_keys.get(side, 0), f"{side} 侧关联列")
        for side in matching.SIDES
    }
    secondary = payload.get("secondary_fields") or []
    if isinstance(secondary, str):
        secondary = [secondary] if secondary else []
    if not isinstance(secondary, list):
        raise _bad("secondary_fields 必须是数组")
    clean: list[str] = []
    for item in secondary:
        if not isinstance(item, str) or not item.strip():
            raise _bad("次级字段名无效")
        item = item.strip()
        if len(item) > 80:
            raise _bad("次级字段名过长")
        if item in clean:
            raise _bad(f"次级字段重复: {item}")
        clean.append(item)
    if len(clean) > MAX_SECONDARY:
        raise _bad(f"最多选择 {MAX_SECONDARY} 个次级字段")
    return {"key_columns": key_columns, "secondary_fields": clean}


def normalize_decisions(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get("decisions")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _bad("decisions 必须是对象")
    out: dict[str, str] = {}
    for cid, decision in raw.items():
        if not isinstance(cid, str) or not cid or len(cid) > 400:
            raise _bad("碰撞组 id 无效")
        if decision not in matching.DECISIONS:
            raise _bad(f"碰撞处理方式无效: {decision!r}")
        out[cid] = decision
    return out


def run_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    sides = normalize_sides(payload)
    config = normalize_config(payload)
    decisions = normalize_decisions(payload)
    rules = K.validate_rules(payload.get("rules"))
    return matching.analyze(sides, rules, config, decisions)


def preview_rules(payload: dict[str, Any]) -> dict[str, Any]:
    """Rule playground: shows raw vs cleaned probe strings, no file needed."""

    rules = K.validate_rules(payload.get("rules"))
    custom = payload.get("samples") or []
    if not isinstance(custom, list):
        raise _bad("samples 必须是字符串数组")
    probes = list(K.PROBES)
    for index, value in enumerate(custom[:20]):
        if not isinstance(value, str) or len(value) > 200:
            raise _bad("自定义样本必须是 200 字以内的字符串")
        probes.append({"raw": value, "note": "自定义"})
    out = []
    grouped: dict[str, list[str]] = {}
    for probe in probes:
        prepared = K.prepare_key(probe["raw"], rules)
        entry = {
            "raw": probe["raw"],
            "note": probe["note"],
            "clean": prepared["clean"],
            "steps": prepared["steps"],
            "changed": bool(prepared["steps"]),
            "flags": prepared["flags"],
            "codepoints": [c["codepoint"] for c in K.describe_chars(probe["raw"])],
            "collides_with": [],
        }
        grouped.setdefault(prepared["clean"], []).append(probe["raw"])
        out.append(entry)
    for entry in out:
        clean = entry["clean"]
        entry["collides_with"] = [
            raw for raw in grouped[clean] if raw != entry["raw"]
        ]
    collisions = [
        {"clean": clean, "raws": raws}
        for clean, raws in grouped.items() if len(raws) > 1
    ]
    impacts: dict[str, int] = {rule: 0 for rule, _ in K.PIPELINE}
    for entry in out:
        seen = set()
        for step in entry["steps"]:
            if step["rule"] not in seen:
                impacts[step["rule"]] = impacts.get(step["rule"], 0) + 1
                seen.add(step["rule"])
    return {"rules": rules, "probes": out, "collisions": collisions,
            "probe_impact": impacts,
            "rules_fingerprint": K.rules_fingerprint(rules),
            "destructive_rules": K.active_destructive_rules(rules),
            "pipeline": [{"rule": rule, "label": label} for rule, label in K.PIPELINE]}


# ---------------------------------------------------------------------------
# Versions and runs
# ---------------------------------------------------------------------------


def save_version(payload: dict[str, Any], actor: str) -> dict[str, Any]:
    analysis = run_analysis(payload)
    label = str(payload.get("label") or "").strip()[:120]
    note = payload.get("note")
    if note is not None:
        note = str(note).strip()[:500]
    signatures = {side: analysis["sides"][side]["data_signature"]
                  for side in matching.SIDES}
    version = store.create_version(
        actor=actor, label=label or "未命名版本", rules=analysis["rules"],
        config=analysis["config"], decisions=normalize_decisions(payload),
        side_signatures=signatures, note=note,
        fingerprint=analysis["rules_fingerprint"])
    pairs = [{"a": m["a"], "b": m["b"], "key": m["key"], "mode": m["mode"]}
             for m in analysis["matches"]]
    version["pairs"] = pairs
    store.save_version_pairs(version["id"], pairs)
    return version


def get_version(version_id: str) -> dict[str, Any]:
    version = store.get_version(version_id)
    if not version:
        raise _bad("版本不存在", 404)
    version["pairs"] = store.get_version_pairs(version_id)
    return version


def analyze_with_baseline(payload: dict[str, Any]) -> dict[str, Any]:
    sides = normalize_sides(payload)
    config = normalize_config(payload)
    decisions = normalize_decisions(payload)
    rules = K.validate_rules(payload.get("rules"))
    baseline = None
    baseline_id = payload.get("baseline_version_id")
    if baseline_id:
        if not isinstance(baseline_id, str) or len(baseline_id) > 80:
            raise _bad("baseline_version_id 无效")
        baseline = get_version(baseline_id)
    analysis = matching.analyze(sides, rules, config, decisions, baseline)
    if baseline_id:
        analysis["baseline_label"] = baseline.get("label") if baseline else None
        analysis["baseline_id"] = baseline_id
    return analysis


def save_run(payload: dict[str, Any], actor: str) -> dict[str, Any]:
    analysis = run_analysis(payload)
    version_id = payload.get("version_id")
    if version_id:
        get_version(str(version_id))
    else:
        version_id = save_version(payload, actor)["id"]
    run_id = store.save_run(actor, str(version_id), analysis)
    return {"run_id": run_id, "version_id": version_id,
            "summary": analysis["summary"]}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _safe_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@"):
        return "'" + text
    return text


def _write_csv(header: list[str], rows: list[list[Any]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_safe_cell(cell) for cell in row])
    return buffer.getvalue()


EXPORT_HEADER = [
    "result", "match_key", "key_kind", "possibly_matched", "reason",
    "a_row", "a_raw_key", "a_clean_key", "a_name", "a_rule_steps",
    "b_row", "b_raw_key", "b_clean_key", "b_name", "b_rule_steps",
    "secondary_fields", "secondary_a", "secondary_b",
    "version_label", "version_id", "rules_fingerprint", "rules_json",
]


def build_export(analysis: dict[str, Any], version: dict[str, Any] | None = None
                 ) -> tuple[str, str]:
    """One CSV row per pair / candidate row, with full traceability columns."""

    sides = analysis["sides"]
    secondary = analysis["config"]["active_secondary"]
    fingerprint = analysis["rules_fingerprint"]
    version = version or {}
    rules_json = json.dumps(analysis["rules"], ensure_ascii=False, sort_keys=True)
    version_label = version.get("label") or ""
    version_id = version.get("id") or ""

    cache: dict[tuple[str, int], dict[str, Any]] = {}

    def row_of(side: str, row_no: int) -> dict[str, Any] | None:
        key = (side, row_no)
        if key not in cache:
            data = next((r for r in sides[side]["rows"]
                         if r["row_no"] == row_no), None)
            if data is not None:
                cache[key] = data
        return cache.get(key)

    def name_of(side: str, row_no: int) -> str:
        data = row_of(side, row_no)
        if not data:
            return ""
        columns = sides[side]["columns"]
        index = next((c["index"] for c in columns if c["name"] == "name"), None)
        if index is None and len(columns) > 1:
            index = 1
        if index is None:
            return ""
        return data["cells"][index] if index < len(data["cells"]) else ""

    def steps_text(side: str, row_no: int) -> str:
        data = row_of(side, row_no)
        if not data:
            return ""
        return json.dumps(
            [{"rule": s["rule"], "before": s["before"], "after": s["after"]}
             for s in data.get("steps", [])],
            ensure_ascii=False)

    def secondary_of(side: str, row_no: int) -> str:
        data = row_of(side, row_no)
        if not data:
            return ""
        values = {}
        for field in secondary:
            item = (data.get("secondary") or {}).get(field)
            values[field] = item.get("clean") if isinstance(item, dict) else ""
        return json.dumps(values, ensure_ascii=False) if values else ""

    def base(result: str, key: str, kind: str, possible: Any, reason: str,
             a: int | None, b: int | None) -> list[Any]:
        a_data = row_of("A", a) if a else None
        b_data = row_of("B", b) if b else None
        return [
            result, key, kind, possible, reason,
            a if a else "", a_data["raw_key"] if a_data else "",
            a_data["clean_key"] if a_data else "",
            name_of("A", a) if a else "", steps_text("A", a) if a else "",
            b if b else "", b_data["raw_key"] if b_data else "",
            b_data["clean_key"] if b_data else "",
            name_of("B", b) if b else "", steps_text("B", b) if b else "",
            ",".join(secondary),
            secondary_of("A", a) if a else "",
            secondary_of("B", b) if b else "",
            version_label, version_id, fingerprint, rules_json,
        ]

    rows: list[list[Any]] = []
    for match in analysis["matches"]:
        rows.append(base("matched", match["key"], match["key_kind"], 1,
                         match["mode"], match["a"], match["b"]))
    for group in analysis["ambiguous_groups"]:
        for pair in group["candidates"]:
            rows.append(base("ambiguous", group["key"], group["key_kind"],
                             group["possible"], group["reason"],
                             pair["a"], pair["b"]))
        if not group["candidates"]:
            rows.append(base("ambiguous", group["key"], group["key_kind"],
                             group["possible"], group["reason"], None, None))
    for group in analysis["collisions"]:
        for pair in group["candidate_pairs"]:
            if group["side"] == "A":
                rows.append(base("collision_pending", group["clean_key"], "clean",
                                 group["possible_matches"], "collision_pending",
                                 pair["a"], pair["b"]))
            else:
                rows.append(base("collision_pending", group["clean_key"], "clean",
                                 group["possible_matches"], "collision_pending",
                                 pair["b"], pair["a"]))
    for side in matching.SIDES:
        other = "B" if side == "A" else "A"
        for item in analysis["unmatched"][side]:
            rows.append(base("unmatched", item["clean_key"], "clean", 0,
                             item.get("reason") or "no_counterpart",
                             item["row_no"] if side == "A" else None,
                             item["row_no"] if side == "B" else None))
        for item in analysis["excluded"][side]:
            rows.append(base("excluded", item["clean_key"], "clean", 0,
                             item.get("exclude_reason") or "excluded",
                             item["row_no"] if side == "A" else None,
                             item["row_no"] if side == "B" else None))
    # Rows blocked by a pending collision already appear as collision_pending
    # pairs above; blocked_rows is kept for the API/UI only.

    filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", version_label or "linkage")[:80]
    return f"{(filename or 'linkage').strip('_')}.csv", _write_csv(
        EXPORT_HEADER, rows)


def export_rules_json(analysis: dict[str, Any], version: dict[str, Any] | None = None
                      ) -> tuple[str, str]:
    version = version or {}
    payload = {
        "version_id": version.get("id"),
        "version_label": version.get("label"),
        "created_at": version.get("created_at"),
        "rules": analysis["rules"],
        "rules_fingerprint": analysis["rules_fingerprint"],
        "config": analysis["config"],
        "destructive_rules": analysis["destructive_rules"],
        "summary": analysis["summary"],
    }
    return "linkage_rules.json", json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Metadata for the page
# ---------------------------------------------------------------------------


def meta() -> dict[str, Any]:
    return {
        "rules": K.RULE_META,
        "defaults": K.DEFAULT_RULES,
        "pipeline": [{"rule": rule, "label": label} for rule, label in K.PIPELINE],
        "encodings": S.ENCODING_CHOICES,
        "samples": [
            {key: value for key, value in sample.items() if key != "text"}
            for sample in S.SAMPLES.values()
        ],
        "secondary_candidates": S.SECONDARY_FIELDS,
        "column_labels": S.COLUMN_LABELS,
        "decisions": [
            {"value": "pending", "label": "待确认（阻断自动关联）"},
            {"value": "merge", "label": "视为同一键（合并）"},
            {"value": "separate", "label": "按原始键区分"},
            {"value": "skip", "label": "排除这些行"},
        ],
        "max_secondary": 2,
        "version": "1.0.0",
    }
