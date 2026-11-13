"""Deterministic key preparation for multilingual product data.

Every transformation is an explicit, individually selectable rule.  The
original cell value is never mutated; :func:`prepare_key` returns a derived
key together with the ordered list of steps that changed it.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Rule catalogue (also served to the UI)
# ---------------------------------------------------------------------------

RULE_META: list[dict[str, Any]] = [
    {
        "key": "encoding_invalid",
        "label": "编码错误单元格",
        "description": (
            "文件按所选编码解码后出现替换符 U+FFFD 时如何处理。"
            "保留会留下可疑键；跳过会排除该行并计数。"
        ),
        "default": "skip",
        "options": [
            {"value": "keep", "label": "保留（标记为编码可疑）"},
            {"value": "skip", "label": "跳过该行", "warning": "该行不参与关联"},
        ],
    },
    {
        "key": "invisible",
        "label": "不可见字符",
        "description": (
            "移除 Unicode Cf 类字符（零宽空格、BOM、软连字符、方向控制符等）"
            "以及 Hangul 填充符。它们肉眼不可见，但会让键无法相等。"
        ),
        "default": "keep",
        "options": [
            {"value": "keep", "label": "保留"},
            {"value": "strip", "label": "移除不可见字符"},
        ],
    },
    {
        "key": "whitespace",
        "label": "空白",
        "description": (
            "仅可选择一种空白策略。unicode 变体会把 NBSP（U+00A0）与"
            "全角空格（U+3000）视为空白。"
        ),
        "default": "keep",
        "options": [
            {"value": "keep", "label": "保留"},
            {"value": "trim", "label": "去首尾 ASCII 空白"},
            {"value": "trim_unicode", "label": "去首尾 Unicode 空白（含 NBSP/全角空格）"},
            {"value": "collapse", "label": "合并内部 ASCII 空白并去首尾"},
            {"value": "collapse_unicode", "label": "合并内部 Unicode 空白并去首尾"},
            {"value": "remove", "label": "删除全部 Unicode 空白", "warning": "可能把不同编号粘在一起"},
        ],
    },
    {
        "key": "width",
        "label": "全角 / 半角",
        "description": (
            "只把全角 ASCII（U+FF01–U+FF5E，含全角数字与连字符）映射为半角，"
            "并把全角空格 U+3000 转为普通空格。不会做其他兼容转换。"
        ),
        "default": "keep",
        "options": [
            {"value": "keep", "label": "保留"},
            {"value": "fullwidth_ascii", "label": "全角 ASCII → 半角"},
        ],
    },
    {
        "key": "normalization",
        "label": "Unicode 规范化",
        "description": (
            "NFC/NFKC 可把“基字符 + 组合重音”与预组合字符统一；NFKC 还会做"
            "兼容分解（上标、罗马数字、圈号等）。NFD/NFKD 则拆开组合字符。"
        ),
        "default": "none",
        "options": [
            {"value": "none", "label": "不规范"},
            {"value": "NFC", "label": "NFC（组合字符合成）"},
            {"value": "NFD", "label": "NFD（拆开组合字符）"},
            {"value": "NFKC", "label": "NFKC（兼容合成）", "warning": "兼容字符会被改写"},
            {"value": "NFKD", "label": "NFKD（兼容分解）", "warning": "兼容字符会被改写"},
        ],
    },
    {
        "key": "diacritics",
        "label": "重音 / 变音符号",
        "description": (
            "去掉组合附加符号（é→e、ş→s、İ→I）。重音在商品编码里"
            "可能是业务含义，默认保留。"
        ),
        "default": "keep",
        "options": [
            {"value": "keep", "label": "保留（推荐）"},
            {"value": "strip", "label": "移除组合符号", "warning": "é、ş、İ 等会失去区别"},
        ],
    },
    {
        "key": "case",
        "label": "大小写",
        "description": (
            "turkish_lower 按土耳其语规则把 I→ı、İ→i；casefold 可能把 ß 变成 ss。"
            "大小写对某些业务编码可能有意义。"
        ),
        "default": "none",
        "options": [
            {"value": "none", "label": "保留（推荐）"},
            {"value": "ascii_lower", "label": "仅 ASCII 小写"},
            {"value": "unicode_lower", "label": "Unicode 小写"},
            {"value": "casefold", "label": "Unicode casefold", "warning": "ß→ss，可能合并编码"},
            {"value": "turkish_lower", "label": "土耳其语小写（I→ı，İ→i）", "warning": "与其他语言的小写结果不同"},
        ],
    },
    {
        "key": "punctuation",
        "label": "标点 / 分隔符",
        "description": (
            "仅把各种破折号（U+2010–U+2015、减号、全角连字符）统一为 '-'，"
            "或删除全部标点。分隔符含义由业务决定。"
        ),
        "default": "keep",
        "options": [
            {"value": "keep", "label": "保留"},
            {"value": "dashes", "label": "破折号 → '-'"},
            {"value": "strip_all", "label": "删除全部标点", "warning": "可能改变编号结构"},
        ],
    },
    {
        "key": "leading_zeros",
        "label": "前导零",
        "description": (
            "货号中的前导零可能有业务含义（00123 ≠ 123）。strip_numeric 只处理"
            "整串为数字的键；strip_tokens 会处理 '-'、'/'、空格等分隔后的数字段。"
        ),
        "default": "keep",
        "options": [
            {"value": "keep", "label": "保留（推荐）"},
            {"value": "strip_numeric", "label": "纯数字键去前导零", "warning": "00123 与 123 会合并"},
            {"value": "strip_tokens", "label": "数字段去前导零（如 SKU-007）", "warning": "可能合并不同货号"},
        ],
    },
]

RULES_META = RULE_META

RULE_FIELDS = {meta["key"]: [o["value"] for o in meta["options"]] for meta in RULE_META}
DEFAULT_RULES = {meta["key"]: meta["default"] for meta in RULE_META}

DESTRUCTIVE_RULES = {
    meta["key"]: [o["value"] for o in meta["options"] if o.get("warning")]
    for meta in RULE_META
}

PIPELINE = [
    ("encoding_invalid", "编码校验"),
    ("invisible", "不可见字符"),
    ("whitespace", "空白"),
    ("width", "全角/半角"),
    ("normalization", "Unicode 规范化"),
    ("diacritics", "重音符号"),
    ("case", "大小写"),
    ("punctuation", "标点"),
    ("leading_zeros", "前导零"),
]

# Characters that render as nothing but are not always category Cf.
ZERO_WIDTH_EXTRAS = {0x115F, 0x1160, 0x3164, 0xFFA0, 0x180E}

DASH_MAP = {cp: "-" for cp in range(0x2010, 0x2016)}
DASH_MAP.update({0x2212: "-", 0xFE58: "-", 0xFE63: "-", 0xFF0D: "-", 0x02D7: "-"})

_ASCII_WS = " \t\r\n\f\v"


def validate_rules(raw: Any) -> dict[str, str]:
    """Return a complete, validated rules mapping or raise ValueError."""

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("rules 必须是对象")
    unknown = set(raw) - set(RULE_FIELDS)
    if unknown:
        raise ValueError(f"未知规则字段: {', '.join(sorted(unknown))}")
    rules = dict(DEFAULT_RULES)
    for key, value in raw.items():
        if not isinstance(value, str) or value not in RULE_FIELDS[key]:
            raise ValueError(f"规则 {key} 的取值无效: {value!r}")
        rules[key] = value
    return rules


def rules_fingerprint(rules: dict[str, str]) -> str:
    canonical = json.dumps(validate_rules(rules), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def active_destructive_rules(rules: dict[str, str]) -> list[dict[str, str]]:
    out = []
    for key, values in DESTRUCTIVE_RULES.items():
        if rules.get(key) in values:
            label = next(m for m in RULE_META if m["key"] == key)["label"]
            option = next(o for o in next(m for m in RULE_META if m["key"] == key)["options"]
                          if o["value"] == rules[key])
            out.append({"rule": key, "label": label, "value": rules[key],
                        "option_label": option["label"], "warning": option["warning"]})
    return out


# ---------------------------------------------------------------------------
# Individual transformations
# ---------------------------------------------------------------------------


def _is_invisible(ch: str) -> bool:
    return unicodedata.category(ch) == "Cf" or ord(ch) in ZERO_WIDTH_EXTRAS


def _strip_invisible(value: str) -> tuple[str, list[str]]:
    removed = [ch for ch in value if _is_invisible(ch)]
    if not removed:
        return value, []
    return "".join(ch for ch in value if not _is_invisible(ch)), removed


def _map_whitespace(value: str, mode: str) -> str:
    if mode == "keep":
        return value
    if mode == "trim":
        return value.strip(_ASCII_WS)
    if mode == "trim_unicode":
        return value.strip()
    if mode == "collapse":
        lead = value.lstrip(_ASCII_WS)
        trail = lead.rstrip(_ASCII_WS)
        return re.sub(r"[ \t\r\n\f\v]+", " ", trail)
    if mode == "collapse_unicode":
        out = []
        pending_space = False
        for ch in value:
            if ch.isspace():
                pending_space = True
            else:
                if pending_space and out:
                    out.append(" ")
                pending_space = False
                out.append(ch)
        return "".join(out)
    if mode == "remove":
        return "".join(ch for ch in value if not ch.isspace())
    raise ValueError(mode)


def _fullwidth_to_halfwidth(value: str) -> str:
    out = []
    for ch in value:
        cp = ord(ch)
        if 0xFF01 <= cp <= 0xFF5E:
            out.append(chr(cp - 0xFEE0))
        elif cp == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


_NUMERIC_RE = re.compile(r"[0-9]+")
_TOKEN_ZERO_RE = re.compile(r"(?<![0-9A-Za-z])([0-9]+)")


def _leading_zeros(value: str, mode: str) -> str:
    if mode == "keep":
        return value
    if mode == "strip_numeric":
        if _NUMERIC_RE.fullmatch(value):
            return value.lstrip("0") or "0"
        return value
    if mode == "strip_tokens":
        def fix(match: re.Match[str]) -> str:
            token = match.group(1)
            return token.lstrip("0") or "0"

        return _TOKEN_ZERO_RE.sub(fix, value)
    raise ValueError(mode)


def _turkish_lower(value: str) -> str:
    # Uppercase dotted I lowercases to i; uppercase ASCII I to dotless ı.
    return (value.replace("İ", "i")
                 .replace("İ", "i")
                 .replace("I", "ı")
                 .lower())


def _strip_diacritics(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", stripped)


def _strip_punctuation(value: str) -> str:
    return "".join(ch for ch in value
                   if not unicodedata.category(ch).startswith("P"))


def codepoint_entry(ch: str) -> dict[str, str]:
    cp = ord(ch)
    try:
        name = unicodedata.name(ch)
    except ValueError:
        name = "<未命名>"
    return {
        "char": ch,
        "codepoint": f"U+{cp:04X}",
        "name": name,
        "category": unicodedata.category(ch),
        "visible_placeholder": (
            "␠" if ch == " " else
            "⍽" if ch.isspace() else
            "·" if unicodedata.category(ch).startswith("M") else
            "∅" if _is_invisible(ch) else ch
        ),
    }


def describe_chars(value: str) -> list[dict[str, str]]:
    return [codepoint_entry(ch) for ch in value]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def prepare_key(raw: str, rules: dict[str, str]) -> dict[str, Any]:
    """Apply the selected rules to one raw key, recording every change."""

    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raw = str(raw)
    rules = validate_rules(rules)

    steps: list[dict[str, Any]] = []
    flags: list[str] = []
    warnings: list[str] = []

    def record(rule: str, before: str, after: str, detail: str) -> None:
        nonlocal value
        if before != after:
            steps.append({"rule": rule, "before": before, "after": after,
                          "detail": detail})
            value = after

    value = raw

    # encoding sanity is evaluated by the caller, but flag the cell here.
    if "�" in raw:
        flags.append("encoding_invalid")
        if rules["encoding_invalid"] == "skip":
            return {
                "raw": raw,
                "clean": value,
                "steps": steps,
                "flags": flags,
                "warnings": ["该单元格包含 U+FFFD 替换符，规则要求跳过"],
                "blank": not value.strip(),
                "excluded": True,
                "exclude_reason": "encoding_invalid",
            }
        warnings.append("键包含 U+FFFD 替换符，请检查文件编码")

    # 1. invisible characters
    if rules["invisible"] == "strip":
        before = value
        after, removed = _strip_invisible(value)
        if removed:
            names = sorted({codepoint_entry(ch)["codepoint"] for ch in removed})
            record("invisible", before, after, f"移除 {len(removed)} 个不可见字符（{'、'.join(names)}）")

    # 2. whitespace
    if rules["whitespace"] != "keep":
        before = value
        after = _map_whitespace(value, rules["whitespace"])
        record("whitespace", before, after, f"空白规则: {rules['whitespace']}")

    # 3. width
    if rules["width"] == "fullwidth_ascii":
        before = value
        after = _fullwidth_to_halfwidth(value)
        record("width", before, after, "全角 ASCII 映射为半角")

    # 4. unicode normalization
    if rules["normalization"] != "none":
        before = value
        after = unicodedata.normalize(rules["normalization"], value)
        record("normalization", before, after, f"Unicode {rules['normalization']}")

    # 5. diacritics
    if rules["diacritics"] == "strip":
        before = value
        after = _strip_diacritics(value)
        record("diacritics", before, after, "移除组合重音符号")

    # 6. case
    if rules["case"] != "none":
        before = value
        if rules["case"] == "ascii_lower":
            after = value.translate(str.maketrans(
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
        elif rules["case"] == "unicode_lower":
            after = value.lower()
        elif rules["case"] == "casefold":
            after = value.casefold()
        elif rules["case"] == "turkish_lower":
            after = _turkish_lower(value)
        else:  # validated already
            raise ValueError(rules["case"])
        record("case", before, after, f"大小写规则: {rules['case']}")

    # 7. punctuation
    if rules["punctuation"] != "keep":
        before = value
        if rules["punctuation"] == "dashes":
            after = value.translate(DASH_MAP)
        elif rules["punctuation"] == "strip_all":
            after = _strip_punctuation(value)
        else:
            raise ValueError(rules["punctuation"])
        record("punctuation", before, after, f"标点规则: {rules['punctuation']}")

    # 8. leading zeros
    if rules["leading_zeros"] != "keep":
        before = value
        after = _leading_zeros(value, rules["leading_zeros"])
        record("leading_zeros", before, after, f"前导零规则: {rules['leading_zeros']}")

    blank = value.strip() == ""
    if blank:
        flags.append("blank")

    return {
        "raw": raw,
        "clean": value,
        "steps": steps,
        "flags": flags,
        "warnings": warnings,
        "blank": blank,
        "excluded": blank,
        "exclude_reason": "blank" if blank else None,
    }


def prepare_many(values: Iterable[str], rules: dict[str, str]) -> list[dict[str, Any]]:
    return [prepare_key(v, rules) for v in values]


def rule_impacts(prepared: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {key: 0 for key, _ in PIPELINE}
    for item in prepared:
        seen = set()
        for step in item["steps"]:
            if step["rule"] not in seen:
                counts[step["rule"]] = counts.get(step["rule"], 0) + 1
                seen.add(step["rule"])
    return counts


# ---------------------------------------------------------------------------
# Probe strings shown in the live rule preview
# ---------------------------------------------------------------------------

PROBES: list[dict[str, str]] = [
    {"raw": "ＡＢＣ－００１", "note": "全角字母、全角连字符与全角数字"},
    {"raw": "ABC-001", "note": "同一编码的半角写法"},
    {"raw": "SKU 007 ", "note": "NBSP 尾随空白（普通 trim 去不掉）"},
    {"raw": "　ＴＡＢ－００９", "note": "全角空格开头 + 全角编码"},
    {"raw": "​TAB-009", "note": "零宽空格开头"},
    {"raw": "Café-01", "note": "组合重音（e + U+0301）"},
    {"raw": "Café-01", "note": "预组合重音（U+00E9）"},
    {"raw": "CAFÉ-01", "note": "大写重音"},
    {"raw": "IST-001", "note": "ASCII 大写 I"},
    {"raw": "İST-001", "note": "土耳其语带点大写 İ"},
    {"raw": "ıstanbul-01", "note": "土耳其语无点小写 ı"},
    {"raw": "ŞİP-001", "note": "土耳其语 Ş / İ"},
    {"raw": "00123", "note": "带前导零的货号"},
    {"raw": "123", "note": "去掉前导零后的写法"},
    {"raw": "SKU-007", "note": "分段编号中的前导零"},
    {"raw": "ａｂｃ　００７", "note": "全角 + 全角空格混合"},
    {"raw": "ß", "note": "casefold 后会变成 ss"},
    {"raw": "①", "note": "NFKC 兼容字符"},
]

