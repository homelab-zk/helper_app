"""Convert uploaded PDF and DOCX files to Markdown.

PDF:  pymupdf4llm (headings, lists, tables), run with the Python of the virtual environment that has it
      (CONV_HELPER_PDF_PYTHON, default ~/.venvs/pdf2md/bin/python). Falls back to `pdftotext -layout`.
DOCX: standard library only: the file is a zip of XML. Headings, bold/italic, lists, tables, links.

convert(path) returns (markdown, info) where info has "tool", "pages" (PDF) and an optional "warning".
Raises ConvertError with a readable message when a file can't be converted.
"""
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

PDF_PYTHON = os.environ.get("CONV_HELPER_PDF_PYTHON", str(Path.home() / ".venvs/pdf2md/bin/python"))
TIMEOUT_S = 300
CONVERTIBLE = {".pdf", ".docx"}

PDF_SCRIPT = r"""
import sys, pymupdf, pymupdf4llm
doc = pymupdf.open(sys.argv[1])
pages = doc.page_count
md = pymupdf4llm.to_markdown(doc, header=False, footer=False)
sys.stdout.write(f"{pages}\n" + md)
"""


class ConvertError(Exception):
    pass


def convert(path):
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        md, info = pdf_to_md(path)
    elif ext == ".docx":
        md, info = docx_to_md(path), {"tool": "docx"}
    else:
        raise ConvertError(f"can't convert {ext} files")
    md = tidy(md)
    if not md.strip():
        raise ConvertError("no text found in the file" + (" (a scanned PDF? text in images can't be read)"
                                                         if ext == ".pdf" else ""))
    return md, info


def tidy(md):
    md = "\n".join(line.rstrip() for line in md.splitlines())
    return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"


# ---------- PDF ----------
def pdf_to_md(path):
    if Path(PDF_PYTHON).is_file():
        try:
            out = subprocess.run([PDF_PYTHON, "-I", "-c", PDF_SCRIPT, str(path)], capture_output=True, text=True,
                                 timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise ConvertError(f"converting took longer than {TIMEOUT_S} s") from None
        if out.returncode == 0:
            first, _, md = out.stdout.partition("\n")
            pages = int(first) if first.isdigit() else None
            return md, {"tool": "pymupdf4llm", "pages": pages, **scan_warning(md, pages)}
        err = (out.stderr.strip().splitlines() or ["unknown error"])[-1]
        if "password" in err.lower() or "encrypt" in err.lower():
            raise ConvertError("the PDF is password protected")
        if "FileDataError" in err or "Failed to open" in err or "cannot open" in err.lower():
            raise ConvertError("not a valid PDF file")
        raise ConvertError(f"could not convert the PDF ({err[:200]})")
    # pymupdf4llm isn't installed: plain text from poppler, better than nothing
    if not shutil.which("pdftotext"):
        raise ConvertError(f"no PDF converter available ({PDF_PYTHON} not found, no pdftotext)")
    try:
        out = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True,
                             timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ConvertError(f"converting took longer than {TIMEOUT_S} s") from None
    if out.returncode != 0:
        raise ConvertError("not a valid PDF file" if "Couldn't" in out.stderr else "pdftotext failed")
    pages = out.stdout.count("\f") or None
    text = out.stdout.replace("\f", "\n\n")
    return text, {"tool": "pdftotext (plain text)", "pages": pages, **scan_warning(text, pages)}


def scan_warning(md, pages):
    if pages and len(md.strip()) < 100 * pages:
        return {"warning": "Very little text found. This may be a scanned PDF: text inside images can't be read."}
    return {}


# ---------- DOCX ----------
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
LIST = "\x00list:"  # marks list items until the blocks are joined
R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def docx_to_md(path):
    try:
        z = zipfile.ZipFile(path)
        body = ET.fromstring(z.read("word/document.xml")).find(f"{W}body")
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise ConvertError("not a valid .docx file (old .doc files aren't supported: save as .docx first)") from None
    ctx = {"styles": _styles(z), "numbering": _numbering(z), "links": _links(z)}
    out = []
    _blocks(body, ctx, out)
    blocks = [b for b in out if b.strip()]
    md = ""
    for i, b in enumerate(blocks):
        if i:  # list items stay on consecutive lines; everything else is separated by a blank line
            md += "\n" if b.startswith(LIST) and blocks[i - 1].startswith(LIST) else "\n\n"
        md += b.removeprefix(LIST)
    return md


def _read(z, name):
    try:
        return ET.fromstring(z.read(name))
    except (KeyError, ET.ParseError):
        return None


def _styles(z):
    """styleId -> heading level (1-6) for heading and title styles."""
    root, levels = _read(z, "word/styles.xml"), {}
    if root is None:
        return levels
    for s in root.iter(f"{W}style"):
        sid = s.get(f"{W}styleId")
        name = (s.find(f"{W}name").get(f"{W}val") if s.find(f"{W}name") is not None else "").lower()
        outline = s.find(f"{W}pPr/{W}outlineLvl")
        if m := re.fullmatch(r"heading (\d)", name):
            levels[sid] = min(int(m.group(1)), 6)
        elif name == "title":
            levels[sid] = 1
        elif outline is not None and outline.get(f"{W}val", "").isdigit():
            levels[sid] = min(int(outline.get(f"{W}val")) + 1, 6)
    return levels


def _numbering(z):
    """(numId, level) -> True when that list level is numbered (not bullets)."""
    root, result = _read(z, "word/numbering.xml"), {}
    if root is None:
        return result
    abstract = {}
    for a in root.iter(f"{W}abstractNum"):
        for lvl in a.iter(f"{W}lvl"):
            fmt = lvl.find(f"{W}numFmt")
            abstract[(a.get(f"{W}abstractNumId"), lvl.get(f"{W}ilvl"))] = \
                fmt is not None and fmt.get(f"{W}val") not in ("bullet", "none")
    for n in root.iter(f"{W}num"):
        a = n.find(f"{W}abstractNumId")
        if a is not None:
            for (aid, ilvl), numbered in abstract.items():
                if aid == a.get(f"{W}val"):
                    result[(n.get(f"{W}numId"), ilvl)] = numbered
    return result


def _links(z):
    root = _read(z, "word/_rels/document.xml.rels")
    if root is None:
        return {}
    return {r.get("Id"): r.get("Target") for r in root.iter(f"{REL}Relationship")
            if r.get("TargetMode") == "External"}


def _blocks(parent, ctx, out):
    for el in parent:
        if el.tag == f"{W}p":
            out.append(_paragraph(el, ctx))
        elif el.tag == f"{W}tbl":
            out.append(_table(el, ctx))
        elif el.tag == f"{W}sdt":  # content controls (e.g. a table of contents) wrap normal blocks
            content = el.find(f"{W}sdtContent")
            if content is not None:
                _blocks(content, ctx, out)


def _on(el):
    return el is not None and el.get(f"{W}val", "true") not in ("0", "false", "none")


def _runs(p, ctx):
    """Inline text of a paragraph with **bold**, *italic* and [links](url)."""
    pieces = []  # (text, bold, italic, link)

    def run(r, link=None):
        rpr = r.find(f"{W}rPr")
        bold = _on(rpr.find(f"{W}b")) if rpr is not None else False
        italic = _on(rpr.find(f"{W}i")) if rpr is not None else False
        for c in r:
            if c.tag == f"{W}t":
                pieces.append((c.text or "", bold, italic, link))
            elif c.tag == f"{W}tab":
                pieces.append((" ", False, False, link))
            elif c.tag in (f"{W}br", f"{W}cr"):
                pieces.append(("\n", False, False, link))

    for c in p:
        if c.tag == f"{W}r":
            run(c)
        elif c.tag == f"{W}hyperlink":
            link = ctx["links"].get(c.get(R_ID))
            for r in c.iter(f"{W}r"):
                run(r, link)
        elif c.tag in (f"{W}ins", f"{W}smartTag", f"{W}fldSimple"):  # accepted changes, fields: keep their text
            for r in c.iter(f"{W}r"):
                run(r)

    # merge neighbours with the same formatting, so "**a****b**" doesn't happen
    merged = []
    for text, b, i, link in pieces:
        if merged and merged[-1][1:] == (b, i, link):
            merged[-1] = (merged[-1][0] + text, b, i, link)
        else:
            merged.append((text, b, i, link))
    out = []
    for text, b, i, link in merged:
        core = text.strip()
        if not core:
            out.append(text)
            continue
        lead, trail = text[:len(text) - len(text.lstrip())], text[len(text.rstrip()):]
        if b and i:
            core = f"***{core}***"
        elif b:
            core = f"**{core}**"
        elif i:
            core = f"*{core}*"
        if link:
            core = f"[{core}]({link})"
        out.append(lead + core + trail)
    return "".join(out)


def _paragraph(p, ctx):
    text = _runs(p, ctx).strip()
    if not text:
        return ""
    ppr = p.find(f"{W}pPr")
    style = ppr.find(f"{W}pStyle").get(f"{W}val") if ppr is not None and ppr.find(f"{W}pStyle") is not None else ""
    level = ctx["styles"].get(style)
    if level:
        return "#" * level + " " + re.sub(r"\*+", "", text).replace("\n", " ")  # headings: no bold markers
    num = ppr.find(f"{W}numPr") if ppr is not None else None
    if num is not None and num.find(f"{W}numId") is not None:
        num_id = num.find(f"{W}numId").get(f"{W}val")
        ilvl = num.find(f"{W}ilvl").get(f"{W}val") if num.find(f"{W}ilvl") is not None else "0"
        if num_id != "0":
            marker = "1." if ctx["numbering"].get((num_id, ilvl)) else "-"
            indent = "   " * int(ilvl) if ilvl.isdigit() else ""
            return LIST + indent + marker + " " + text.replace("\n", " ")
    return text.replace("\n", "  \n")


def _table(tbl, ctx):
    rows = []
    for tr in tbl.findall(f"{W}tr"):
        cells = []
        for tc in tr.findall(f"{W}tc"):
            parts = []
            for p in tc.iter(f"{W}p"):
                t = _paragraph(p, ctx).removeprefix(LIST)
                if t:
                    parts.append(t.replace("\n", " "))
            cells.append("<br>".join(parts).replace("|", "\\|"))
            span = tc.find(f"{W}tcPr/{W}gridSpan")
            if span is not None and span.get(f"{W}val", "1").isdigit():
                cells += [""] * (int(span.get(f"{W}val")) - 1)
        rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)
