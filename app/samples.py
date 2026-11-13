"""Bundled local sample files used by the colour-coded demo.

Two artisanal exports of the same product catalogue are provided in the
encodings they would realistically arrive in:

* A — Chinese export, GB18030, full-width codes, ideographic blanks,
  invisible zero-width characters, leading zeros, combining accents.
* B — Turkish export, Windows-1254 (cp1254), dotted/dotless I, ş/ğ,
  NBSP trailing blanks, English names.

Every row is a deliberately observed edge case; nothing here is random.
"""

from __future__ import annotations

import csv
import io

COLUMNS = ["code", "name", "brand_code", "size", "color_code"]

SECONDARY_FIELDS = [
    {"key": "brand_code", "label": "品牌代码"},
    {"key": "size", "label": "容量 / 规格"},
    {"key": "color_code", "label": "颜色代码"},
]

COLUMN_LABELS = {
    "code": "关联编码",
    "name": "商品名称",
    "brand_code": "品牌代码",
    "size": "容量 / 规格",
    "color_code": "颜色代码",
}

HAIRY = "​"  # zero width space
NBSP = " "
IDEOGRAPHIC = "　"

A_ROWS = [
    ["ＡＢＣ－００１", "蓝色保温杯", "LK001", "500ML", "BLUE"],
    ["ABC-001", "蓝色保温杯 电商装", "LK001", "500ML", "BLUE"],
    ["00123", "不锈钢餐具套装", "ZW001", "ONE_SIZE", "SILVER"],
    ["123", "餐具套装（旧编码）", "ZW001", "ONE_SIZE", "SILVER"],
    ["CAFÉ-01", "咖啡杯 陶瓷", "KTS01", "300ML", "WHITE"],
    ["SKU 007", "随行水壶", "ZO001", "600ML", "BLACK"],
    ["ABC-002", "保温杯 红", "LK001", "500ML", "RED"],
    ["ABC-002", "保温杯 红（重复导入）", "LK001", "750ML", "RED"],
    ["中文商品-01", "中文编码商品", "CN001", "STD", "RED"],
    ["IST-001", "伊斯坦布尔纪念杯", "TR001", "350ML", "BLUE"],
    ["SIP-001", "发货样品杯", "TR001", "350ML", "BLUE"],
    [HAIRY + "TAB-009", "蓝色托盘", "KC001", "40CM", "BLUE"],
    ["IST-002", "伊斯坦布尔杯二代", "TR001", "350ML", "RED"],
    ["ＡＢＣ－０２０", "保温杯 绿", "LK001", "750ML", "GREEN"],
    [IDEOGRAPHIC, "空白键（测试）", "TST01", "ONE_SIZE", "NONE"],
    [HAIRY, "不可见键（测试）", "TST01", "ONE_SIZE", "NONE"],
    ["Café-01", "咖啡杯（组合重音拼写）", "KTS01", "300ML", "WHITE"],
    ["ＣＡＦＥ－０１", "咖啡杯（全角拼写）", "KTS01", "300ML", "WHITE"],
    ["SKU-００７", "随行水壶（全角数字）", "ZO001", "600ML", "BLACK"],
]

B_ROWS = [
    ["abc-001", "Blue Thermos", "LK001", "500ML", "BLUE"],
    ["abc-001", "Blue Thermos — EU", "LK001", "750ML", "BLUE"],
    ["IST-001", "Istanbul Mug", "TR001", "350ML", "BLUE"],
    ["İST-001", "İstanbul Kupa", "TR001", "350ML", "MAVİ"],
    ["Café-01", "Coffee Mug Ceramic", "KTS01", "300ML", "WHITE"],
    ["ŞİP-001", "Şipariş Örneği", "TR001", "350ML", "BLUE"],
    ["abc-002", "Red Thermos", "LK001", "500ML", "RED"],
    ["abc-002", "Red Thermos — EU", "LK001", "750ML", "RED"],
    ["TAB-009", "Blue Tray", "KC001", "40CM", "BLUE"],
    ["TAB-009", "Blue Tray — EU", "KC001", "45CM", "BLUE"],
    ["ABC-001", "Blue Thermos — Legacy", "LK001", "500ML", "BLUE"],
    ["İST-002", "İstanbul Kupa v2", "TR001", "350ML", "RED"],
    ["SKU 007", "Travel Bottle", "ZO001", "600ML", "BLACK"],
    ["SKU 007" + NBSP, "Travel Bottle — NBSP", "ZO001", "600ML", "BLACK"],
    ["123", "Cutlery Set (old code)", "ZW001", "ONE_SIZE", "SILVER"],
    ["Café-01", "Coffee Mug Ceramic — EU", "KTS01", "300ML", "WHITE"],
]


def _rows_to_text(rows: list[list[str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows(rows)
    return buffer.getvalue()


A_TEXT = _rows_to_text(A_ROWS)
B_TEXT = _rows_to_text(B_ROWS)

SAMPLES = {
    "A": {
        "side": "A",
        "id": "A",
        "label": "中国商品导出（GB18030）",
        "short_label": "中国 / GB18030",
        "filename": "sample_products_cn.csv",
        "encoding": "gb18030",
        "encoding_choices": ["gb18030", "gbk", "utf-8", "cp1254", "big5"],
        "notes": "全角编码、全角空格、零宽空格、前导零、组合重音；含中文商品。",
        "text": A_TEXT,
    },
    "B": {
        "side": "B",
        "id": "B",
        "label": "土耳其商品导出（Windows-1254）",
        "short_label": "土耳其 / cp1254",
        "filename": "sample_products_tr.csv",
        "encoding": "cp1254",
        "encoding_choices": ["cp1254", "utf-8", "gb18030", "latin-1", "cp1250"],
        "notes": "土耳其语大小写（I/ı、İ/i）、ş/ğ、NBSP 尾随空白；英文名称。",
        "text": B_TEXT,
    },
}

ENCODING_CHOICES = [
    {"value": "utf-8", "label": "UTF-8"},
    {"value": "gb18030", "label": "GB18030（中国）"},
    {"value": "gbk", "label": "GBK（中国）"},
    {"value": "cp1254", "label": "Windows-1254（土耳其）"},
    {"value": "latin-1", "label": "ISO-8859-1"},
    {"value": "cp1250", "label": "Windows-1250（中欧）"},
    {"value": "big5", "label": "Big5（繁体中文）"},
]


def sample_text(side: str, encoding: str | None = None,
                errors: str = "replace") -> dict[str, object]:
    """Return the sample decoded with the *declared* (or requested) codec.

    A wrong encoding choice is a realistic mistake; decoding with
    ``errors='replace'`` makes the damage visible as U+FFFD instead of
    crashing, and the encoding rule can then skip those cells.
    """

    if side not in SAMPLES:
        raise KeyError(side)
    sample = SAMPLES[side]
    encoding = encoding or sample["encoding"]
    try:
        "".encode(encoding)  # codec lookup, raises LookupError early
    except LookupError as exc:
        raise ValueError(f"未知编码: {encoding}") from exc
    raw = str(sample["text"]).encode(sample["encoding"])
    text = raw.decode(encoding, errors=errors)
    replacements = text.count("�")
    return {
        "side": side,
        "filename": sample["filename"],
        "label": sample["label"],
        "declared_encoding": sample["encoding"],
        "read_encoding": encoding,
        "text": text,
        "replacement_chars": replacements,
        "bytes_len": len(raw),
    }


def sample_bytes(side: str) -> tuple[str, bytes]:
    if side not in SAMPLES:
        raise KeyError(side)
    sample = SAMPLES[side]
    return str(sample["filename"]), str(sample["text"]).encode(sample["encoding"])
