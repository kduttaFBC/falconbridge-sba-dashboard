"""Phase 2: stream the selected bank's eligible FOIA rows into the template copy's FOIA sheet.

openpyxl is far too slow for 100k+ row sheets, so the FOIA worksheet XML is rewritten directly:
the header row and the cell styles come from the phase-1 file (template row 1 / row 2), data
columns are matched to the template's header by FIELD NAME, and every row receives the
template's own helper formulas (AO:AX) re-anchored to that row.
"""
import os
import re
import shutil
import tempfile
import zipfile
from datetime import date
from xml.sax.saxutils import escape

from openpyxl.formula.translate import Translator
from openpyxl.utils import column_index_from_string as CI, get_column_letter as L

import config as C
from source_data import parse_date

ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
EXCEL_EPOCH = date(1899, 12, 30)
SENTINEL = 987654


def _sheet_path(zf, sheet_name):
    wb_xml = zf.read("xl/workbook.xml").decode("utf-8")
    m = re.search(r'<sheet [^>]*name="%s"[^>]*r:id="([^"]+)"' % re.escape(escape(sheet_name, {'"': "&quot;"})), wb_xml)
    rid = m.group(1)
    rels = zf.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    t = re.search(r'<Relationship [^>]*Id="%s"[^>]*Target="([^"]+)"' % rid, rels) or \
        re.search(r'<Relationship [^>]*Target="([^"]+)"[^>]*Id="%s"' % rid, rels)
    target = t.group(1)
    return target.lstrip("/") if target.startswith("/") else "xl/" + target


def _row_styles(row_xml):
    return {m.group(1): m.group(2) for m in re.finditer(r'<c r="([A-Z]+)\d+"[^>]*?\ss="(\d+)"', row_xml)}


def _num(text):
    v = float(text)
    return str(int(v)) if v.is_integer() else repr(v)


def _date_serial(text):
    return str((parse_date(text) - EXCEL_EPOCH).days)


class CellWriter:
    def __init__(self):
        self.stats = {"control_chars_removed": 0, "non_numeric_kept_as_text": 0}

    def text(self, ref, s, style):
        clean = ILLEGAL_XML.sub("", s)
        if clean != s:
            self.stats["control_chars_removed"] += 1
        space = ' xml:space="preserve"' if clean != clean.strip() or "\n" in clean else ""
        return f'<c r="{ref}"{style} t="inlineStr"><is><t{space}>{escape(clean)}</t></is></c>'

    def value(self, ref, field, raw, style):
        if raw is None or raw.strip() == "":
            return ""
        if field in C.DATE_FIELDS:
            return f'<c r="{ref}"{style}><v>{_date_serial(raw)}</v></c>'
        if field in C.NUMERIC_FIELDS or (field in C.NUMERIC_IF_DIGITS_FIELDS and raw.strip().isdigit()):
            try:
                return f'<c r="{ref}"{style}><v>{_num(raw)}</v></c>'
            except ValueError:
                self.stats["non_numeric_kept_as_text"] += 1
        return self.text(ref, raw, style)


def write_foia_sheet(xlsx_path, rows, helper_formulas, as_of, log=print):
    with zipfile.ZipFile(xlsx_path) as zin:
        sheet = _sheet_path(zin, C.SH_FOIA)
        xml = zin.read(sheet).decode("utf-8")
    head, rest = xml.split("<sheetData>", 1) if "<sheetData>" in xml else (None, None)
    if head is None:
        raise RuntimeError("Unexpected FOIA sheet structure (no <sheetData>)")
    body, tail = rest.split("</sheetData>", 1)
    row_xml = re.findall(r"<row [^>]*>.*?</row>", body, flags=re.S)
    header_row = next(r for r in row_xml if re.search(r'<row [^>]*r="1"', r))
    row2 = next((r for r in row_xml if re.search(r'<row [^>]*r="2"', r)), "")
    header_vals = {}
    for m in re.finditer(r'<c r="([A-Z]+)1"[^>]*>(.*?)</c>', header_row, flags=re.S):
        t = re.search(r"<t[^>]*>(.*?)</t>", m.group(2), flags=re.S)
        header_vals[m.group(1)] = t.group(1) if t else None
    if any('t="s"' in c for c in re.findall(r'<c r="[A-Z]+1"[^>]*>', header_row)):
        # header strings stored in sharedStrings: resolve them
        with zipfile.ZipFile(xlsx_path) as zin:
            sst = zin.read("xl/sharedStrings.xml").decode("utf-8")
        strings = [re.sub(r"<[^>]+>", "", s) for s in re.findall(r"<si>(.*?)</si>", sst, flags=re.S)]
        for m in re.finditer(r'<c r="([A-Z]+)1"[^>]*t="s"[^>]*><v>(\d+)</v>', header_row):
            header_vals[m.group(1)] = strings[int(m.group(2))]
    styles = _row_styles(row2)
    data_cols = [(L(c), header_vals.get(L(c))) for c in range(1, CI("AN") + 1)]
    missing = [f for _, f in data_cols if f is None or f not in rows[0]] if rows else []
    if missing:
        raise RuntimeError(f"FOIA template headers not found in source fields: {missing}")
    # helper formula templates re-anchored by row (template row 2 -> any row)
    templates = {}
    for col_idx, f in helper_formulas.items():
        col = L(col_idx)
        if isinstance(f, str) and f.startswith("="):
            t = Translator(f, f"{col}2").translate_formula(f"{col}{SENTINEL}")
            templates[col] = escape(t[1:]).split(str(SENTINEL))
    cw = CellWriter()
    as_of_serial = str((as_of - EXCEL_EPOCH).days)
    n = len(rows)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx", dir=str(xlsx_path.parent))
    tmp.close()
    try:
        _stream(xlsx_path, tmp.name, sheet, head, header_row, tail, rows, data_cols, templates, styles, cw, as_of_serial, n)
    except BaseException:
        os.unlink(tmp.name)
        raise
    shutil.move(tmp.name, xlsx_path)
    os.chmod(xlsx_path, 0o644)
    log(f"FOIA tab: {n:,} eligible rows written with helper formulas {sorted(templates)}")
    return cw.stats


def _stream(xlsx_path, tmp_name, sheet, head, header_row, tail, rows, data_cols, templates, styles, cw, as_of_serial, n):
    with zipfile.ZipFile(xlsx_path) as zin, zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename != sheet:
                zout.writestr(item, zin.read(item.filename))
        with zout.open(sheet, "w", force_zip64=True) as out:
            dim_head = re.sub(r'<dimension ref="([A-Z]+\d+):([A-Z]+)\d+"\s*/>', lambda m: f'<dimension ref="{m.group(1)}:{m.group(2)}{n + 1}"/>', head)
            out.write(dim_head.encode("utf-8"))
            out.write(b"<sheetData>")
            out.write(header_row.encode("utf-8"))
            buf = []
            for i, rec in enumerate(rows, start=2):
                cells = []
                for col, field in data_cols:
                    st = f' s="{styles[col]}"' if col in styles else ""
                    cells.append(cw.value(f"{col}{i}", field, rec.get(field), st))
                for col, parts in templates.items():
                    st = f' s="{styles[col]}"' if col in styles else ""
                    cells.append(f'<c r="{col}{i}"{st}><f>{str(i).join(parts)}</f></c>')
                cells = [c for c in cells if c]
                if i == 2:   # as-of date parameter cell used by the template (FOIA!$AW$2)
                    st = f' s="{styles["AW"]}"' if "AW" in styles else ""
                    cells.append(f'<c r="AW2"{st}><v>{as_of_serial}</v></c>')
                    cells.sort(key=lambda c: CI(re.match(r'<c r="([A-Z]+)', c).group(1)))
                buf.append(f'<row r="{i}">{"".join(cells)}</row>')
                if len(buf) >= 2000:
                    out.write("".join(buf).encode("utf-8"))
                    buf = []
            out.write("".join(buf).encode("utf-8"))
            out.write(b"</sheetData>")
            out.write(tail.encode("utf-8"))
