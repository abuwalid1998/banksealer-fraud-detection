"""Build the VeriShield solution book (PDF).

    python docs/verishield/book/build_book.py bop  <screenshots-dir> <out.pdf>   # Bank of Palestine edition
    python docs/verishield/book/build_book.py gen  <screenshots-dir> <out.pdf>   # general marketing edition

Screenshots are PNG files named as in ``SHOTS`` (captured from the running system in Demo Mode).
All colours of an edition are defined in ``PALETTES`` - replace the hex values with the official brand guide.
"""
from __future__ import annotations

import os
import sys
import tempfile

from PIL import Image as PILImage
from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame, Image, KeepTogether, NextPageTemplate,
                                PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle)
from reportlab.platypus.tableofcontents import TableOfContents

EDITION = sys.argv[1] if len(sys.argv) > 1 else "bop"
SHOTS = sys.argv[2] if len(sys.argv) > 2 else "shots"
OUT = sys.argv[3] if len(sys.argv) > 3 else f"VeriShield_Solution_Book_{EDITION}.pdf"
BOP = EDITION == "bop"
AUTHOR = "Amjad Khaliliah"

for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",):
    if os.path.exists(path):
        pdfmetrics.registerFont(TTFont("DejaVu", path))
ARROW = '<font name="DejaVu">→</font>'

# --------------------------------------------------------------------------- colours
PALETTES = {
    # Bank of Palestine edition: green brand family (swap for the exact values of the bank's brand guide)
    "bop": {"dark": "#0B3B2A", "dark2": "#11513A", "primary": "#00843D", "accent": "#8CC63F", "soft": "#EAF5EE",
            "line": "#CFE3D6", "ink": "#2F4339", "muted": "#64776C", "mist": "#F4F9F5", "blue": "#2D7FA8"},
    # Marketing edition: VeriShield product colours
    "gen": {"dark": "#10233F", "dark2": "#1B3557", "primary": "#0BAA9B", "accent": "#3569C8", "soft": "#E6F6F4",
            "line": "#D5DDE6", "ink": "#3E5068", "muted": "#6B7C93", "mist": "#F4F7FA", "blue": "#3569C8"},
}
PAL = {k: colors.HexColor(v) for k, v in PALETTES[EDITION].items()}
DARK, DARK2, PRI, ACC, SOFT = PAL["dark"], PAL["dark2"], PAL["primary"], PAL["accent"], PAL["soft"]
LINE, INK, MUTED, MIST, BLUE = PAL["line"], PAL["ink"], PAL["muted"], PAL["mist"], PAL["blue"]
AMBER, RED, WHITE = colors.HexColor("#E3A23B"), colors.HexColor("#C84B55"), colors.white

W, H = A4
M = 18 * mm
CW = W - 2 * M


def t(bop: str, gen: str) -> str:
    """Edition-specific wording."""
    return bop if BOP else gen


BANK = t("Bank of Palestine", "your bank")
BANK_T = t("Bank of Palestine", "The bank")
CUR = t("ILS", "ILS")

S = {
    "eyebrow": ParagraphStyle("eb", fontName="Helvetica-Bold", fontSize=8.5, textColor=PRI, leading=11, spaceAfter=3),
    "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=23, leading=27, textColor=DARK, spaceAfter=8),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=13.5, leading=17, textColor=DARK, spaceBefore=9,
                         spaceAfter=5),
    "h3": ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=10.5, leading=13, textColor=DARK, spaceBefore=5,
                         spaceAfter=3),
    "body": ParagraphStyle("b", fontName="Helvetica", fontSize=9.7, leading=14.2, textColor=INK, spaceAfter=5),
    "lead": ParagraphStyle("lead", fontName="Helvetica", fontSize=11, leading=15.8, textColor=INK, spaceAfter=9),
    "small": ParagraphStyle("s", fontName="Helvetica", fontSize=8, leading=10.5, textColor=MUTED),
    "cap": ParagraphStyle("cap", fontName="Helvetica-Oblique", fontSize=8.2, leading=10.5, textColor=MUTED,
                          spaceBefore=3, spaceAfter=10),
    "cell": ParagraphStyle("c", fontName="Helvetica", fontSize=8.3, leading=10.8, textColor=INK),
    "cellb": ParagraphStyle("cb", fontName="Helvetica-Bold", fontSize=8.3, leading=10.8, textColor=DARK),
    "head": ParagraphStyle("hd", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=WHITE),
    "bullet": ParagraphStyle("bl", fontName="Helvetica", fontSize=9.7, leading=13.8, textColor=INK, leftIndent=11,
                             bulletIndent=1, spaceAfter=2.5),
    "call": ParagraphStyle("call", fontName="Helvetica-Bold", fontSize=9.7, leading=13.8, textColor=DARK),
    "mono_c": ParagraphStyle("mc", fontName="Helvetica-Bold", fontSize=24, leading=28, textColor=PRI, alignment=1),
    "kpiv": ParagraphStyle("kv", fontName="Helvetica-Bold", fontSize=19, leading=22, textColor=DARK),
    "kpil": ParagraphStyle("kl", fontName="Helvetica-Bold", fontSize=7.2, leading=9, textColor=MUTED),
    "kpih": ParagraphStyle("kh", fontName="Helvetica", fontSize=7.6, leading=9.5, textColor=MUTED),
    "mono": ParagraphStyle("mono", fontName="Courier", fontSize=7.7, leading=9.8, textColor=colors.HexColor("#DDEBE2")),
    "toc1": ParagraphStyle("toc1", fontName="Helvetica-Bold", fontSize=10.5, leading=15, textColor=DARK,
                           leftIndent=0, spaceBefore=3),
    "toc2": ParagraphStyle("toc2", fontName="Helvetica", fontSize=9, leading=12.5, textColor=INK, leftIndent=14),
}

story: list = []
fig_no = [0]
TMP = tempfile.mkdtemp(prefix="vsbook-")


def P(text, st="body"):
    return Paragraph(text, S[st])


def para(text, st="body"):
    story.append(P(text, st))


def h2(text, need=45):
    story.append(CondPageBreak(need * mm))
    p = P(text, "h2")
    p._toc = (1, text)
    story.append(p)


def h3(text):
    story.append(CondPageBreak(30 * mm))
    story.append(P(text, "h3"))


def bullets(items):
    for it in items:
        story.append(Paragraph(it, S["bullet"], bulletText="•"))
    story.append(Spacer(1, 3))


def chapter(num, eyebrow, title, lead=None):
    story.append(PageBreak())
    story.append(P(f"CHAPTER {num:02d}  /  {eyebrow.upper()}" if num else eyebrow.upper(), "eyebrow"))
    p = P(title, "h1")
    p._toc = (0, f"{num}. {title}" if num else title)
    story.append(p)
    if lead:
        para(lead, "lead")


def table(rows, widths, head=True, bold_first=True, zebra=True):
    data = []
    for i, r in enumerate(rows):
        row = []
        for j, v in enumerate(r):
            if isinstance(v, str):
                st = "head" if head and i == 0 else ("cellb" if bold_first and j == 0 else "cell")
                row.append(Paragraph(v, S[st]))
            else:
                row.append(v)
        data.append(row)
    tb = Table(data, colWidths=[w * CW for w in widths], repeatRows=1 if head else 0)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("GRID", (0, 0), (-1, -1), 0.35, LINE),
          ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
          ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6)]
    if head:
        st.append(("BACKGROUND", (0, 0), (-1, 0), DARK))
    if zebra:
        st.append(("ROWBACKGROUNDS", (0, 1 if head else 0), (-1, -1), [WHITE, MIST]))
    tb.setStyle(TableStyle(st))
    story.append(tb)
    story.append(Spacer(1, 8))


def callout(text, color=None, bg=None):
    tb = Table([[P(text, "call")]], colWidths=[CW])
    tb.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg or SOFT),
                            ("LINEBEFORE", (0, 0), (0, -1), 3.2, color or PRI),
                            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                            ("LEFTPADDING", (0, 0), (-1, -1), 11), ("RIGHTPADDING", (0, 0), (-1, -1), 10)]))
    story.append(tb)
    story.append(Spacer(1, 9))


def cards(items, cols=3):
    """items: (title, text, colour)."""
    w = CW / cols
    rows = [items[i:i + cols] for i in range(0, len(items), cols)]
    for r in rows:
        cells = []
        for title, text, _ in r:
            inner = Table([[P(f"<b>{title}</b>", "cellb")], [P(text, "cell")]], colWidths=[w - 14])
            inner.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                       ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
            cells.append(inner)
        tb = Table([cells + [""] * (cols - len(r))], colWidths=[w] * cols)
        st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 7),
              ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("LEFTPADDING", (0, 0), (-1, -1), 8),
              ("RIGHTPADDING", (0, 0), (-1, -1), 6)]
        for i, (_, _, col) in enumerate(r):
            st += [("BACKGROUND", (i, 0), (i, 0), MIST), ("LINEBEFORE", (i, 0), (i, 0), 3, col),
                   ("BOX", (i, 0), (i, 0), 0.3, LINE)]
        tb.setStyle(TableStyle(st))
        story.append(tb)
        story.append(Spacer(1, 5))
    story.append(Spacer(1, 3))


def kpis(items):
    w = CW / len(items)
    row = []
    for label, value, hint, _ in items:
        cell = Table([[P(label.upper(), "kpil")], [P(value, "kpiv")], [P(hint, "kpih")]], colWidths=[w - 12])
        cell.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
        row.append(cell)
    tb = Table([row], colWidths=[w] * len(items))
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 8),
          ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("LEFTPADDING", (0, 0), (-1, -1), 9)]
    for i, it in enumerate(items):
        st += [("BACKGROUND", (i, 0), (i, 0), WHITE), ("BOX", (i, 0), (i, 0), 0.4, LINE),
               ("LINEBEFORE", (i, 0), (i, 0), 3, it[3])]
    tb.setStyle(TableStyle(st))
    story.append(tb)
    story.append(Spacer(1, 10))


def code(text):
    tb = Table([[Paragraph(text.replace("&", "&amp;").replace("<", "&lt;").replace(" ", "&nbsp;")
                           .replace("\n", "<br/>"), S["mono"])]], colWidths=[CW])
    tb.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), DARK), ("TOPPADDING", (0, 0), (-1, -1), 8),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("LEFTPADDING", (0, 0), (-1, -1), 10)]))
    story.append(tb)
    story.append(Spacer(1, 8))


# --------------------------------------------------------------------------- screenshots
def _img(name, crop=None, width_px=1900):
    """Screenshot as JPEG; ``crop`` = (left, top, right, bottom) fractions."""
    src = os.path.join(SHOTS, name + ".png")
    if not os.path.exists(src):
        return None
    key = f"{name}-{crop}".replace(" ", "").replace("(", "").replace(")", "").replace(",", "_")
    dst = os.path.join(TMP, key + ".jpg")
    if not os.path.exists(dst):
        im = PILImage.open(src).convert("RGB")
        if crop:
            w, h = im.size
            im = im.crop((int(crop[0] * w), int(crop[1] * h), int(crop[2] * w), int(crop[3] * h)))
        if im.size[0] > width_px:
            im = im.resize((width_px, int(im.size[1] * width_px / im.size[0])), PILImage.LANCZOS)
        im.save(dst, "JPEG", quality=84, optimize=True, progressive=True)
    return dst


def figure(name, caption, crop=None, max_h=112 * mm, width=CW):
    path = _img(name, crop)
    if not path:
        print("missing screenshot", name)
        return
    iw, ih = PILImage.open(path).size
    w = width
    h = w * ih / iw
    if h > max_h:
        h = max_h
        w = h * iw / ih
    fig_no[0] += 1
    frame = Table([[Image(path, width=w, height=h)]], colWidths=[w + 2])
    frame.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("LEFTPADDING", (0, 0), (-1, -1), 1),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 1), ("TOPPADDING", (0, 0), (-1, -1), 1),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    frame.hAlign = "CENTER"
    story.append(KeepTogether([frame, P(f"<b>Figure {fig_no[0]}.</b> {caption}", "cap")]))


def figure_pair(a, b, caption, crop_a=None, crop_b=None, max_h=95 * mm):
    cells = []
    for name, crop in ((a, crop_a), (b, crop_b)):
        path = _img(name, crop)
        if not path:
            print("missing screenshot", name)
            return
        iw, ih = PILImage.open(path).size
        w = CW / 2 - 6
        h = w * ih / iw
        if h > max_h:
            h = max_h
            w = h * iw / ih
        cells.append(Image(path, width=w, height=h))
    fig_no[0] += 1
    tb = Table([cells], colWidths=[CW / 2, CW / 2])
    tb.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                            ("BOX", (0, 0), (0, 0), 0.6, LINE), ("BOX", (1, 0), (1, 0), 0.6, LINE)]))
    story.append(KeepTogether([tb, P(f"<b>Figure {fig_no[0]}.</b> {caption}", "cap")]))


# --------------------------------------------------------------------------- diagram primitives
def _box(d, x, y, w, h, title, sub="", fill=None, bar=None, title_color=None, size=7.8, sub_size=6.4, dark=False):
    d.add(Rect(x, y, w, h, rx=4, ry=4, fillColor=fill or (DARK if dark else MIST),
               strokeColor=LINE if not dark else None, strokeWidth=0.6))
    if bar is not None:
        d.add(Rect(x, y + h - 3, w, 3, fillColor=bar, strokeColor=None))
    lines = [title] if isinstance(title, str) else title
    subs = sub.split("\n") if sub else []
    total = len(lines) * (size + 1.5) + len(subs) * (sub_size + 1.8)
    cy = y + h / 2 + total / 2 - size
    for ln in lines:
        d.add(String(x + w / 2, cy, ln, fontName="Helvetica-Bold", fontSize=size,
                     fillColor=title_color or (WHITE if dark else DARK), textAnchor="middle"))
        cy -= size + 1.5
    for ln in subs:
        d.add(String(x + w / 2, cy - 0.5, ln, fontName="Helvetica", fontSize=sub_size,
                     fillColor=colors.HexColor("#C9D8CF") if dark else MUTED, textAnchor="middle"))
        cy -= sub_size + 1.8


def _arrow(d, x1, y1, x2, y2, color=None, width=1.1, label=None, both=False, dash=None):
    color = color or PRI
    ln = Line(x1, y1, x2, y2, strokeColor=color, strokeWidth=width)
    if dash:
        ln.strokeDashArray = dash
    d.add(ln)
    import math
    ang = math.atan2(y2 - y1, x2 - x1)

    def head(x, y, a):
        s = 4.2
        d.add(Polygon([x, y, x - s * math.cos(a - 0.42), y - s * math.sin(a - 0.42),
                       x - s * math.cos(a + 0.42), y - s * math.sin(a + 0.42)], fillColor=color, strokeColor=color))
    head(x2, y2, ang)
    if both:
        head(x1, y1, ang + math.pi)
    if label:
        d.add(String((x1 + x2) / 2, (y1 + y2) / 2 + 3, label, fontName="Helvetica", fontSize=5.9, fillColor=MUTED,
                     textAnchor="middle"))


def diagram(d, caption):
    fig_no[0] += 1
    story.append(KeepTogether([d, P(f"<b>Figure {fig_no[0]}.</b> {caption}", "cap")]))


def flow(steps, colors_=None):
    n = len(steps)
    gap = 6 * mm
    bw = (CW - gap * (n - 1)) / n
    bh = 22 * mm
    d = Drawing(CW, bh + 4)
    pal = colors_ or [BLUE, PRI, ACC, AMBER, DARK2, PRI]
    for i, (title, sub) in enumerate(steps):
        x = i * (bw + gap)
        _box(d, x, 0, bw, bh, title, sub, bar=pal[i % len(pal)])
        if i < n - 1:
            _arrow(d, x + bw + 1, bh / 2, x + bw + gap - 1, bh / 2)
    story.append(d)
    story.append(Spacer(1, 10))


# ---------------------------------------------------------------------------- diagrams
def engine_diagram():
    h = 92 * mm
    d = Drawing(CW, h)
    # input
    _box(d, 0, h / 2 - 16 * mm, 30 * mm, 32 * mm, ["Transaction"], "amount, type, payee,\ncountry, device, IP,\nchannel, time",
         bar=BLUE)
    _box(d, 36 * mm, h / 2 - 12 * mm, 24 * mm, 24 * mm, ["Preparation"], "aliases, types,\nvalidation,\nde-duplication",
         bar=BLUE)
    _arrow(d, 30 * mm, h / 2, 36 * mm, h / 2)
    ys = [h - 26 * mm, h / 2 - 11 * mm, 4 * mm]
    prof = [("Local profile", "HBOS per customer\n& per operation type", PRI),
            ("Temporal profile", "24 h windows vs\npersonal mean + 3σ", ACC),
            ("Global profile", "peer groups: DBSCAN\n+ CBLOF distance", AMBER)]
    for (ttl, sub, col), y in zip(prof, ys):
        _box(d, 68 * mm, y, 34 * mm, 22 * mm, ttl, sub, bar=col)
        _arrow(d, 60 * mm, h / 2, 68 * mm, y + 11 * mm)
        _arrow(d, 102 * mm, y + 11 * mm, 110 * mm, h / 2)
    d.add(String(85 * mm, h - 2.5 * mm, "BankSealer engine (unsupervised, explainable)", fontName="Helvetica-Bold",
                 fontSize=6.6, fillColor=MUTED, textAnchor="middle"))
    _box(d, 110 * mm, h / 2 - 13 * mm, 22 * mm, 26 * mm, ["Fraud", "score"], "0.5 L + 0.3 T\n+ 0.2 G\n+ bank rules",
         dark=True)
    _box(d, 138 * mm, h - 30 * mm, 36 * mm, 26 * mm, ["Signals"], "AML indicators, sanctions,\nwatch list, login risk,\nlearned model",
         bar=RED)
    _box(d, 138 * mm, 4 * mm, 36 * mm, 30 * mm, ["Decision policy"], "versioned thresholds,\nrules & actions\nPASS · REVIEW · HOLD\n· DECLINE",
         bar=PRI)
    _arrow(d, 132 * mm, h / 2, 156 * mm, 34 * mm)
    _arrow(d, 156 * mm, h - 30 * mm, 156 * mm, 34 * mm)
    d.add(String(CW - 1, 1, "every decision carries reason codes and the contribution of each factor", fontName="Helvetica-Oblique",
                 fontSize=6.2, fillColor=MUTED, textAnchor="end"))
    return d


def integration_diagram():
    h = 112 * mm
    d = Drawing(CW, h)
    cx, cy, cw_, ch = CW / 2 - 26 * mm, h / 2 - 17 * mm, 52 * mm, 34 * mm
    _box(d, cx, cy, cw_, ch, ["VeriShield"], "decision API · connectors\nengine · cases · reports\naudit · MCP",
         dark=True, size=10)
    left = [("Internet & mobile banking", "pre-authorisation call"), ("Core banking", "transfers, history, KYC"),
            ("Card switch / POS / ATM", "card & cash events"), ("Payment gateway", "real-time stream")]
    right = [("Active Directory", "sign-in & roles"), ("SMTP relay & SMS gateway", "alerts, customer confirmation"),
             ("Results DB / data warehouse", "every decision written"), ("FFU (goAML)", "STR XML / PDF")]
    bw, bh = 46 * mm, 17 * mm
    for i, (ttl, sub) in enumerate(left):
        y = h - (i + 1) * (bh + 9 * mm) + 4 * mm
        _box(d, 0, y, bw, bh, ttl, sub, bar=BLUE, size=7.2)
        _arrow(d, bw, y + bh / 2, cx, cy + ch / 2 + (1.5 - i) * 7 * mm, color=BLUE, both=(i == 0))
    for i, (ttl, sub) in enumerate(right):
        y = h - (i + 1) * (bh + 9 * mm) + 4 * mm
        _box(d, CW - bw, y, bw, bh, ttl, sub, bar=PRI, size=7.2)
        _arrow(d, cx + cw_, cy + ch / 2 + (1.5 - i) * 7 * mm, CW - bw, y + bh / 2, color=PRI, both=(i == 0))
    _box(d, cx + 4 * mm, 1 * mm, cw_ - 8 * mm, 13 * mm, "AI agents (Claude, MCP)", "read-only tools, masked data", bar=ACC,
         size=7.2)
    _arrow(d, cx + cw_ / 2, 14 * mm, cx + cw_ / 2, cy, color=ACC, both=True)
    _box(d, cx + 4 * mm, h - 14 * mm, cw_ - 8 * mm, 13 * mm, "Analysts' browsers (HTTPS)", "5 roles, MFA, audit", bar=AMBER,
         size=7.2)
    _arrow(d, cx + cw_ / 2, h - 14 * mm, cx + cw_ / 2, cy + ch, color=AMBER, both=True)
    return d


def sequence_diagram():
    lanes = ["Customer", "Mobile app", "VeriShield", "SMS gateway", "Core banking"]
    h = 88 * mm
    d = Drawing(CW, h)
    lw = CW / len(lanes)
    xs = [lw * i + lw / 2 for i in range(len(lanes))]
    for x, name in zip(xs, lanes):
        d.add(Rect(x - lw / 2 + 4, h - 10 * mm, lw - 8, 8 * mm, rx=3, ry=3, fillColor=DARK, strokeColor=None))
        d.add(String(x, h - 6.6 * mm, name, fontName="Helvetica-Bold", fontSize=7.4, fillColor=WHITE, textAnchor="middle"))
        ln = Line(x, 2 * mm, x, h - 10 * mm, strokeColor=LINE, strokeWidth=0.8)
        ln.strokeDashArray = [2, 2]
        d.add(ln)
    steps = [(0, 1, "1. transfer 9,500 ILS to a new account"), (1, 2, "2. POST /decisions/evaluate (pre-authorisation)"),
             (2, 1, "3. HOLD + reasons (in milliseconds)"), (2, 3, "4. confirmation request"), (3, 0, "5. 'Did you send 9,500 ILS?'"),
             (0, 3, "6. reply NO"), (3, 2, "7. answer received"), (2, 4, "8. reject command (outbound callback)"),
             (2, 2, "9. urgent case opened, payee watch-listed")]
    y = h - 15 * mm
    for a, b, label in steps:
        if a == b:
            d.add(Rect(xs[a] - 3, y - 4, 6, 8, fillColor=AMBER, strokeColor=None))
            d.add(String(xs[a] + 6, y - 2, label, fontName="Helvetica", fontSize=6.5, fillColor=INK))
        else:
            _arrow(d, xs[a], y, xs[b], y, color=PRI if b > a else BLUE)
            d.add(String((xs[a] + xs[b]) / 2, y + 2.2, label, fontName="Helvetica", fontSize=6.3, fillColor=INK,
                         textAnchor="middle"))
        y -= 8.2 * mm
    return d


def architecture_diagram():
    h = 128 * mm
    d = Drawing(CW, h)
    layers = [
        ("Channels", ["Mobile / internet", "Core banking", "Cards & ATM", "Gateway", "Files / DB"], BLUE),
        ("Ingestion", ["Decision API", "Event API", "Connectors runtime", "Field mapping", "Dead-letter queue"], BLUE),
        ("Decisioning", ["BankSealer engine", "AML indicators", "Screening & watch list", "Login & device risk",
                         "Policy engine"], PRI),
        ("Actions", ["Execution callback", "Customer SMS", "Alerts", "Results DB", "Notifications"], ACC),
        ("Investigation", ["Alert queue", "Cases & evidence", "Network & timeline", "STR workflow", "KYC reviews"], AMBER),
        ("Insight & control", ["Dashboards", "Reports", "Model ops", "Audit chain", "MCP / AI"], DARK2),
    ]
    lh = 17.5 * mm
    lab_w = 30 * mm
    for i, (name, items, col) in enumerate(layers):
        y = h - (i + 1) * (lh + 3.4 * mm)
        d.add(Rect(0, y, lab_w, lh, rx=3, ry=3, fillColor=col, strokeColor=None))
        d.add(String(lab_w / 2, y + lh / 2 - 3, name, fontName="Helvetica-Bold", fontSize=8, fillColor=WHITE,
                     textAnchor="middle"))
        iw = (CW - lab_w - 4 * mm) / len(items)
        for j, it in enumerate(items):
            x = lab_w + 4 * mm + j * iw
            d.add(Rect(x, y, iw - 2.5 * mm, lh, rx=3, ry=3, fillColor=MIST, strokeColor=LINE, strokeWidth=0.5))
            d.add(String(x + (iw - 2.5 * mm) / 2, y + lh / 2 - 3, it, fontName="Helvetica", fontSize=7.2, fillColor=DARK,
                         textAnchor="middle"))
        if i < len(layers) - 1:
            _arrow(d, lab_w / 2, y, lab_w / 2, y - 3.4 * mm + 0.5, color=MUTED, width=0.8)
    d.add(String(CW, 0, "Security across every layer: TLS, RBAC, MFA, tenant & workspace scoping, encrypted vault, hash-chained audit",
                 fontName="Helvetica-Oblique", fontSize=6.4, fillColor=MUTED, textAnchor="end"))
    return d


def deployment_diagram():
    h = 62 * mm
    d = Drawing(CW, h)
    _box(d, 0, 18 * mm, 40 * mm, 28 * mm, ["Bank systems"], "channels, core banking,\ncard switch, AD,\nSMTP, SMS", bar=BLUE)
    _box(d, 52 * mm, 8 * mm, 60 * mm, 48 * mm, ["VeriShield application server"],
         "Windows Server or Linux\nPython 3.12 service (HTTPS)\nweb UI · REST API · MCP\nconnectors · engine · jobs\n"
         "offline installer (no internet)", dark=True, size=8.4)
    _box(d, 124 * mm, 30 * mm, 50 * mm, 24 * mm, ["PostgreSQL"], "VeriShield database\n(SQLite for pilots)", bar=PRI)
    _box(d, 124 * mm, 2 * mm, 50 * mm, 22 * mm, ["Results database"], "decision copy for DWH /\nreporting (optional)", bar=ACC)
    _arrow(d, 40 * mm, 32 * mm, 52 * mm, 32 * mm, both=True, color=BLUE)
    _arrow(d, 112 * mm, 42 * mm, 124 * mm, 42 * mm, both=True)
    _arrow(d, 112 * mm, 14 * mm, 124 * mm, 13 * mm)
    return d


def erd_diagram():
    h = 150 * mm
    d = Drawing(CW, h)
    bw, bh = 37 * mm, 22 * mm
    pos = {}

    def ent(key, x, y, cols, col):
        pos[key] = (x, y)
        d.add(Rect(x, y, bw, bh, rx=3, ry=3, fillColor=WHITE, strokeColor=col, strokeWidth=0.9))
        d.add(Rect(x, y + bh - 6 * mm, bw, 6 * mm, rx=3, ry=3, fillColor=col, strokeColor=None))
        d.add(String(x + 3, y + bh - 4.3 * mm, key, fontName="Helvetica-Bold", fontSize=7.6, fillColor=WHITE))
        for k, c in enumerate(cols):
            d.add(String(x + 3, y + bh - 9.5 * mm - k * 3.4 * mm, c, fontName="Helvetica", fontSize=6.2, fillColor=INK))

    def rel(a, b, label="1 : n"):
        ax, ay = pos[a]
        bx, by = pos[b]
        x1, y1 = ax + bw / 2, ay + bh / 2
        x2, y2 = bx + bw / 2, by + bh / 2
        # connect box edges
        if abs(x1 - x2) > abs(y1 - y2):
            x1 += bw / 2 * (1 if x2 > x1 else -1)
            x2 -= bw / 2 * (1 if x2 > x1 else -1)
        else:
            y1 += bh / 2 * (1 if y2 > y1 else -1)
            y2 -= bh / 2 * (1 if y2 > y1 else -1)
        d.add(Line(x1, y1, x2, y2, strokeColor=MUTED, strokeWidth=0.7))
        d.add(String((x1 + x2) / 2 + 2, (y1 + y2) / 2 + 2, label, fontName="Helvetica", fontSize=5.6, fillColor=MUTED))

    gx = [0, 46 * mm, 92 * mm, 137 * mm]
    gy = [h - (i + 1) * bh - 2 * mm - i * 8 * mm for i in range(5)]
    ent("tenants", gx[0], gy[0], [t("id (BOP)  name", "id (code)  name"), "currency, timezone", "branding, settings"], DARK2)
    ent("users", gx[1], gy[0], ["id  username  role", "auth_source (LOCAL/AD)", "mfa, lockout, phone"], DARK2)
    ent("audit_events", gx[2], gy[0], ["id  actor  action", "before / after", "prev_hash → hash"], DARK2)
    ent("connectors", gx[3], gy[0], ["id  type  direction", "config, mapping", "status, counters"], BLUE)
    ent("customers", gx[0], gy[1], ["customer_ref  name", "risk_category  segment", "expected turnover"], PRI)
    ent("transactions", gx[1], gy[1], ["tx_ref  customer_ref", "type  amount  currency", "payee  device  ip  ts"], PRI)
    ent("decisions", gx[2], gy[1], ["id  tx_ref  decision", "fraud_score  aml level", "reason_codes  policy"], PRI)
    ent("execution_events", gx[3], gy[1], ["decision_id  status", "RELEASE / REJECT", "callback result"], BLUE)
    ent("model_versions", gx[0], gy[2], ["id  kind  status", "ACTIVE / SHADOW", "metrics, params"], ACC)
    ent("alerts", gx[1], gy[2], ["id  severity  status", "kind (AML/FRAUD/", "SCREENING)  summary"], AMBER)
    ent("cases", gx[2], gy[2], ["id  status  priority", "owner  assignee  SLA", "purpose  disposition"], AMBER)
    ent("case_notes / evidence", gx[3], gy[2], ["case_id  author", "kind  body  file", "sha256 (evidence)"], AMBER)
    ent("policy_versions", gx[0], gy[3], ["id  version  status", "thresholds  rules", "approved_by"], ACC)
    ent("screening_matches", gx[1], gy[3], ["customer / payment", "list  score  status", "sanction_entries"], RED)
    ent("str_reports", gx[2], gy[3], ["case_id  status", "narrative  approved_by", "FFU reference"], AMBER)
    ent("customer_confirmations", gx[3], gy[3], ["tx_ref  phone  status", "reply YES / NO", "expires_at"], RED)
    ent("watchlist", gx[0], gy[4], ["kind  value  reason", "ACCOUNT/DEVICE/IP", "active  created_by"], RED)
    ent("login_events", gx[1], gy[4], ["customer_ref  device", "country  success", "risk_score factors"], RED)
    ent("customer_reviews", gx[2], gy[4], ["customer_ref  reason", "due_at  checklist", "outcome  new_risk"], AMBER)
    ent("report_schedules", gx[3], gy[4], ["report  format", "frequency  recipients", "next_run"], DARK2)
    for a, b in (("tenants", "users"), ("users", "audit_events"), ("customers", "transactions"),
                 ("transactions", "decisions"), ("decisions", "execution_events"), ("decisions", "alerts"),
                 ("alerts", "cases"), ("cases", "case_notes / evidence"), ("cases", "str_reports")):
        rel(a, b)
    d.add(String(CW, 0, "Every business table also carries tenant_id and space (LIVE / DEMO). Lower rows: compliance "
                        "and operations tables, linked by customer_ref, tx_ref or case_id.",
                 fontName="Helvetica-Oblique", fontSize=6.2, fillColor=MUTED, textAnchor="end"))
    return d


def mcp_diagram():
    h = 58 * mm
    d = Drawing(CW, h)
    _box(d, 0, 16 * mm, 36 * mm, 26 * mm, ["AI agent"], "Claude Desktop,\nClaude Code or any\nMCP client", bar=ACC)
    _box(d, 48 * mm, 6 * mm, 50 * mm, 46 * mm, ["VeriShield MCP gateway"],
         "/mcp (HTTP) or stdio bridge\naccess key + scopes\ntool allow-list (IT Admin)\nrate limit per minute\nON / OFF switch\nevery call audited",
         dark=True, size=8.4)
    _box(d, 110 * mm, 26 * mm, 30 * mm, 22 * mm, ["REST API"], "same permission\nchecks as the UI", bar=PRI)
    _box(d, 110 * mm, 2 * mm, 30 * mm, 20 * mm, ["Audit trail"], "tool, actor,\noutcome", bar=AMBER)
    _box(d, 148 * mm, 26 * mm, 26 * mm, 22 * mm, ["Data"], "masked\ncustomer data", bar=BLUE)
    _arrow(d, 36 * mm, 29 * mm, 48 * mm, 29 * mm, both=True, color=ACC, label="tools/call")
    _arrow(d, 98 * mm, 37 * mm, 110 * mm, 37 * mm, both=True)
    _arrow(d, 98 * mm, 14 * mm, 110 * mm, 12 * mm, color=AMBER)
    _arrow(d, 140 * mm, 37 * mm, 148 * mm, 37 * mm, both=True, color=BLUE)
    return d


def swimlane_diagram():
    lanes = [("Customer", BLUE), ("VeriShield", PRI), ("AML Manager", AMBER), ("AML Analyst", ACC),
             ("Chief AML / FFU", DARK2)]
    h = 104 * mm
    d = Drawing(CW, h)
    lab = 27 * mm
    lh = h / len(lanes)
    for i, (name, col) in enumerate(lanes):
        y = h - (i + 1) * lh
        d.add(Rect(0, y + 1, CW, lh - 2, fillColor=MIST if i % 2 == 0 else WHITE, strokeColor=LINE, strokeWidth=0.4))
        d.add(Rect(0, y + 1, lab, lh - 2, fillColor=col, strokeColor=None))
        d.add(String(lab / 2, y + lh / 2 - 3, name, fontName="Helvetica-Bold", fontSize=7.4, fillColor=WHITE,
                     textAnchor="middle"))
    bw, bh = 25 * mm, 12 * mm
    col_x = [lab + 3 * mm + k * 29.5 * mm for k in range(5)]

    def step(lane, colx, text):
        y = h - (lane + 1) * lh + (lh - bh) / 2
        x = col_x[colx]
        d.add(Rect(x, y, bw, bh, rx=3, ry=3, fillColor=WHITE, strokeColor=lanes[lane][1], strokeWidth=0.9))
        for k, ln in enumerate(text.split("\n")):
            d.add(String(x + bw / 2, y + bh - 4.6 * mm - k * 3.2 * mm, ln, fontName="Helvetica", fontSize=6.1,
                         fillColor=DARK, textAnchor="middle"))
        return x, y

    pts = [step(0, 0, "sends transfer\nfrom mobile app"), step(1, 0, "scores in ms:\nHOLD + alert"),
           step(0, 1, "replies NO to\nSMS confirmation"), step(1, 1, "rejects payment,\nopens urgent case"),
           step(2, 2, "reviews alert,\nassigns analyst"), step(3, 2, "investigates:\nnotes, network"),
           step(3, 3, "recommends\nSTR"), step(2, 3, "approves outcome,\nprepares STR"),
           step(4, 4, "Chief approves;\nSTR sent to FFU"), step(1, 4, "labels outcome,\nmodel learns")]
    order = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8), (8, 9)]
    for a, b in order:
        (x1, y1), (x2, y2) = pts[a], pts[b]
        if abs(x1 - x2) < 1:
            ya, yb = (y1, y2 + bh) if y1 > y2 else (y1 + bh, y2)
            _arrow(d, x1 + bw / 2, ya, x2 + bw / 2, yb, width=0.8)
        else:
            _arrow(d, x1 + bw, y1 + bh / 2, x2, y2 + bh / 2, width=0.8)
    return d


# ---------------------------------------------------------------------------- page furniture
def shield(c, x, y, s, color, width=None):
    c.saveState()
    c.setStrokeColor(color)
    c.setLineWidth(width or s * 0.09)
    c.setLineJoin(1)
    c.setLineCap(1)
    k = s / 64.0
    p = c.beginPath()
    p.moveTo(x + 32 * k, y + 59 * k)
    p.lineTo(x + 54 * k, y + 51 * k)
    p.lineTo(x + 54 * k, y + 34 * k)
    p.curveTo(x + 54 * k, y + 20 * k, x + 44.5 * k, y + 9.5 * k, x + 32 * k, y + 5 * k)
    p.curveTo(x + 19.5 * k, y + 9.5 * k, x + 10 * k, y + 20 * k, x + 10 * k, y + 34 * k)
    p.lineTo(x + 10 * k, y + 51 * k)
    p.close()
    c.drawPath(p, stroke=1, fill=0)
    q = c.beginPath()
    q.moveTo(x + 21 * k, y + 32 * k)
    q.lineTo(x + 29 * k, y + 24 * k)
    q.lineTo(x + 44 * k, y + 40 * k)
    c.drawPath(q, stroke=1, fill=0)
    c.restoreState()


def cover(c, doc):
    c.saveState()
    c.setFillColor(DARK)
    c.rect(0, 0, W, H, stroke=0, fill=1)
    c.setFillColor(DARK2)
    p = c.beginPath()
    p.moveTo(W * 0.30, H)
    p.lineTo(W * 0.52, H)
    p.lineTo(W, H * 0.43)
    p.lineTo(W, H * 0.21)
    p.close()
    c.drawPath(p, stroke=0, fill=1)
    c.setFillColor(PRI)
    c.rect(0, 0, 5 * mm, H, stroke=0, fill=1)
    c.setFillColor(ACC)
    c.rect(5 * mm, 0, 1.6 * mm, H, stroke=0, fill=1)
    c.setStrokeColor(colors.Color(1, 1, 1, alpha=0.10))
    c.setLineWidth(0.7)
    for r in range(14, 96, 12):
        c.circle(W * 0.82, H * 0.74, r * mm * 0.62, stroke=1, fill=0)
    shield(c, 22 * mm, H - 44 * mm, 18 * mm, ACC)
    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 12.5)
    c.drawString(42 * mm, H - 31.5 * mm, "V E R I S H I E L D")
    c.setFillColor(colors.HexColor("#BFD3C7") if BOP else colors.HexColor("#B9C8DC"))
    c.setFont("Helvetica", 8.5)
    c.drawString(42 * mm, H - 36.5 * mm, "Transaction Intelligence. Trusted Decisions.")
    c.setFillColor(ACC)
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(24 * mm, H * 0.60, t("SOLUTION BOOK  |  PREPARED FOR BANK OF PALESTINE",
                                      "SOLUTION BOOK  |  FOR BANKS AND FINANCIAL INSTITUTIONS"))
    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 38)
    c.drawString(23 * mm, H * 0.60 - 17 * mm, "Stop fraud before")
    c.drawString(23 * mm, H * 0.60 - 31 * mm, "the money leaves.")
    c.setFont("Helvetica", 13.5)
    c.setFillColor(colors.HexColor("#D9E7DE") if BOP else colors.HexColor("#D5E0EE"))
    c.drawString(24 * mm, H * 0.60 - 44 * mm, "Real-time fraud decisioning, AML investigation and regulatory reporting")
    c.drawString(24 * mm, H * 0.60 - 51 * mm, t("built on the BankSealer behavioural engine - for Bank of Palestine.",
                                                "built on the BankSealer behavioural engine."))
    # pillars
    y0, x0, pw = H * 0.22, 24 * mm, (W - 48 * mm) / 4
    pills = [("DETECT", "Behaviour of every", "customer, in ms."), ("DECIDE", "Pass, review, hold or", "decline - explained."),
             ("INVESTIGATE", "Cases, network graph,", "screening and STR."), ("CONNECT", "Plug into channels,", "AD, SMS and agents.")]
    for i, (a, b, cc) in enumerate(pills):
        x = x0 + i * pw
        c.setFillColor(colors.Color(1, 1, 1, alpha=0.06))
        c.setStrokeColor(colors.Color(1, 1, 1, alpha=0.18))
        c.rect(x, y0, pw - 3 * mm, 25 * mm, stroke=1, fill=1)
        c.setFillColor(ACC)
        c.rect(x, y0 + 25 * mm - 1.2 * mm, pw - 3 * mm, 1.2 * mm, stroke=0, fill=1)
        c.setFillColor(WHITE)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(x + 4 * mm, y0 + 17 * mm, a)
        c.setFillColor(colors.HexColor("#C2D4C9") if BOP else colors.HexColor("#B8C6DA"))
        c.setFont("Helvetica", 7.8)
        c.drawString(x + 4 * mm, y0 + 10.5 * mm, b)
        c.drawString(x + 4 * mm, y0 + 6.5 * mm, cc)
    c.setFillColor(WHITE)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(24 * mm, 34 * mm, f"Developed by {AUTHOR}")
    c.setFillColor(colors.HexColor("#BFD3C7") if BOP else colors.HexColor("#B9C8DC"))
    c.setFont("Helvetica", 8.4)
    c.drawString(24 * mm, 28.5 * mm, t("VeriShield 1.3  |  November 2026  |  Confidential - prepared for Bank of Palestine",
                                       "VeriShield 1.3  |  November 2026  |  Product overview"))
    c.setFont("Helvetica", 7)
    c.drawString(24 * mm, 22 * mm, "All screens show the VeriShield Demo Mode with synthetic customers and transactions - no real customer data.")
    c.restoreState()


def page(c, doc):
    c.saveState()
    c.setFillColor(PRI)
    c.rect(0, H - 4 * mm, W, 4 * mm, stroke=0, fill=1)
    c.setFillColor(ACC)
    c.rect(0, H - 5 * mm, W * 0.22, 1 * mm, stroke=0, fill=1)
    shield(c, M - 1 * mm, H - 14.5 * mm, 6.5 * mm, PRI, width=0.8)
    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(M + 6.5 * mm, H - 11.3 * mm, "VERISHIELD")
    c.setFont("Helvetica", 7.5)
    c.setFillColor(MUTED)
    c.drawString(M + 24 * mm, H - 11.3 * mm, t("Solution book  ·  Bank of Palestine", "Solution book"))
    c.drawRightString(W - M, H - 11.3 * mm, getattr(doc, "chapter", ""))
    c.setStrokeColor(LINE)
    c.setLineWidth(0.5)
    c.line(M, 13 * mm, W - M, 13 * mm)
    c.setFont("Helvetica", 7.2)
    c.drawString(M, 8.6 * mm, f"Developed by {AUTHOR}" + t("  ·  Confidential", ""))
    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", 8)
    c.drawRightString(W - M, 8.6 * mm, str(doc.page))
    c.restoreState()


class Book(BaseDocTemplate):
    def __init__(self, path):
        super().__init__(path, pagesize=A4, leftMargin=M, rightMargin=M, topMargin=20 * mm, bottomMargin=18 * mm,
                         title=t("VeriShield Solution Book - Bank of Palestine", "VeriShield Solution Book"),
                         author=AUTHOR, subject="Fraud and AML decisioning platform", creator="VeriShield")
        fr = Frame(M, 18 * mm, CW, H - 38 * mm, id="body", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        self.addPageTemplates([PageTemplate("cover", [Frame(0, 0, W, H, id="c")], onPage=cover),
                               PageTemplate("body", [fr], onPageEnd=page)])
        self.chapter = ""

    def beforeDocument(self):
        self.chapter = "Contents"

    def afterFlowable(self, f):
        toc = getattr(f, "_toc", None)
        if toc:
            level, text = toc
            key = f"k{id(f)}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=level, closed=level > 0)
            self.notify("TOCEntry", (level, text, self.page, key))
            if level == 0:
                self.chapter = text


# =============================================================================== CONTENT
story += [NextPageTemplate("body"), PageBreak()]

# -------------------------------------------------------------------- about this book + contents
story.append(P("ABOUT THIS BOOK", "eyebrow"))
story.append(P(t("VeriShield for Bank of Palestine", "VeriShield in one book"), "h1"))
para(t("This book explains VeriShield to the people at Bank of Palestine who will decide on it, run it and live "
       "with it every day: the AML and fraud teams, IT and information security, risk and compliance, and executive "
       "management. Each chapter answers one question, from how the detection algorithm thinks to how the "
       "database is designed and how AI agents can work with the platform safely.",
       "This book explains VeriShield to the people who decide on a fraud and AML platform, run it and live with it "
       "every day: fraud and AML teams, IT and information security, risk and compliance, and executive management. "
       "Each chapter answers one question, from how the detection algorithm thinks to how the database is designed "
       "and how AI agents can work with the platform safely."), "lead")
para("Screens are taken from the running system in Demo Mode, where a built-in payment gateway simulator streams "
     "synthetic but realistic transactions through the real engine. Figures quoted as measured come from that "
     "environment and must be confirmed on " + t("the bank's own data during the pilot.", "your own data during a pilot."))
toc = TableOfContents()
toc.levelStyles = [S["toc1"], S["toc2"]]
toc.dotsMinLevel = 0
story.append(Spacer(1, 4))
story.append(toc)

# -------------------------------------------------------------------- executive summary
chapter(0, "Executive summary", "VeriShield at a glance",
        t("VeriShield scores every payment of Bank of Palestine's customers against their own behaviour before it is "
          "executed, explains every decision, and gives the AML department one place to investigate, decide and "
          "report to the Financial Follow-Up Unit.",
          "VeriShield scores every payment against the customer's own behaviour before it is executed, explains every "
          "decision, and gives the AML department one place to investigate, decide and report to the financial "
          "intelligence unit."))
kpis([("Decision latency", "~6 ms", "median engine time; p95 under 10 ms", PRI),
      ("Transaction types", "7", "transfers, cards, cash, top-ups, incoming", ACC),
      ("Roles", "5", "segregated duties, MFA, AD sign-in", AMBER),
      ("Connector types", "13", "API, stream, files, DB, MQ, callbacks", BLUE)])
cards([("Detect", "The BankSealer engine learns each customer's normal behaviour - amounts, payees, countries, times, "
                  "devices - and the behaviour of similar customers. No fraud labels are needed to start.", PRI),
       ("Decide", "A versioned decision policy turns scores, AML indicators, sanctions hits and login risk into "
                  "PASS, REVIEW, HOLD or DECLINE, with reason codes a customer-service agent can read.", ACC),
       ("Confirm", "Held payments trigger an SMS to the customer. YES releases the payment, NO rejects it, "
                   "watch-lists the payee and opens an urgent case - automatically.", AMBER),
       ("Investigate", "Alert queue, cases with evidence and SLA, customer 360 timeline, network graph of linked "
                       "accounts, sanctions/PEP screening and periodic KYC reviews.", BLUE),
       ("Report", "Suspicious transaction reports drafted from the case, approved by the AML Manager or Chief, "
                  "exported as goAML-style XML or a branded PDF. Board and regulatory reports on schedule.", DARK2),
       ("Govern", "Five roles, masked data for IT, MFA, Active Directory, encrypted secrets, hash-chained audit "
                  "trail, model health monitoring, shadow mode and policy simulation.", RED)])
callout(t("Recommended path for Bank of Palestine: a 4-6 week pilot in shadow mode on one channel (mobile "
          "transfers), then activation of HOLD with SMS confirmation, then extension to cards, ATM and incoming "
          "transfers.",
          "Recommended path: a 4-6 week pilot in shadow mode on one channel, then activation of HOLD with SMS "
          "confirmation, then extension to cards, ATM and incoming transfers."))
figure("mgr_overview", "AML Manager overview - live scoring, decision mix, alerts and cases (Demo Mode).")

# -------------------------------------------------------------------- 1. how it works
chapter(1, "How it works", "How it works: the BankSealer algorithm",
        "VeriShield's engine implements BankSealer, a decision-support approach for online-banking fraud published "
        "by Carminati, Caron, Maggi, Epifani and Zanero (Computers &amp; Security, 2015). Its idea is simple: a "
        "fraudster can steal credentials, but cannot steal the customer's habits.")
h2("Three profiles, three questions")
table([["Profile", "Question it answers", "Method", "Example reason"],
       ["Local", "Is this payment unusual <b>for this customer</b>?",
        "Histogram-based outlier score (HBOS): one histogram per feature, per customer and per transaction type",
        "Beneficiary account never used before by this customer"],
       ["Temporal", "Is the customer's <b>recent activity</b> unusual?",
        "24-hour sliding windows (count, total, largest amount, distinct payees, foreign transfers) compared with "
        "the customer's own mean + 3 standard deviations",
        "Total sent in 24 h is 6.2x the customer's usual level"],
       ["Global", "Does the customer behave like <b>their peers</b>?",
        "Customers are clustered by spending pattern (iterative DBSCAN in Mahalanobis space); distance to the peer "
        "group is scored with CBLOF",
        "Recharged amount per 30 days far above the peer-group average"]],
      [0.12, 0.25, 0.36, 0.27])
diagram(engine_diagram(), "From a raw transaction to an explained decision.")
h2("How the score is built")
bullets([
    "<b>Local (HBOS).</b> For each feature (amount, payee account, payee country, connection country/ASN, IP, "
    "hour; merchant and MCC for cards; terminal and location for cash) the engine looks up how often the customer "
    "used that value. Rare values add <i>w · log(1/density)</i>. New customers borrow from the population "
    "histogram until they have history (shrinkage λ = n / (n + 10)).",
    "<b>Temporal.</b> Every 24-hour window is aggregated. An excess over the customer's personal threshold adds "
    "to the score in proportion to how far it goes; customers with little history are blended with the "
    "population statistics.",
    "<b>Global.</b> Each customer is summarised as a vector (operations and amounts per type, average amount, "
    "distinct payees, foreign and night-time shares). Peer groups are found automatically; a customer drifting "
    "away from every group is suspicious even if their own history is short.",
    f"<b>Combination.</b> fraud score = 0.5 · local + 0.3 · temporal + 0.2 · global, plus bank "
    "rules (amount limits per type, high-risk countries, night hours). Weights are configurable per institution.",
    "<b>Explanation.</b> The engine keeps the contribution of every feature, so each decision lists its top "
    "reasons in plain language, ranked by weight.",
    "<b>Learning.</b> Profiles are updated online with every accepted transaction; transactions judged suspicious "
    "are kept out of the learning so fraud does not become 'normal'. Outcomes of investigations train an "
    "additional supervised model (logistic regression) that is run in shadow first.",
])
h2("A worked example")
para("A retail customer usually pays 300-800 ILS to four known payees in the afternoon from the same phone. At "
     "02:10 a transfer of 9,500 ILS is requested to an account in another country, from a device never seen before:")
table([["Signal", "What the engine sees", "Effect"],
       ["Local", "New payee account, new payee country, amount in the top 1% of the customer's history, unusual hour",
        "high local score"],
       ["Temporal", "24-hour total and largest amount far above mean + 3σ", "excess added"],
       ["Global", "The customer's vector moves away from their peer group of salaried retail customers",
        "moderate global score"],
       ["Signals", "New device and new country at login; payee country on the high-risk list", "policy rule fires"],
       ["Decision", "Score 0.82 ≥ HOLD threshold for mobile transfers", "<b>HOLD + SMS confirmation</b>"]],
      [0.14, 0.62, 0.24])
figure_pair("tx_decision", "tx_why", "A held transaction: the score of each profile and the reasons behind the decision.",
            crop_a=(0.45, 0, 1, 1), crop_b=(0.45, 0, 1, 1), max_h=105 * mm)
para("On the synthetic benchmark shipped with the engine (45 days of training, 10 days scored with injected "
     "fraud) the BankSealer engine reaches an AUC of about 0.99, precision about 0.94 and recall about 0.83-0.85. "
     "In the Demo Mode stream with policy, AML and screening layers the false-positive rate is about 1.3% of "
     "transactions. These are synthetic results; the pilot measures them on " + BANK + "'s data.", "small")

# -------------------------------------------------------------------- 2. why
chapter(2, "Why", "Why we need a fraud engine",
        "Payments have become instant and digital; fraud has followed. Once money leaves the bank it is usually "
        "gone within minutes, so the only reliable control is a decision <i>before</i> execution.")
h2("What has changed")
cards([("Instant digital payments", "Mobile and internet banking move money in seconds, at any hour. Manual review "
                                    "after the fact cannot stop it.", BLUE),
       ("Social engineering", "Authorised push-payment scams and account takeover use the customer's own device "
                              "and credentials - passwords and OTPs do not stop them.", RED),
       ("Mule networks", "Stolen funds are moved through chains of mule accounts and cashed out at ATMs, often "
                         "across several banks and countries.", AMBER),
       ("Card fraud", "Card testing with small e-commerce payments, followed by large purchases or ATM cash-out at "
                      "night.", ACC),
       ("Regulation", t("PMA instructions and the AML/CFT law require monitoring, investigation, record keeping and "
                        "timely reporting to the Financial Follow-Up Unit.",
                        "AML/CFT regulation requires monitoring, investigation, record keeping and timely reporting "
                        "to the financial intelligence unit."), DARK2),
       ("Customer trust", "Customers expect the bank to notice when something is wrong - and to ask before "
                          "blocking them.", PRI)])
h2("Rules alone are not enough")
para("Static rules ('more than 10,000 ILS', 'new beneficiary') catch only what someone anticipated, and they treat a "
     "business owner and a student the same way. They either block many good customers or miss fraud that stays "
     "below the limits. A behavioural engine compares every payment with that customer's own normal behaviour and "
     "with similar customers, and explains why. Rules remain useful: VeriShield combines both, and every rule "
     "change can be simulated against past decisions before it goes live.")
table([["Need", "Without a fraud engine", "With VeriShield"],
       ["Stop fraud before execution", "Detected days later from complaints", "Decision in milliseconds; HOLD with SMS confirmation"],
       ["Fewer false alarms", "Fixed limits for everyone", "Personal baselines, peer groups, tunable policy"],
       ["Explain decisions", "'The system blocked it'", "Reason codes and contributions per decision"],
       ["Investigate", "Spreadsheets, e-mail, shared folders", "Alerts, cases, evidence, SLA, network graph, timeline"],
       ["Report to the regulator", "STR written by hand", "STR drafted from the case, approved, exported (XML/PDF)"],
       ["Prove control", "Partial logs", "Hash-chained audit trail, versioned policies and models"]],
      [0.24, 0.33, 0.43])
callout("The engine does not replace the AML team. It does the repetitive screening of every transaction so that "
        "the team can spend its time on the few cases that deserve human judgement.")

# -------------------------------------------------------------------- 3. plug and play
chapter(3, "Plug and play", "How easy it is to plug in and play",
        "VeriShield is installed on one server inside the bank, connects to the channels the bank already has, and "
        "learns from history. It runs in shadow mode before it ever touches a customer payment.")
flow([("1. Install", "offline Windows bundle\nor Linux; one service\n(about 1 day)"),
      ("2. Connect", "pick a connector,\ntest, preview mapping\n(1-3 days per channel)"),
      ("3. Learn", "import history (CSV/DB),\nsegment customers,\ntrain the engine"),
      ("4. Shadow", "score live traffic,\nno customer impact,\ntune the policy"),
      ("5. Go live", "activate HOLD/DECLINE\nchannel by channel\nwith SMS confirmation")])
h2("What makes it plug-and-play")
bullets([
    "<b>Offline installer.</b> A single ZIP with the application, Python and every library as Windows wheels; no "
    "internet access is needed on the server. A Linux service works the same way.",
    "<b>Built-in database.</b> SQLite for a pilot, PostgreSQL for production - the schema is created and upgraded "
    "automatically at start-up.",
    "<b>Connectors, not projects.</b> REST push or pull, TCP stream, HTTP listener, CSV/JSONL file drop, database "
    "read replica, plus Kafka, RabbitMQ, JMS/IBM MQ and SFTP definitions. Each connector has a connection test, a "
    "field-mapping preview and a dead-letter queue for rejected records.",
    "<b>Smart field mapping.</b> Common column names are recognised automatically (for example <i>iban</i>, "
    "<i>beneficiary_iban</i>, <i>terminal_id</i>, <i>atm_id</i>, <i>merchant_id</i>, <i>originator_iban</i>); "
    "anything else is mapped once in the screen.",
    "<b>Cold start.</b> Customers without history are placed in a peer segment (k-means) so they are protected "
    "from day one.",
    "<b>Demo Mode.</b> A separate workspace with a payment-gateway simulator lets the team train and test with "
    "synthetic data, never mixed with live data.",
    "<b>Shadow and simulation.</b> New models and policy versions run in shadow or are replayed against stored "
    "decisions before activation.",
])
figure("it_channels", "IT Admin: live channels - start, stop, test and monitor each connection, including the "
                      "results database writer.")
figure("quality", "Data quality per channel: completeness of key fields, rejected and duplicate records.")

# -------------------------------------------------------------------- 4. integration
chapter(4, "Integration", "How it connects with the bank's other systems",
        "VeriShield sits beside the payment channels, not inside the core banking system. It receives transactions, "
        "returns decisions, and talks to the bank's identity, messaging and reporting systems through standard "
        "interfaces.")
diagram(integration_diagram(), t("VeriShield in the Bank of Palestine landscape.", "VeriShield in the bank's landscape."))
h2("Three ways to send transactions")
table([["Mode", "How", "When to use"],
       ["Pre-authorisation (synchronous)", "The channel calls <b>POST /api/v1/decisions/evaluate</b> before executing "
        "and waits for PASS / REVIEW / HOLD / DECLINE (milliseconds)", "Mobile and internet transfers, high-value "
        "payments - where the bank wants to stop fraud"],
       ["Event ingestion (asynchronous)", "<b>POST /api/v1/transactions/events</b>, stream or files; scored after the "
        "fact, alerts raised", "Cards, ATM, branch and incoming transfers; AML monitoring"],
       ["Pull / replica", "VeriShield polls a REST endpoint or reads a read-only database replica / CDC table",
        "Systems that cannot call out; the least effort for the core banking team"]],
      [0.24, 0.46, 0.30])
h2("What comes back to the bank")
bullets([
    "<b>Execution callback.</b> When an analyst or the customer releases or rejects a held payment, VeriShield calls "
    "the bank's execution adapter (outbound REST, signed) and records the result.",
    "<b>Results database.</b> Every decision can be written to a separate database for the data warehouse, BI and "
    "reconciliation.",
    "<b>Notifications.</b> E-mail through the bank's SMTP relay and SMS through its HTTP SMS gateway - each switched ON "
    "or OFF by IT, with secrets in the encrypted vault.",
    "<b>Identity.</b> Staff sign in with their Active Directory account; AD groups map to VeriShield roles.",
    "<b>Regulator.</b> STRs are exported as goAML-style XML and branded PDF.",
])
h2("Example: the pre-authorisation flow")
diagram(sequence_diagram(), "A held mobile transfer confirmed by the customer over SMS.")
code('POST /api/v1/decisions/evaluate          X-API-Key: <channel key>\n'
     '{ "transaction_id": "MB-2026-118204", "customer_id": "C00369", "type": "bank_transfer",\n'
     '  "amount": 9500.00, "currency": "ILS", "beneficiary_iban": "JO71...0564", "channel": "mobile_banking",\n'
     '  "device_id": "iPhone #4F21A9C0", "ip": "37.8.68.12", "timestamp": "2026-11-24T02:10:44Z" }\n\n'
     '200 OK\n'
     '{ "decision": "HOLD", "fraud_score": 0.82, "aml_signal": "MEDIUM", "action": "CONFIRM_WITH_CUSTOMER",\n'
     '  "reason_codes": ["NEW_BENEFICIARY", "UNUSUAL_BENEFICIARY_COUNTRY", "AMOUNT_ABOVE_PROFILE", "NEW_DEVICE"],\n'
     '  "decision_id": "DEC-...", "policy_version": "' + t('BOP-POLICY-01', 'POLICY-01') + ' v3", "latency_ms": 6.1 }')
para("If VeriShield cannot produce a reliable decision (for example during maintenance) the institution-approved "
     "fallback applies per transaction type - fail open, or hold - so the channels are never left waiting.", "small")

# -------------------------------------------------------------------- 5. database impact
chapter(5, "Database impact", "The impact on the database",
        "VeriShield has its own database. It never writes to the core banking database, and reads from it only if "
        "the bank chooses the read-replica connector.")
cards([("No change to core banking", "No new tables, triggers or stored procedures in the core system. Data reaches "
                                     "VeriShield through the API, a stream, files or a read-only replica.", PRI),
       ("Own database", "PostgreSQL in production (SQLite for pilots). The schema is created and migrated "
                        "automatically; indexes are part of the model.", BLUE),
       ("Read load", "The replica / CDC connector reads only new rows in small batches on a schedule the DBA "
                     "chooses; no reporting queries run against the core.", ACC)])
h2("Storage sizing")
para("Measured in Demo Mode (52,000 transactions): about <b>0.8 KB per transaction</b> including its indexes, and "
     "about <b>0.8 KB per decision</b> with reasons and policy references. Adding alerts, case links and audit, "
     "VeriShield plans conservatively with <b>3 KB per scored transaction</b> on PostgreSQL. Indicative sizes:")
kb = 3.0
rows = [["Daily volume", "Per day", "Per year", "With 13 months online retention"]]
for vol in (50_000, 200_000, 1_000_000):
    day = vol * kb / 1024
    rows.append([f"{vol:,} transactions", f"{day:,.0f} MB", f"{day * 365 / 1024:,.0f} GB",
                 f"{day * 395 / 1024:,.0f} GB (older data archived)"])
table(rows, [0.25, 0.2, 0.2, 0.35])
h2("Keeping it healthy")
bullets([
    "<b>Archiving.</b> Transactions older than the online retention period are moved to archive files by a scheduled "
    "job, with their decisions and case links preserved for the legal retention period.",
    "<b>Indexes</b> on customer, time, decision and reference columns keep dashboards responsive; the CEO overview "
    "over 30 days answers in well under a second on the demo volume.",
    "<b>Backups</b> follow the bank's standard PostgreSQL procedure; the hash-chained audit can be verified after a "
    "restore.",
    "<b>Results database.</b> A copy of every decision can be written to a separate database so that BI and the data "
    "warehouse never query the operational database.",
    "<b>Data protection.</b> IT roles see masked customer data; secrets are encrypted in a vault; Demo and Live "
    "data are kept in separate workspaces.",
])

# -------------------------------------------------------------------- 6. risks
chapter(6, "Risks", "The risks of adding it - and of not having it",
        "Every new control has risks. The table below lists the ones a risk committee should weigh, with the "
        "mitigation built into VeriShield or the rollout plan.")
h2("Risks of adding VeriShield")
table([["Risk", "Impact", "Mitigation"],
       ["False positives annoy good customers", "Complaints, abandoned payments", "Shadow mode first; personal baselines; "
        "SMS confirmation instead of hard blocks; policy simulation before every change"],
       ["Engine unavailable or slow", "Payments delayed", "Millisecond decisions; per-type fallback (fail-open or hold) "
        "approved by the bank; health checks and incidents"],
       ["Integration effort", "IT time", "Standard connectors and field mapping; start with one channel; read-replica "
        "option needs no core changes"],
       ["Data protection", "Exposure of customer data", "On-premises; RBAC with masking; MFA; encrypted vault; audit of "
        "every access; AI assistant OFF by default and masks identifiers"],
       ["Model drift / wrong tuning", "Missed fraud or more alerts", "Model health checks every 30 minutes, drift and "
        "retraining recommendation, champion vs challenger, versioned rollback"],
       ["Over-reliance on automation", "Weak human judgement", "Decisions are recommendations with reasons; dispositions "
        "and STRs always need a person; four-eyes on STR approval"],
       ["Key-person dependency", "Support continuity", "Full source code, documentation, automated tests, offline "
        "installer and runbooks handed over to " + BANK]],
      [0.25, 0.2, 0.55])
h2("Risks of not having it")
table([["Risk", "What happens"],
       ["Direct fraud losses", "Account takeover, scams and mule transfers are discovered after the money has left."],
       ["Regulatory exposure", "Weak monitoring, late or missing STRs, no evidence of control during inspections."],
       ["Reputation", "Customers and media remember the bank that did not notice; trust in digital channels falls."],
       ["Operational overload", "Manual review of reports and spreadsheets does not scale with digital volumes."],
       ["Blind spots", "No view of networks of linked accounts, devices and IP addresses across customers."],
       ["No learning", "Investigation outcomes are not fed back, so the same patterns are missed again."]],
      [0.25, 0.75])
callout("The decisive comparison is not 'risk versus no risk' but controlled, measurable risk with VeriShield "
        "versus unmeasured risk without it.")

# -------------------------------------------------------------------- 7. transaction kinds
chapter(7, "Coverage", "Kinds of transactions the solution covers",
        "VeriShield scores seven transaction types, each with its own features, limits and typical fraud patterns, "
        "plus digital-banking login events that feed the payment decision.")
table([["Type", "Channels", "What the engine profiles", "Typical fraud / AML patterns"],
       ["Bank transfer", "mobile, internet, branch, core", "amount, payee account and country, connection, IP, hour",
        "account takeover, scam to new payee, mule chains, structuring"],
       ["Card payment", "POS, e-commerce", "amount, merchant, MCC, merchant country, entry mode, hour",
        "card testing, foreign e-commerce spree"],
       ["Cash withdrawal", "ATM, branch", "amount, terminal, location, hour", "night-time ATM cash-out after compromise"],
       ["Cash deposit", "ATM, branch", "amount, terminal, location, hour", "structuring below reporting thresholds"],
       ["Incoming transfer", "SWIFT / local clearing", "amount, originator account and country, hour",
        "mule pass-through, many new originators"],
       ["Phone recharge", "mobile, internet", "amount, phone number, operator, connection", "recharge bursts after takeover"],
       ["Prepaid card", "mobile, internet", "amount, card, connection", "prepaid card drain"]],
      [0.16, 0.19, 0.33, 0.32])
h2("AML indicators and signals")
table([["Indicator", "Meaning"],
       ["Structuring", "Several amounts just under the reporting threshold within a short period"],
       ["Velocity / turnover above profile", "Activity far above the declared expected monthly turnover"],
       ["Rapid pass-through", "Money received from new senders and sent on within hours, often abroad"],
       ["Many originators", "An individual suddenly receiving from many new senders"],
       ["Shared counterparty", "Unrelated customers paying the same account (mule network)"],
       ["High-risk jurisdiction", "Payee or originator in a country on the bank's list"],
       ["Sanctions / PEP match", "Customer or payee matches OFAC, UN, EU or local lists (fuzzy, Arabic-aware)"],
       ["Watch list", "Account, device, IP or customer flagged by the bank"],
       ["Login risk", "New device, new country, impossible travel, failed logins, recent password reset"]],
      [0.3, 0.7])
figure("transactions", "All transaction types in one list, with decision, score and channel.")

# -------------------------------------------------------------------- 8. roles & workflow
chapter(8, "Roles and workflow", "Role management and business workflow",
        "Five roles separate who configures the system, who investigates, who decides and who oversees. "
        "Permissions are enforced by the API, so the same rules apply to the screens, integrations and AI agents.")
table([["Role", "Responsibilities", "Cannot"],
       ["IT Admin", "Users and roles, Active Directory, channels and connectors, e-mail/SMS, models (train, shadow, "
        "activate), AI-agent access, Demo Mode, system health", "See unmasked customer data; decide cases"],
       ["AML Manager", "Alert triage, create and assign cases, dispositions, policies and thresholds, screening and watch "
        "list, STR preparation and approval, KYC reviews, report branding", "Change users or infrastructure"],
       ["AML Employee (analyst)", "Work assigned cases: checklist, notes, evidence, recommendation; prepare STR drafts; "
        "complete assigned reviews", "See cases not assigned to them; approve"],
       ["Chief AML", "Oversight of all alerts and cases, audit trail, executive and AML reports, STR approval",
        "Edit policies (unless granted by IT, audited)"],
       ["CEO", "Institution-wide overview: value protected, heat map, what-if, board reports", "Change anything"]],
      [0.17, 0.58, 0.25])
h2("A real example, end to end")
diagram(swimlane_diagram(), "From a suspicious transfer to an STR - who does what.")
para("The screens below follow one case through the system in Demo Mode.")
h3("1. The payment is held and the alert raised")
para("A customer's transfer to an account that has also received money from several unrelated customers is held. "
     "The alert appears in the AML Manager's queue with severity, score and reasons; similar alerts are grouped.")
figure("alerts", "Alert queue of the AML Manager.")
h3("2. The manager opens a case and assigns an analyst")
figure("case_create", "Creating a case from the alert: purpose, questions for the analyst, assignee and due date.")
h3("3. The analyst investigates")
para("The analyst sees only assigned cases and customers. A checklist guides the work; notes, calls and evidence "
     "are recorded with time and author; the recommendation goes back to the manager.")
figure("case_analyst", "The analyst's case file: checklist, notes, evidence and recommendation.")
h3("4. Network and timeline show the bigger picture")
figure("network_case", "Network graph from the case: the customer (centre) and four other customers who paid the same "
                       "beneficiary account (red links) - the footprint of a mule account. Shops, ATMs and public hubs "
                       "are left out.", crop=(0.44, 0.27, 0.84, 0.63), max_h=100 * mm, width=CW * 0.8)
figure("timeline", "Customer 360 timeline: transactions, alerts, cases, confirmations, logins and screening on one line.")
h3("5. Decision and suspicious transaction report")
para("The manager reviews the recommendation, records the disposition and prepares the STR. The narrative is drafted "
     "from the case facts (or by the optional AI assistant), edited by a person, approved, and exported for the "
     + t("Financial Follow-Up Unit.", "financial intelligence unit."))
figure("case_summary", "Case summary drafted from the facts of the case (or by the optional AI assistant).",
       crop=(0.0, 0.0, 1.0, 1.0))
figure("disposition", "The AML Manager records the disposition with a rationale; the decision is audited.")
figure("str", "STR draft generated from the case: indicators, narrative, subjects and transactions; XML and PDF export.",
       crop=(0.47, 0, 1, 1), max_h=125 * mm, width=CW * 0.75)
h3("6. The system learns")
para("The outcome labels the transactions. The payee is on the watch list, the confirmed fraud trains the learned "
     "model, and the next payment to that account is held immediately.")

# -------------------------------------------------------------------- 9. solution design
chapter(9, "Solution design", "Solution design, end to end",
        "One application, organised in clear layers: channels feed ingestion; the decision layer combines the engine "
        "with AML, screening and policy; actions go back to the bank; people investigate; management sees results; "
        "governance runs through everything.")
diagram(architecture_diagram(), "Logical architecture.")
h2("Technology")
table([["Area", "Choice", "Why"],
       ["Language & API", "Python 3.12, FastAPI, OpenAPI documentation at /api/docs", "Fast, typed, widely known"],
       ["Engine", "BankSealer (NumPy): HBOS, temporal thresholds, DBSCAN, CBLOF; logistic regression for learning",
        "Explainable, no GPU, millisecond scoring"],
       ["Data", "SQLAlchemy 2; PostgreSQL (production) or SQLite (pilot)", "Standard, portable, bank-friendly"],
       ["User interface", "Single-page web app, no external CDN, works offline on the bank network", "No client install"],
       ["Reports", "ReportLab PDF, Excel and CSV with the bank's branding", "Formal documents for board and regulator"],
       ["Security", "TLS, scrypt password hashing, TOTP MFA, lockout, AD/LDAP, encrypted vault, hash-chained audit",
        "Defence in depth"],
       ["Operations", "Windows service or Linux systemd; offline installer; health, readiness and metrics endpoints",
        "Fits the bank's data centre"]],
      [0.17, 0.53, 0.30])
h2("Deployment")
diagram(deployment_diagram(), "Recommended production deployment inside the bank network.")
h2("Decision path inside the platform")
flow([("Receive", "API / stream / file\nmap & validate\nidempotency"), ("Enrich", "customer, segment,\nlogin & device risk,\nwatch list"),
      ("Score", "BankSealer profiles\n+ learned model\n+ AML indicators"), ("Decide", "policy version:\nthresholds, rules,\nactions"),
      ("Act", "respond, SMS, alert,\nauto-case, callback,\nresults DB")])

# -------------------------------------------------------------------- 10. database design
chapter(10, "Database design", "Database design",
        "The data model has 34 tables in seven areas. Every business table carries the tenant (institution) and the "
        "workspace (LIVE or DEMO), so live and demo data never mix and several institutions can share one "
        "installation if needed.")
diagram(erd_diagram(), "Core entities and their relationships (simplified).")
h2("Tables by area")
table([["Area", "Tables", "Purpose"],
       ["Identity & access", "tenants, users, user_sessions, api_clients, secrets", "Institutions, staff, sessions, "
        "channel keys, encrypted credentials"],
       ["Channels", "connectors, connector_logs, dead_letters", "Connection settings, activity log, rejected records"],
       ["Scoring", "customers, customer_segments, model_versions, transactions, decisions, execution_events",
        "Profiles, peer segments, model registry, every transaction and its decision and execution result"],
       ["Investigation", "alerts, cases, case_links, case_events, case_notes, evidence", "Queue, case file, state "
        "history, notes and files with SHA-256"],
       ["Compliance", "sanction_entries, screening_matches, watchlist, str_reports, customer_reviews, "
        "customer_confirmations, login_events", "Lists and matches, watch list, STRs, KYC reviews, SMS confirmations, "
        "login risk"],
       ["Governance", "policy_versions, audit_events, incidents, counters", "Versioned policies, tamper-evident audit, "
        "operational incidents, reference numbers"],
       ["Reporting & messaging", "report_jobs, report_schedules, notification_log", "Generated reports, schedules, "
        "e-mail/SMS outbox"]],
      [0.19, 0.42, 0.39])
h2("Design principles")
bullets([
    "<b>Decision is separate from transaction.</b> A transaction can be re-scored by a new model or replayed by a "
    "policy simulation without losing the original decision.",
    "<b>Append-only history.</b> Case events, execution events and audit events are never updated; the audit chain "
    "links every event to the previous hash so tampering is detectable.",
    "<b>Explainability is stored.</b> Reason codes, contributions and the policy and model versions are saved with "
    "each decision.",
    "<b>Flexible details.</b> Channel-specific fields (merchant, terminal, device, location) are kept in a JSON "
    "column, so new channels do not need schema changes.",
    "<b>Automatic migration.</b> New tables, columns and indexes are added at start-up; no manual SQL for upgrades.",
])

# -------------------------------------------------------------------- 11. MCP
chapter(11, "MCP and AI agents", "MCP: how VeriShield works with AI agents",
        "The Model Context Protocol (MCP) is an open standard that lets AI assistants such as Claude call tools in "
        "other systems. VeriShield ships an MCP server so that an approved agent can answer questions about the "
        "platform - under the same permissions, masking and audit as a person.")
diagram(mcp_diagram(), "How an agent reaches VeriShield.")
h2("Controls")
bullets([
    "<b>OFF by default.</b> The IT Admin switches agent access ON, chooses which tools are allowed and sets a rate "
    "limit.",
    "<b>Access keys with scopes.</b> Each agent gets its own key, bound to the Live or Demo workspace, and can be "
    "revoked instantly.",
    "<b>Read-oriented tools.</b> There is no SQL access, no secret retrieval and no tool to block or release a "
    "payment. Customer data is returned masked.",
    "<b>Every call audited</b> with the agent, tool and outcome, visible on the AI Agents page.",
])
h2("Available tools")
table([["Tool", "What the agent gets"],
       ["get_dashboard, get_system_health", "Workspace figures, latency percentiles, incidents"],
       ["get_aml_queue_summary, get_case_summary", "Open alerts and cases with status, priority and SLA"],
       ["search_transactions", "Masked transaction search"],
       ["get_model_status, get_model_health, get_clusters", "Active and shadow models, drift, retraining advice, segments"],
       ["list_connectors, get_connector_health, get_ingestion_failures", "Channel status and rejected records"],
       ["preview_policy, simulate_policy", "Read the active policy; replay a draft without side effects"],
       ["generate_report", "Create a permitted report (for example channel health or model health)"]],
      [0.42, 0.58])
h2("What it looks like in practice")
code("AML Manager > Which open cases are past their SLA, and what do they have in common?\n"
     "Agent       > calls get_case_summary, get_aml_queue_summary\n"
     "            > 3 cases are past due. Two involve transfers to the same foreign account and were\n"
     "              raised by SHARED_COUNTERPARTY; one is a structuring pattern on cash deposits...\n\n"
     "IT Admin    > Is the card channel healthy? Should we retrain?\n"
     "Agent       > calls get_connector_health, get_model_health\n"
     "            > The card feed is streaming (0.3% rejected, missing MCC). Score drift PSI 0.04:\n"
     "              no retraining needed yet.")
para("Inside VeriShield, a separate optional <b>AI assistant</b> drafts case summaries and STR narratives with "
     "Claude. It is OFF by default, masks names and account numbers before sending, can point to the bank's approved "
     "gateway, and every draft is reviewed by an analyst. Without it, VeriShield writes the narrative from the case "
     "facts itself.")
figure("mcp", "AI Agents page: switch, allowed tools, access keys, connection snippets and agent activity.")

# -------------------------------------------------------------------- gallery
chapter(12, "Screens", "More of the system",
        "A selection of further screens from the Demo workspace.")
figure("ceo", "CEO overview: value protected, intervention rate, risk heat map by city, money saved and what-if.")
figure("screening", "Screening and watch list: sanctions/PEP matches to resolve, list imports and name check.")
figure("clusters", "Customer clusters: peer groups found by the engine, with their profiles and risk mix.")
figure("models", "Model health and retraining recommendations for the IT Admin.")
figure("challenger", "Champion vs challenger: compare a new model with the active one on the same transactions.")
figure("policies", "Versioned decision policies with thresholds, rules and actions; drafts are simulated before approval.")
figure("reviews", "Periodic KYC reviews scheduled from the risk rating, with checklist and assignee.")
figure("reports", "Reports: formal, bank-branded PDF and Excel reports, on demand or on a schedule.")
figure("users", "Users and roles, with Active Directory sign-in and MFA.")

# -------------------------------------------------------------------- about the developer
story.append(PageBreak())
story.append(P("ABOUT THE DEVELOPER", "eyebrow"))
p_ = P(f"Developed by {AUTHOR}", "h1")
p_._toc = (0, "About the developer")
story.append(p_)
about = Table([[
    Table([[P("AK", "mono_c")]], colWidths=[30 * mm], rowHeights=[30 * mm],
          style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), SOFT), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEBELOW", (0, 0), (-1, -1), 3, PRI)])),
    [P(f"<b>{AUTHOR}</b>", "h2"),
     P("Software engineer and solution architect - designer and developer of VeriShield.", "lead")],
]], colWidths=[36 * mm, CW - 36 * mm])
about.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
story.append(about)
story.append(Spacer(1, 8))
para(f"{AUTHOR} conceived, designed and built VeriShield end to end: the implementation of the BankSealer "
     "behavioural engine, the decision and policy layers, AML indicators and sanctions screening, the case "
     "management and STR workflow, the integrations with channels, Active Directory, e-mail, SMS and AI agents, the "
     "web interface for five roles, the reporting, the offline Windows installer and the documentation and test "
     "suite that come with it.")
para("The work starts from a practical conviction: banks in the region deserve fraud and AML tooling "
     "that is as modern as the threats they face, that runs inside their own data centre, explains every decision "
     "in language an investigator and a customer can understand, and can be adopted step by step without replacing "
     "the systems they already have.")
para(f"VeriShield reflects that approach: research-grade detection made explainable, operational discipline "
     f"(shadow mode, simulation, audit) built in from day one, and a delivery model that does not depend on internet "
     f"access or a large integration project. {AUTHOR} remains the technical owner of the platform and supports "
     + t("Bank of Palestine through the pilot, the go-live and the extension to further channels.",
         "institutions through the pilot, the go-live and the extension to further channels."))
cards([("Detection engineering", "Behavioural profiling, outlier detection, peer-group analysis, explainable "
                                 "scoring and learning from investigation outcomes.", PRI),
       ("Banking platforms", "Real-time decision APIs, channel integration, AML case management, regulatory "
                             "reporting and role-based security.", BLUE),
       ("Delivery", "On-premises and offline deployment, automated tests, documentation and hands-on support "
                    "for operations teams.", ACC)])
story.append(Spacer(1, 10))
callout(t("Contact the developer to plan the VeriShield pilot at Bank of Palestine.",
          "Contact the developer to arrange a demonstration or a pilot of VeriShield at your institution."))

# =============================================================================== build
doc = Book(OUT)
doc.multiBuild(story)
print("written", OUT)
