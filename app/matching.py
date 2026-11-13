"""Key matching, collision detection, duplicate previews and version diffs."""

from __future__ import annotations

import hashlib
import itertools
import json
from typing import Any, Iterable

from . import keys as K

MAX_CANDIDATE_PREVIEW = 60
MAX_DIFF_PAIRS = 2500

SIDES = ("A", "B")
DECISIONS = ("merge", "separate", "skip")


def _signature(side: dict[str, Any]) -> str:
    payload = {
        "filename": side.get("filename"),
        "encoding": side.get("encoding"),
        "columns": [c["name"] for c in side["columns"]],
        "rows": [[r["row_no"], r["cells"]] for r in side["rows"]],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _resolve_secondary(sides: dict[str, Any], fields: Iterable[str]) -> tuple[
        dict[str, dict[str, int]], list[str]]:
    resolved: dict[str, dict[str, int]] = {}
    unavailable: list[str] = []
    for field in fields:
        found: dict[str, int] = {}
        for side in SIDES:
            idx = next((c["index"] for c in sides[side]["columns"]
                        if c["name"] == field), None)
            if idx is None:
                break
            found[side] = idx
        if len(found) == 2:
            resolved[field] = found
        else:
            unavailable.append(field)
    return resolved, unavailable


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("row_no", "cells", "raw_key", "clean_key", "steps", "flags",
            "warnings", "excluded", "exclude_reason", "secondary", "status",
            "partners", "partner_count", "possible", "match_key",
            "match_key_kind", "collision_id", "duplicate_group", "reason")
    return {key: row.get(key) for key in keys if key in row}


def _collision_id(side: str, raws: list[str]) -> str:
    blob = json.dumps([side, sorted(raws)], ensure_ascii=False,
                      separators=(",", ":"))
    return side + "|" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _secondary_detail(row: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    return {field: row["secondary"][field]["clean"] for field in fields}


def _pair_detail(a: dict[str, Any], b: dict[str, Any],
                 fields: list[str]) -> dict[str, Any]:
    return {
        "a": a["row_no"],
        "b": b["row_no"],
        "secondary": {
            field: {
                "a": a["secondary"][field]["clean"],
                "b": b["secondary"][field]["clean"],
            }
            for field in fields
        },
    }


def analyze(sides: dict[str, Any], rules_raw: Any, config: dict[str, Any] | None,
            decisions: dict[str, Any] | None,
            baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    rules = K.validate_rules(rules_raw)
    fingerprint = K.rules_fingerprint(rules)
    if not sides.get("A") or not sides.get("B"):
        raise ValueError("请先载入两侧数据")
    config = config or {}
    decisions = decisions or {}
    if not isinstance(decisions, dict):
        raise ValueError("decisions 必须是对象")

    key_columns: dict[str, int] = {}
    for side in SIDES:
        raw_idx = config.get("key_columns", {}).get(side, 0)
        try:
            idx = int(raw_idx)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{side} 侧关联列无效") from exc
        if not 0 <= idx < len(sides[side]["columns"]):
            raise ValueError(f"{side} 侧关联列超出范围")
        key_columns[side] = idx

    raw_secondary = config.get("secondary_fields", []) or []
    if not isinstance(raw_secondary, list) or not all(
            isinstance(item, str) for item in raw_secondary):
        raise ValueError("secondary_fields 必须是字符串数组")
    secondary_fields = list(dict.fromkeys(raw_secondary))
    sec_idx, unavailable_secondary = _resolve_secondary(sides, secondary_fields)
    active_secondary = [f for f in secondary_fields if f in sec_idx]

    prepared: dict[str, list[dict[str, Any]]] = {}
    side_meta: dict[str, Any] = {}
    rule_impact: dict[str, dict[str, int]] = {}

    for side in SIDES:
        data = sides[side]
        idx = key_columns[side]
        rows: list[dict[str, Any]] = []
        for source_row in data["rows"]:
            cells = list(source_row["cells"])
            raw_key = cells[idx] if idx < len(cells) else ""
            prep = K.prepare_key(raw_key, rules)
            secondary: dict[str, Any] = {}
            for field in active_secondary:
                col = sec_idx[field][side]
                cell = cells[col] if col < len(cells) else ""
                prepared_cell = K.prepare_key(cell, rules)
                secondary[field] = {
                    "raw": cell,
                    "clean": prepared_cell["clean"],
                    # A cell the encoding rule skips cannot confirm a pairing.
                    "missing": (prepared_cell["clean"].strip() == ""
                                or prepared_cell["exclude_reason"] == "encoding_invalid"),
                }
            rows.append({
                "row_no": source_row["row_no"],
                "cells": cells,
                "raw_key": raw_key,
                "clean_key": prep["clean"],
                "steps": prep["steps"],
                "flags": prep["flags"],
                "warnings": prep["warnings"],
                "excluded": prep["excluded"],
                "exclude_reason": prep["exclude_reason"],
                "secondary": secondary,
                "status": "excluded" if prep["excluded"] else "unmatched",
                "partners": [],
                "partner_count": 0,
                "possible": 0,
                "match_key": prep["clean"],
                "match_key_kind": "clean",
            })
        prepared[side] = rows
        impact = K.rule_impacts(
            [{"steps": row["steps"]} for row in rows])
        # Secondary-field transformations are informative but reported per key
        # column only; rules are shared, so this mirrors the key impact.
        rule_impact[side] = impact
        side_meta[side] = {
            "side": side,
            "label": data.get("label", side),
            "filename": data.get("filename"),
            "encoding": data.get("encoding"),
            "columns": data["columns"],
            "key_column": {
                "index": idx,
                "name": data["columns"][idx]["name"],
            },
            "row_count": len(rows),
            "data_signature": _signature(data),
        }

    # ------------------------------------------------------------------
    # Collision groups: distinct raw spellings that collapse to one clean key
    # ------------------------------------------------------------------
    collision_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    collisions: list[dict[str, Any]] = []
    for side in SIDES:
        grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for row in prepared[side]:
            if row["excluded"]:
                continue
            grouped.setdefault(row["clean_key"], {}).setdefault(
                row["raw_key"], []).append(row)
        for clean, variants in grouped.items():
            if len(variants) < 2:
                continue
            # The id is derived from the raw spellings, not from the cleaned
            # key, so a decision survives rule tweaks that keep the same
            # spellings colliding.
            cid = _collision_id(side, list(variants))
            decision = decisions.get(cid, "pending")
            if decision not in DECISIONS:
                decision = "pending"
            group = {
                "id": cid,
                "side": side,
                "clean_key": clean,
                "decision": decision,
                "variants": [
                    {"raw": raw, "row_nos": [r["row_no"] for r in rows],
                     "row_count": len(rows)}
                    for raw, rows in variants.items()
                ],
                "row_nos": [r["row_no"] for rows in variants.values() for r in rows],
                "rows": [r for rows in variants.values() for r in rows],
                "counterpart_row_nos": [],
                "possible_matches": 0,
                "candidate_pairs": [],
                "candidates_truncated": False,
            }
            collisions.append(group)
            collision_lookup[(side, clean)] = group

    # ------------------------------------------------------------------
    # Decide how every non-excluded row participates
    # ------------------------------------------------------------------
    eff_groups: dict[str, dict[tuple[str, str], list[dict[str, Any]]]] = {
        side: {} for side in SIDES
    }
    for side in SIDES:
        for row in prepared[side]:
            if row["excluded"]:
                continue
            group = collision_lookup.get((side, row["clean_key"]))
            if group is None:
                row["_eff"] = ("clean", row["clean_key"])
                eff_groups[side].setdefault(row["_eff"], []).append(row)
                continue
            decision = group["decision"]
            row["collision_id"] = group["id"]
            if decision == "skip":
                row["excluded"] = True
                row["exclude_reason"] = "collision_skip"
                row["status"] = "excluded"
                continue
            if decision == "separate":
                row["_eff"] = ("raw", row["raw_key"])
                row["match_key_kind"] = "raw"
                row["match_key"] = row["raw_key"]
                eff_groups[side].setdefault(row["_eff"], []).append(row)
                continue
            if decision == "merge":
                row["_eff"] = ("clean", row["clean_key"])
                eff_groups[side].setdefault(row["_eff"], []).append(row)
                continue
            row["_eff"] = None

    # ------------------------------------------------------------------
    # Match effective keys
    # ------------------------------------------------------------------
    matches: list[dict[str, Any]] = []
    ambiguous_groups: list[dict[str, Any]] = []
    group_results: dict[tuple[str, str], dict[str, Any]] = {}
    all_eff = set(eff_groups["A"]) | set(eff_groups["B"])
    ordered_eff = sorted(all_eff, key=lambda eff: (eff[0] != "clean", eff[1]))

    def _candidate_preview(arows: list[dict[str, Any]],
                           brows: list[dict[str, Any]],
                           fields: list[str]) -> list[dict[str, Any]]:
        """Bounded preview: at most MAX_CANDIDATE_PREVIEW qualifying pairs."""

        if not fields:
            pairs = itertools.product(arows, brows)
            return [_pair_detail(a, b, fields)
                    for a, b in itertools.islice(pairs, MAX_CANDIDATE_PREVIEW)]
        out: list[dict[str, Any]] = []
        buckets: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for row in brows:
            sig = _secondary_signature(row, fields)
            if sig is not None:
                buckets.setdefault(sig, []).append(row)
        for a in arows:
            sig = _secondary_signature(a, fields)
            if sig is None:
                continue
            for b in buckets.get(sig, []):
                out.append(_pair_detail(a, b, fields))
                if len(out) >= MAX_CANDIDATE_PREVIEW:
                    return out
        return out

    def _secondary_signature(row: dict[str, Any],
                             fields: list[str]) -> tuple[str, ...] | None:
        """Cleaned secondary values, or None when any is missing.

        Both sides run the *same* rule pipeline, so equality of signatures
        is the same as the previous pairwise comparison, without materialising
        every combination.
        """

        signature = []
        for field in fields:
            value = row["secondary"].get(field)
            if value is None or value["missing"]:
                return None
            signature.append(value["clean"])
        return tuple(signature)

    for eff in ordered_eff:
        arows = eff_groups["A"].get(eff, [])
        brows = eff_groups["B"].get(eff, [])
        kind, label = eff
        if not brows:
            for row in arows:
                row["status"] = "unmatched"
                row["reason"] = "other_side_missing"
            continue
        if not arows:
            for row in brows:
                row["status"] = "unmatched"
                row["reason"] = "other_side_missing"
            continue

        # Products can be enormous (20k x 20k); never materialise them.
        product = len(arows) * len(brows)
        mode: str | None = None
        reason: str | None = None
        qualified_count: int | None = None
        qualified_pair: tuple[dict[str, Any], dict[str, Any]] | None = None

        if active_secondary:
            # Bucket by the cleaned secondary signature, then count compatible
            # combinations arithmetically.
            buckets_a: dict[tuple[str, ...], list[dict[str, Any]]] = {}
            buckets_b: dict[tuple[str, ...], list[dict[str, Any]]] = {}
            for row in arows:
                sig = _secondary_signature(row, active_secondary)
                if sig is not None:
                    buckets_a.setdefault(sig, []).append(row)
            for row in brows:
                sig = _secondary_signature(row, active_secondary)
                if sig is not None:
                    buckets_b.setdefault(sig, []).append(row)
            qualified_count = 0
            for sig, group_a in buckets_a.items():
                group_b = buckets_b.get(sig)
                if not group_b:
                    continue
                qualified_count += len(group_a) * len(group_b)
                if len(group_a) == 1 and len(group_b) == 1:
                    candidate = (group_a[0], group_b[0])
                    if qualified_pair is None:
                        qualified_pair = candidate
                    else:
                        qualified_pair = None  # more than one distinct pair
        if product == 1:
            mode = "unique"
            qualified_pair = (arows[0], brows[0])
        elif active_secondary:
            if qualified_count == 1 and qualified_pair is not None:
                mode = "secondary"
            elif qualified_count == 0:
                reason = "secondary_no_match"
            else:
                reason = "secondary_not_unique"
        else:
            reason = "duplicate_keys"

        if mode:
            a, b = qualified_pair  # type: ignore[misc]
            a["status"] = "matched_unique" if mode == "unique" \
                else "matched_secondary"
            b["status"] = a["status"]
            for row, partner in ((a, b), (b, a)):
                row["partners"] = [partner["row_no"]]
                row["partner_count"] = 1
                row["possible"] = 1
                row["match_key"] = label
                row["match_key_kind"] = kind
            matches.append({
                "a": a["row_no"],
                "b": b["row_no"],
                "key": label,
                "key_kind": kind,
                "mode": mode,
                "secondary": _secondary_detail(a, active_secondary),
                "secondary_b": _secondary_detail(b, active_secondary),
                "status": "matched_unique" if mode == "unique" else "matched_secondary",
            })
            group_results[eff] = {
                "key": label, "key_kind": kind, "a_count": len(arows),
                "b_count": len(brows), "possible": product,
                "secondary_matches": qualified_count,
                "status": "matched", "mode": mode,
                "chosen": {"a": a["row_no"], "b": b["row_no"]},
            }
            continue

        candidates = _candidate_preview(arows, brows, active_secondary)
        for row in arows + brows:
            row["status"] = "ambiguous"
            row["reason"] = reason
            row["match_key"] = label
            row["match_key_kind"] = kind
        for a in arows:
            a["partner_count"] = len(brows)
            a["possible"] = product
        for b in brows:
            b["partner_count"] = len(arows)
            b["possible"] = product
        group = {
            "key": label,
            "key_kind": kind,
            "a_row_nos": [r["row_no"] for r in arows],
            "b_row_nos": [r["row_no"] for r in brows],
            "a_count": len(arows),
            "b_count": len(brows),
            "possible": product,
            "secondary_fields": active_secondary,
            "secondary_matches": qualified_count,
            "reason": reason,
            "candidates": candidates,
            "candidates_truncated": (qualified_count if active_secondary
                                     else product) > MAX_CANDIDATE_PREVIEW,
        }
        ambiguous_groups.append(group)
        group_results[eff] = {
            "key": label, "key_kind": kind, "a_count": len(arows),
            "b_count": len(brows), "possible": product,
            "secondary_matches": qualified_count,
            "status": "ambiguous", "mode": None, "reason": reason,
        }

    # ------------------------------------------------------------------
    # Pending collision previews block automatic pairing
    # ------------------------------------------------------------------
    blocked_sets: list[tuple[set[int], set[int], str]] = []

    def blocked_reason(a: int, b: int) -> str | None:
        for a_set, b_set, clean in blocked_sets:
            if a in a_set and b in b_set:
                return clean
        return None

    for group in collisions:
        if group["decision"] != "pending":
            continue
        side = group["side"]
        other = "B" if side == "A" else "A"
        clean = group["clean_key"]
        other_rows = []
        for row in prepared[other]:
            if row["excluded"] or row["clean_key"] != clean:
                continue
            other_group = collision_lookup.get((other, clean))
            if other_group and other_group["decision"] == "separate":
                continue
            other_rows.append(row)
        group["counterpart_row_nos"] = [r["row_no"] for r in other_rows]
        group["possible_matches"] = len(group["rows"]) * len(other_rows)
        group["candidate_pairs"] = [
            {"a": a["row_no"], "b": b["row_no"]}
            for a, b in itertools.islice(
                itertools.product(group["rows"], other_rows),
                MAX_CANDIDATE_PREVIEW)
        ]
        group["candidates_truncated"] = \
            group["possible_matches"] > MAX_CANDIDATE_PREVIEW
        blocked_sets.append((
            {row["row_no"] for row in group["rows"]},
            {row["row_no"] for row in other_rows},
            clean,
        ))
        for row in group["rows"]:
            row["status"] = "blocked"
            row["reason"] = "collision_pending"
            row["partners"] = [r["row_no"] for r in other_rows]
            row["partner_count"] = len(other_rows)
            row["possible"] = len(group["rows"]) * len(other_rows)
            row["collision_id"] = group["id"]
        for row in other_rows:
            if row["status"] in ("matched_unique", "matched_secondary"):
                continue
            if row["status"] == "ambiguous":
                continue
            row["status"] = "blocked"
            row["reason"] = "counterpart_collision_pending"
            row["partners"] = [r["row_no"] for r in group["rows"]]
            row["partner_count"] = len(group["rows"])
            row["possible"] = group["possible_matches"]
            row.setdefault("collision_id", group["id"])

    # Collision cards for non-pending decisions get counterpart context too.
    for group in collisions:
        if group["decision"] == "pending":
            continue
        side = group["side"]
        other = "B" if side == "A" else "A"
        clean = group["clean_key"]
        group["counterpart_row_nos"] = [
            row["row_no"] for row in prepared[other]
            if not row["excluded"] and row["clean_key"] == clean
        ]
        group["possible_matches"] = (
            len(group["rows"]) * len(group["counterpart_row_nos"])
            if group["decision"] == "merge" else 0
        )

    # ------------------------------------------------------------------
    # Preview row -> group bookkeeping done; serialise rows
    # ------------------------------------------------------------------
    for side in SIDES:
        for row in prepared[side]:
            row.pop("_eff", None)

    def ref(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "row_no": row["row_no"],
            "raw_key": row["raw_key"],
            "clean_key": row["clean_key"],
            "status": row["status"],
            "possible": row["possible"],
            "partner_count": row["partner_count"],
            "reason": row.get("reason"),
            "exclude_reason": row.get("exclude_reason"),
        }

    unmatched = {
        side: [ref(row) for row in prepared[side]
               if row["status"] == "unmatched"]
        for side in SIDES
    }
    excluded = {
        side: [ref(row) for row in prepared[side] if row["excluded"]]
        for side in SIDES
    }

    ambiguous_by_key = {(group["key"], group["key_kind"]): group
                        for group in ambiguous_groups}
    duplicate_groups: list[dict[str, Any]] = []
    for eff, result in sorted(group_results.items(),
                              key=lambda item: (item[0][1], item[0][0])):
        if result["a_count"] <= 1 and result["b_count"] <= 1:
            continue
        detail = ambiguous_by_key.get((result["key"], result["key_kind"]), {})
        duplicate_groups.append({
            **result,
            "a_row_nos": detail.get("a_row_nos", []),
            "b_row_nos": detail.get("b_row_nos", []),
            "candidates": detail.get("candidates", []),
            "candidates_truncated": detail.get("candidates_truncated", False),
            "secondary_fields": active_secondary,
        })

    blocked_rows = [ref(row) for side in SIDES for row in prepared[side]
                    if row["status"] == "blocked"]

    ambiguous_pairs = sum(group["possible"] for group in ambiguous_groups)

    summary = {
        "rows_a": len(prepared["A"]),
        "rows_b": len(prepared["B"]),
        "excluded_a": sum(1 for row in prepared["A"] if row["excluded"]),
        "excluded_b": sum(1 for row in prepared["B"] if row["excluded"]),
        "matched_unique": sum(1 for m in matches if m["mode"] == "unique"),
        "matched_secondary": sum(1 for m in matches if m["mode"] == "secondary"),
        "confirmed_pairs": len(matches),
        "matched_rows_a": len({m["a"] for m in matches}),
        "matched_rows_b": len({m["b"] for m in matches}),
        "unmatched_a": sum(1 for row in prepared["A"]
                           if row["status"] == "unmatched"),
        "unmatched_b": sum(1 for row in prepared["B"]
                           if row["status"] == "unmatched"),
        "ambiguous_groups": len(ambiguous_groups),
        "ambiguous_pairs": ambiguous_pairs,
        "blocked_rows": len(blocked_rows),
        "blocked_pairs": sum(len(a_set) * len(b_set)
                              for a_set, b_set, _ in blocked_sets),
        "collisions": len(collisions),
        "collisions_pending": sum(1 for c in collisions
                                  if c["decision"] == "pending"),
        "collisions_merged": sum(1 for c in collisions
                                 if c["decision"] == "merge"),
        "collisions_separate": sum(1 for c in collisions
                                   if c["decision"] == "separate"),
        "collisions_skipped": sum(1 for c in collisions
                                  if c["decision"] == "skip"),
    }

    analysis = {
        "rules": rules,
        "rules_fingerprint": fingerprint,
        "destructive_rules": K.active_destructive_rules(rules),
        "config": {
            "key_columns": key_columns,
            "secondary_fields": secondary_fields,
            "active_secondary": active_secondary,
            "unavailable_secondary": unavailable_secondary,
        },
        "sides": {
            side: {
                **side_meta[side],
                "rows": [_public_row(row) for row in prepared[side]],
            }
            for side in SIDES
        },
        "rule_impact": rule_impact,
        "collisions": [
            {key: value for key, value in group.items() if key != "rows"}
            for group in collisions
        ],
        "duplicate_groups": duplicate_groups,
        "matches": matches,
        "ambiguous_groups": ambiguous_groups,
        "unmatched": unmatched,
        "excluded": excluded,
        "blocked_rows": blocked_rows,
        "summary": summary,
    }
    if baseline:
        analysis["diff"] = diff_analysis(analysis, baseline,
                                         blocked_sets, ambiguous_groups)
    return analysis


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------


def diff_analysis(analysis: dict[str, Any], version: dict[str, Any],
                  blocked_sets: list[tuple[set[int], set[int], str]],
                  ambiguous_groups: list[dict[str, Any]]) -> dict[str, Any]:
    baseline_pairs = {
        (int(pair["a"]), int(pair["b"])): pair
        for pair in version.get("pairs", [])
        if "a" in pair and "b" in pair
    }
    current = {(m["a"], m["b"]): m for m in analysis["matches"]}

    def conflict_key(pair: tuple[int, int]) -> str | None:
        for a_set, b_set, clean in blocked_sets:
            if pair[0] in a_set and pair[1] in b_set:
                return clean
        return None

    ambiguous_keys: dict[tuple[int, int], str] = {}
    for group in ambiguous_groups:
        for a in group["a_row_nos"]:
            for b in group["b_row_nos"]:
                ambiguous_keys.setdefault((a, b), group["key"])

    def conflict_candidates_get(pair: tuple[int, int]) -> str | None:
        return conflict_key(pair) or ambiguous_keys.get(pair)
    def is_conflict(pair: tuple[int, int]) -> bool:
        return pair not in current and conflict_candidates_get(pair) is not None

    added = [pair for pair in current if pair not in baseline_pairs]
    conflict = [pair for pair in baseline_pairs if is_conflict(pair)]
    removed = [pair for pair in baseline_pairs
               if pair not in current and not is_conflict(pair)]
    unchanged = [pair for pair in baseline_pairs if pair in current]

    def current_record(pair: tuple[int, int]) -> dict[str, Any]:
        match = current.get(pair)
        if match:
            return {
                "a": pair[0], "b": pair[1], "key": match["key"],
                "mode": match["mode"], "status": "matched",
            }
        return {
            "a": pair[0], "b": pair[1],
            "key": conflict_candidates_get(pair) or "",
            "mode": None, "status": "conflict",
        }

    def baseline_record(pair: tuple[int, int]) -> dict[str, Any]:
        record = baseline_pairs[pair]
        match = current.get(pair)
        return {
            "a": pair[0], "b": pair[1],
            "key": record.get("key") or (match["key"] if match else ""),
            "mode": record.get("mode") or (match["mode"] if match else None),
            "status": "removed",
        }

    data_signature = {
        side: analysis["sides"][side]["data_signature"] for side in SIDES
    }
    version_signature = version.get("data_signature") or {}
    data_changed = bool(version_signature) and any(
        version_signature.get(side) not in (None, data_signature[side])
        for side in SIDES
    )

    return {
        "baseline_id": version.get("id"),
        "baseline_label": version.get("label") or version.get("id"),
        "baseline_fingerprint": version.get("rules_fingerprint"),
        "baseline_created_at": version.get("created_at"),
        "data_changed": data_changed,
        "added": [current_record(pair) for pair in sorted(added)][:MAX_DIFF_PAIRS],
        "removed": [baseline_record(pair)
                    for pair in sorted(removed)][:MAX_DIFF_PAIRS],
        "conflict": [current_record(pair)
                     for pair in sorted(conflict)][:MAX_DIFF_PAIRS],
        "unchanged_count": len(unchanged),
        "added_count": len(added),
        "removed_count": len(removed),
        "conflict_count": len(conflict),
        "truncated": max(len(added), len(removed), len(conflict)) > MAX_DIFF_PAIRS,
    }
