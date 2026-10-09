"""Report tables as downloadable files: CSV, TSV, JSON and Excel (standard library only).

The dashboard's "Export" on a report asks the API for the same report with format=csv|tsv|json|xlsx: every row
that matches (not only the page on screen), with the same filters, sort, comparison and goal. Durations are
exported in seconds and rates as fractions (0.25 = 25 %, shown as a percentage in Excel). CSV and TSV start
with a byte-order mark so that spreadsheet programs read accents right; JSON and Excel files also describe
what they contain (property, period, comparison, filters, traffic).
"""

from __future__ import annotations

import io
import json
import math
import re
import time
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .query import DIM_LABELS, METRIC_LABELS

FORMATS = {"csv": "text/csv", "tsv": "text/tab-separated-values", "json": "application/json",
           "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
DURATIONS = {"avg_duration": "Visit duration (s)", "avg_engaged": "Engaged time (s)", "avg_time": "Time on page (s)"}
RATES = {"bounce_rate", "exit_rate", "conversion_rate", "change"}
OP_LABELS = {"is": "is", "is_not": "is not", "contains": "contains", "not_contains": "does not contain",
             "starts_with": "starts with"}
SERIES_METRICS = ("visitors", "sessions", "pageviews", "views_per_session", "bounce_rate", "avg_duration",
                  "avg_engaged", "conversions", "conversion_rate")


class Column:
    def __init__(self, key: str, label: str, kind: str = "text") -> None:
        self.key, self.label, self.kind = key, label, kind  # kind: text | number | rate


def _metric_column(key: str, suffix: str = "") -> Column:
    if key in DURATIONS:
        return Column(key, DURATIONS[key] + suffix, "number")
    return Column(key, METRIC_LABELS.get(key, key) + suffix, "rate" if key in RATES else "number")


def _value(key: str, v: Any) -> Any:
    if v is None:
        return None
    if key in DURATIONS:
        return round(v / 1000.0, 1)
    return v


def _change(cur: Any, prev: Any) -> Optional[float]:
    if cur is None or not prev:
        return None
    return round((cur - prev) / prev, 4)


def breakdown_table(data: Dict[str, Any]) -> Tuple[List[Column], List[List[Any]]]:
    dim, dim2 = data["dim"], data.get("dim2")
    metrics = data.get("metrics") or []
    comparing = "compare" in data
    cols = [Column("value", DIM_LABELS.get(dim, dim) + (" (ISO code)" if dim == "country" else ""))]
    if dim2:
        cols.append(Column("value2", DIM_LABELS.get(dim2, dim2) + (" (ISO code)" if dim2 == "country" else "")))
    for m in metrics:
        cols.append(_metric_column(m))
        if comparing:
            cols.append(_metric_column(m, " · compared period"))
            cols.append(Column(m + ".change", METRIC_LABELS.get(m, m) + " · change", "rate"))
    rows = []
    for r in data.get("rows") or []:
        row: List[Any] = [r.get("value")] + ([r.get("value2")] if dim2 else [])
        prev = r.get("cmp") or {}
        for m in metrics:
            row.append(_value(m, r.get(m)))
            if comparing:
                row.append(_value(m, prev.get(m)))
                row.append(_change(r.get(m), prev.get(m)))
        rows.append(row)
    return cols, rows


def series_table(data: Dict[str, Any]) -> Tuple[List[Column], List[List[Any]]]:
    series = data.get("series") or []
    keys = [k for k in SERIES_METRICS if series and k in series[0]]
    interval = data.get("interval", "day")
    cols = [Column("t", {"hour": "Hour", "day": "Day", "week": "Week starting", "month": "Month"}.get(interval, "Period"))]
    cols += [_metric_column(k) for k in keys]
    compare = (data.get("compare") or {}).get("series")
    if compare:
        cols.append(Column("cmp.t", cols[0].label + " · compared period"))
        cols += [_metric_column(k, " · compared period") for k in keys]
    rows = []
    for i, p in enumerate(series):
        row = [p.get("t")] + [_value(k, p.get(k)) for k in keys]
        if compare:
            c = compare[i] if i < len(compare) else {}
            row += [c.get("t")] + [_value(k, c.get(k)) for k in keys]
        rows.append(row)
    return cols, rows


def goals_table(data: Dict[str, Any]) -> Tuple[List[Column], List[List[Any]]]:
    comparing = "compare" in data
    cols = [Column("name", "Goal"), Column("type", "Type"), Column("match", "Matches")]
    keys = ("conversions", "visitors", "conversion_rate", "value")
    labels = {"visitors": "Converted visitors", "value": "Value"}
    for k in keys:
        cols.append(Column(k, labels.get(k) or METRIC_LABELS.get(k, k), "rate" if k in RATES else "number"))
        if comparing:
            cols.append(Column(k + ".cmp", (labels.get(k) or METRIC_LABELS.get(k, k)) + " · compared period",
                               "rate" if k in RATES else "number"))
    rows = []
    for g in data.get("goals") or []:
        row = [g.get("name"), g.get("type"), g.get("match")]
        for k in keys:
            row.append(g.get(k))
            if comparing:
                row.append((g.get("cmp") or {}).get(k))
        rows.append(row)
    return cols, rows


TABLES = {"breakdown": breakdown_table, "overview": series_table, "goals": goals_table}


def describe(site_label: str, site_id: str, params: Dict[str, str], data: Dict[str, Any],
             filters: Sequence[Any], what: str) -> List[Tuple[str, str]]:
    """What a file holds, in words (the JSON "meta" and the Excel "About" sheet)."""
    out = [("Report", what), ("Property", "%s (%s)" % (site_label, site_id) if site_label != site_id else site_id),
           ("Period", "%s to %s" % (data.get("from", params.get("from", "")), data.get("to", params.get("to", ""))))]
    cmp = data.get("compare")
    if cmp:
        out.append(("Compared with", "%s to %s" % (cmp.get("from"), cmp.get("to"))))
    out.append(("Filters", "; ".join("%s %s %s" % (DIM_LABELS.get(f.dim, f.dim), OP_LABELS.get(f.op, f.op),
                                                  ", ".join(f.values)) for f in filters) or "none"))
    bots = params.get("bots", "")
    out.append(("Traffic", "bots only" if bots == "only" else "humans and bots" if bots in ("1", "include")
                else "humans only (bots excluded)"))
    if params.get("q"):
        out.append(("Search", params["q"]))
    if data.get("goal"):
        out.append(("Goal", data["goal"].get("name", "")))
    out.append(("Exported", time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())))
    return out


# -- writers ------------------------------------------------------------------------------------------

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if not math.isfinite(v):
            return ""
        return str(int(v)) if v == int(v) else ("%.6f" % v).rstrip("0").rstrip(".")
    s = str(v)
    if isinstance(v, str) and s.startswith(_FORMULA_START):  # never a formula in a spreadsheet (CSV injection)
        return "'" + s
    return s


def write_delimited(cols: List[Column], rows: Iterable[List[Any]], sep: str) -> bytes:
    import csv
    buf = io.StringIO()
    buf.write("﻿")
    if sep == "\t":
        def clean(s: str) -> str:
            return s.replace("\t", " ").replace("\r", " ").replace("\n", " ")
        buf.write("\t".join(clean(c.label) for c in cols) + "\r\n")
        for row in rows:
            buf.write("\t".join(clean(_cell_text(v)) for v in row) + "\r\n")
    else:
        w = csv.writer(buf)
        w.writerow([c.label for c in cols])
        for row in rows:
            w.writerow([_cell_text(v) for v in row])
    return buf.getvalue().encode("utf-8")


def write_json(cols: List[Column], rows: Iterable[List[Any]], meta: List[Tuple[str, str]]) -> bytes:
    doc = {"meta": dict(meta), "columns": [{"key": c.key, "label": c.label} for c in cols],
           "rows": [dict(zip([c.key for c in cols], row)) for row in rows]}
    return json.dumps(doc, ensure_ascii=False, indent=1, default=str).encode("utf-8")


_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def _x(text: Any) -> str:
    s = _XML_BAD.sub("", str(text))[:32767]
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _col_name(i: int) -> str:
    name = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        name = chr(65 + r) + name
    return name


def _sheet(cols: List[Column], rows: List[List[Any]], widths: Optional[List[int]] = None, autofilter: bool = True) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
           '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
           'state="frozen"/></sheetView></sheetViews><cols>']
    for i, c in enumerate(cols):
        width = (widths[i] if widths else None) or max(10, min(60, max([len(c.label)] + [len(_cell_text(r[i])) for r in rows[:200]]) + 2))
        out.append('<col min="%d" max="%d" width="%d" customWidth="1"/>' % (i + 1, i + 1, width))
    out.append("</cols><sheetData>")
    out.append('<row r="1">' + "".join('<c r="%s1" t="inlineStr" s="1"><is><t>%s</t></is></c>' % (_col_name(i), _x(c.label))
                                       for i, c in enumerate(cols)) + "</row>")
    for n, row in enumerate(rows, start=2):
        cells = []
        for i, v in enumerate(row):
            ref = "%s%d" % (_col_name(i), n)
            if v is None or v == "":
                continue
            if isinstance(v, float) and not math.isfinite(v):
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                style = ' s="2"' if cols[i].kind == "rate" else ""
                cells.append('<c r="%s"%s><v>%s</v></c>' % (ref, style, repr(v) if isinstance(v, float) else v))
            else:
                cells.append('<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (ref, _x(v)))
        out.append('<row r="%d">%s</row>' % (n, "".join(cells)))
    out.append("</sheetData>")
    if autofilter and cols:
        out.append('<autoFilter ref="A1:%s%d"/>' % (_col_name(len(cols) - 1), max(1, len(rows) + 1)))
    out.append("</worksheet>")
    return "".join(out)


def write_xlsx(cols: List[Column], rows: List[List[Any]], meta: List[Tuple[str, str]], title: str = "Report") -> bytes:
    """A minimal Office Open XML workbook: the report (bold, frozen header, filters) and an About sheet."""
    sheet_name = re.sub(r"[\[\]:*?/\\]", " ", title)[:31] or "Report"
    about = _sheet([Column("k", "About this export"), Column("v", "")], [[k, v] for k, v in meta], widths=[22, 80],
                   autofilter=False)
    last = "%s%d" % (_col_name(max(0, len(cols) - 1)), max(1, len(rows) + 1))
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="%s" sheetId="1" r:id="rId1"/><sheet name="About" sheetId="2" r:id="rId2"/></sheets>'
            '<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">\'%s\'!$A$1:$%s$%s</definedName></definedNames>'
            '</workbook>' % (_x(sheet_name), _x(sheet_name).replace("'", "''"), re.sub(r"\d", "", last),
                             re.sub(r"\D", "", last))),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>'),
        "xl/styles.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
            '<xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '</styleSheet>'),
        "xl/worksheets/sheet1.xml": _sheet(cols, rows),
        "xl/worksheets/sheet2.xml": about,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, text.encode("utf-8"))
    return buf.getvalue()


def render(fmt: str, cols: List[Column], rows: List[List[Any]], meta: List[Tuple[str, str]], title: str) -> bytes:
    if fmt == "csv":
        return write_delimited(cols, rows, ",")
    if fmt == "tsv":
        return write_delimited(cols, rows, "\t")
    if fmt == "json":
        return write_json(cols, rows, meta)
    return write_xlsx(cols, rows, meta, title)


def filename(site_id: str, name: str, start: str, end: str, fmt: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", "%s_%s_%s_%s" % (site_id, name, start, end)).strip("-")[:150]
    return "%s.%s" % (stem or "tidesight", fmt)
