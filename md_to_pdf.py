"""
SPDX-License-Identifier: FLIP-2.0
Copyright (c) 2026 Stefan Nolde (see below)
Contact: snoldemob@gmail.com
License: https://github.com/snolde/flip/blob/v2.0/LICENSE
AI Training: opt-out

This code was developed with generative AI assistance. The human author selected,
arranged, and modified outputs. Outputs have been scanned for license conflicts;
any unresolved matches are unintentional.
See attached NOTICE for details
Use at your own risk.

MDToPDFConverter — converts Markdown files to PDF using fpdf2 + markdown-it-py.

Target environment: PyDroid 3 on Android (pure Python, no subprocess/shell, no tkinter).
Future migration: Keep this class free of Android-specific deps so it can be embedded
via Chaquopy or rewritten in Java (commonmark-java + iText).

Design decisions:
- All sizing derived from available_width so page/margin config flows through cleanly.
- markdown-it-py tokenises the MD; we walk the flat token list with a state machine.
- fpdf2 is used for PDF generation (supports Unicode via built-in core fonts or TTF).
- Images are resolved through a priority chain; missing images follow on_missing_image policy.
- Mermaid diagrams are fetched as PNG via mermaid.ink; offline falls back to policy.
- No hardcoded paths anywhere in the class — all come from config or md_path at call time.
- Syntax highlighting via Pygments (optional; degrades gracefully if not installed).
- Custom monospaced TTF font for code blocks via style.code_font_path.
"""

import os
import re
import textwrap
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Third-party (must be installed in the environment)
# ---------------------------------------------------------------------------
from fpdf import FPDF
from markdown_it import MarkdownIt

# Pygments is optional.  HIGHLIGHT_THEME is built inside the try block so that
# Token.* references never execute when Pygments is absent — avoiding an
# import-time NameError.
try:
    from pygments import lex
    from pygments.lexers import get_lexer_by_name
    from pygments.util import ClassNotFound
    from pygments.token import Token

    _PYGMENTS = True

    # Syntax highlight colour map: Token type → (r, g, b).
    # _highlight_color() walks the token MRO so only the root types are needed;
    # the leaf entries below are shortcuts for the most common cases.
    HIGHLIGHT_THEME = {
        Token.Keyword:              (0,   96,  160),  # blue
        Token.Keyword.Constant:     (0,   96,  160),
        Token.Keyword.Declaration:  (0,   96,  160),
        Token.Keyword.Namespace:    (0,   96,  160),
        Token.Name.Builtin:         (100, 0,   160),  # purple
        Token.Name.Function:        (0,   128, 96),   # teal
        Token.Name.Class:           (0,   128, 96),
        Token.Name.Decorator:       (128, 64,  0),    # brown
        Token.Literal.String:       (160, 32,  32),   # dark red
        Token.Literal.String.Doc:   (160, 32,  32),
        Token.Literal.Number:       (0,   128, 0),    # green
        Token.Comment:              (128, 128, 128),  # grey
        Token.Operator:             (80,  80,  80),
        Token.Punctuation:          (60,  60,  60),
        Token.Error:                (200, 0,   0),    # red
    }

    def _highlight_color(ttype):
        """Walk the Pygments token MRO until we find a match in HIGHLIGHT_THEME."""
        t = ttype
        while t:
            if t in HIGHLIGHT_THEME:
                return HIGHLIGHT_THEME[t]
            t = t.parent if hasattr(t, "parent") else None
        return (30, 30, 30)  # default near-black

except ImportError:
    _PYGMENTS = False
    HIGHLIGHT_THEME = {}

    def _highlight_color(ttype):
        return (30, 30, 30)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PAGE_SIZES = {
    "A4": (210, 297),
    "A5": (148, 210),
}

MARGIN_PRESETS = {
    "small":  10,
    "normal": 20,
    "wide":   30,
}

# Heading scale factors relative to body font size
HEADING_SCALES = {1: 2.0, 2: 1.6, 3: 1.35, 4: 1.15, 5: 1.0, 6: 0.9}

MIN_CODE_FONT_SIZE = 6    # pt — below this we wrap instead of shrinking further
CODE_CONTINUATION  = "\u21b5"  # ↵  appended to wrapped code lines
BLOCKQUOTE_BAR_W   = 1.5  # mm — left accent bar for blockquotes
TABLE_PADDING      = 2    # mm — horizontal cell padding inside tables


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class MDToPDFConverter:
    """
    Convert a Markdown file to a PDF.

    Config keys (all optional — sensible defaults shown):
        page.size             "A4" | "A5"                        default "A4"
        page.margins          "small"(10mm)|"normal"(20mm)|"wide"(30mm)  default "normal"
        image_search_paths    list[str]                           default []
        output_dir            str                                 default same dir as .md
        on_missing_image      "prompt" | "skip" | "placeholder"  default "placeholder"
        style.font_size       int                                 default 11
        style.code_font_size  int                                 default 9
        style.heading_color   (r, g, b)                          default (0, 0, 0)
        style.code_bg_color   (r, g, b)                          default (240, 240, 240)
        style.body_font_path  str  path to a .ttf for body text  default None → Helvetica
        style.code_font_path  str  path to a monospaced .ttf     default None → auto-detect then Courier
        style.syntax_highlight bool                               default True
        mermaid_ink_url       str                                 default "https://mermaid.ink/img/"
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, config: dict):
        self.config = config

        # Page geometry
        page_cfg = config.get("page", {})
        size_name = page_cfg.get("size", "A4").upper()
        self.page_w, self.page_h = PAGE_SIZES.get(size_name, PAGE_SIZES["A4"])
        margin_name = page_cfg.get("margins", "normal").lower()
        self.margin = MARGIN_PRESETS.get(margin_name, 20)
        self.available_width = self.page_w - 2 * self.margin

        # Style
        style = config.get("style", {})
        self.font_size        = style.get("font_size", 11)
        self.hf_font_size   = style.get("hf_font_size",9)
        self.code_font_size   = style.get("code_font_size", 9)
        self.heading_color    = style.get("heading_color", (0, 0, 0))
        self.code_bg_color    = style.get("code_bg_color", (240, 240, 240))
        self.body_font_path   = style.get("body_font_path", None)
        self.code_font_path   = style.get("code_font_path", None)
        self.syntax_highlight = style.get("syntax_highlight", True)

        # Resolved at the start of each _build_pdf() call; fallbacks are the
        # PDF core fonts which require no embedding but are Latin-1 only.
        self._body_font_name = "Helvetica"
        self._code_font_name = "Courier"

        # Behaviour
        self.image_search_paths = config.get("image_search_paths", [])
        self.on_missing_image   = config.get("on_missing_image", "placeholder")
        self.mermaid_ink_url    = config.get("mermaid_ink_url", "https://mermaid.ink/img/")

        # markdown-it parser with tables + strikethrough enabled
        self._md = (
            MarkdownIt("commonmark")
            .enable("table")
            .enable("strikethrough")
        )

    # ------------------------------------------------------------------
    # Font helpers
    # ------------------------------------------------------------------

    # Preferred monospaced TTFs to probe on Android (and Linux desktop).
    # Ordered by preference; first found wins.
    _MONO_CANDIDATES = [
        "/system/fonts/DroidSansMono.ttf",        # Android 4+ universal
        "/system/fonts/CutiveMono.ttf",           # present on this device (decorative fallback)
        "/system/fonts/NotoMono-Regular.ttf",
        "/system/fonts/RobotoMono-Regular.ttf",
        "/system/fonts/CutiveMono-Regular.ttf",
        "/system/fonts/SourceCodePro-Regular.ttf",
        # Desktop fallbacks for testing outside Android
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ]

    _BODY_CANDIDATES = [
        "/system/fonts/Roboto-Regular.ttf",       # present on this device, full family
        "/system/fonts/SourceSansPro-Regular.ttf",# also present on this device
        "/system/fonts/DroidSans.ttf",
        "/system/fonts/NotoSans-Regular.ttf",
        # Desktop fallbacks for testing outside Android
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]

    @staticmethod
    def _find_font(explicit_path: str | None, candidates: list[str],
                   label: str) -> str | None:
        """
        Resolve a font path:
          1. Use explicit_path if provided and the file exists.
          2. Probe candidates in order, return the first found.
          3. Return None (caller falls back to core font).
        Prints a single info line on success, a warning on total failure.
        """
        if explicit_path:
            if Path(explicit_path).exists():
                return explicit_path
            print(f"[WARN] {label} font not found at {explicit_path} — auto-detecting.")
        for path in candidates:
            if Path(path).exists():
                print(f"[INFO] {label} font: {path}")
                return path
        print(f"[WARN] No {label} TTF found — falling back to core PDF font.")
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def convert(self, md_path: str) -> str | None:
        """
        Convert *md_path* to PDF.
        Returns the output path on success, None on failure.
        Prints a confirmation or error message either way.
        """
        md_path = Path(md_path)
        if not md_path.exists():
            print(f"[ERROR] File not found: {md_path}")
            return None

        try:
            md_text = md_path.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"[ERROR] Cannot read {md_path}: {exc}")
            return None

        out_dir = self.config.get("output_dir")
        if out_dir:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
        else:
            out_dir = md_path.parent

        out_path = out_dir / (md_path.stem + ".pdf")

        try:
            tokens = self._md.parse(md_text)
            self._build_pdf(tokens, md_path, out_path)
            print(f"[OK] PDF written to: {out_path}")
            return str(out_path)
        except Exception as exc:
            import traceback
            print(f"[ERROR] Conversion failed: {exc}")
            traceback.print_exc()
            return None

    # ------------------------------------------------------------------
    # PDF construction
    # ------------------------------------------------------------------

    def _build_pdf(self, tokens: list, md_path: Path, out_path: Path):
        """Walk the markdown-it token list and render each element."""

        md_dir  = md_path.parent
        out_dir = out_path.parent

        # Extract title from first H1, fall back to filename stem
        doc_title = md_path.stem
        for i, tok in enumerate(tokens):
            if tok.type == "heading_open" and tok.tag == "h1":
                if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                    doc_title = tokens[i + 1].content
                break

        # Build FPDF instance
        # Resolve font paths first so _body_font_name/_code_font_name are final
        # before _DocumentPDF is constructed. header() fires on the first
        # add_page() call and must use the resolved TTF name, not "Helvetica".
        self._body_font_name = "Helvetica"
        self._code_font_name = "Courier"
        body_path = self._find_font(self.body_font_path, self._BODY_CANDIDATES, "body")
        if body_path:
            self._body_font_name = "BodyFont"
        code_path = self._find_font(self.code_font_path, self._MONO_CANDIDATES, "code")
        if code_path:
            self._code_font_name = "CodeFont"

        # Now construct with the resolved name so header()/footer() use it
        pdf = _DocumentPDF(
            doc_title=doc_title,
            margin=self.margin,
            font_size=self.font_size,
            hf_font_size=self.hf_font_size,
            heading_color=self.heading_color,
            body_font_name=self._body_font_name,
            orientation="P",
            unit="mm",
            format=(self.page_w, self.page_h),
        )

        # Register TTFs on the pdf instance before add_page()
        if body_path:
            try:
                p = Path(body_path)
                stem = p.stem
                folder = p.parent

                def _variant(suffixes):
                    base = stem
                    for word in ("-Regular", "-Light", "-Medium", "-Thin"):
                        base = base.replace(word, "")
                    for suffix in suffixes:
                        candidate = folder / (base + suffix + ".ttf")
                        if candidate.exists():
                            return str(candidate)
                    return body_path

                pdf.add_font("BodyFont",             fname=body_path, uni=True)
                pdf.add_font("BodyFont", style="B",  fname=_variant(["-Bold"]), uni=True)
                pdf.add_font("BodyFont", style="I",  fname=_variant(["-Italic"]), uni=True)
                pdf.add_font("BodyFont", style="BI", fname=_variant(["-BoldItalic"]), uni=True)
            except Exception as exc:
                print(f"[WARN] Could not register body font: {exc} — using Helvetica.")
                self._body_font_name = "Helvetica"

        if code_path:
            try:
                pdf.add_font("CodeFont", fname=code_path, uni=True)
            except Exception as exc:
                print(f"[WARN] Could not register code font: {exc} — using Courier.")
                self._code_font_name = "Courier"

        pdf.set_auto_page_break(auto=True, margin=self.margin + 8)
        pdf.add_page()
        pdf.set_margins(self.margin, self.margin, self.margin)

        # Token walker state
        i = 0
        list_stack       = []   # stack of ("bullet"|"ordered", counter)
        blockquote_depth = 0
        in_table         = False
        table_rows       = []
        table_alignments = []

        while i < len(tokens):
            tok = tokens[i]

            # ── Headings ────────────────────────────────────────────────
            if tok.type == "heading_open":
                level  = int(tok.tag[1])
                text   = self._inline_text(tokens[i + 1].children or [])
                h_size = round(self.font_size * HEADING_SCALES.get(level, 1.0))
                line_h = h_size * 0.4 + 2

                # Orphan control: move to new page if < 2 heading-lines remain
                if pdf.get_y() + line_h * 2 > self.page_h - self.margin - 8:
                    pdf.add_page()

                r, g, b = self.heading_color
                pdf.set_font(self._body_font_name, style="B", size=h_size)
                pdf.set_text_color(r, g, b)
                pdf.ln(3 if level > 1 else 5)
                pdf.multi_cell(self.available_width, line_h, text, align="L")
                pdf.ln(2)
                pdf.set_text_color(0, 0, 0)
                i += 3  # heading_open + inline + heading_close
                continue

            # ── Paragraphs ───────────────────────────────────────────────
            if tok.type == "paragraph_open":
                inline = tokens[i + 1]
                indent = len(list_stack) * 6 if list_stack else blockquote_depth * 8

                # Draw list marker before the paragraph text when inside a list.
                # The marker sits in the indent gutter to the left of the text.
                # Bullet levels use •, ◦, ▸ (cycling for deep nesting).
                # Ordered items use the current counter from list_stack.
                if list_stack:
                    kind, count = list_stack[-1]
                    line_h = self.font_size * 0.4 + 1
                    depth  = len(list_stack)          # 1-based nesting depth
                    marker_x = self.margin + (depth - 1) * 6  # left edge of gutter
                    text_x   = self.margin + depth * 6        # where text starts

                    if kind == "bullet":
                        # Unicode bullet markers — safe when a TTF body font is loaded.
                        # Falls back gracefully to ASCII if Helvetica core font is used,
                        # but that case should not occur on Android with Roboto available.
                        # Only U+2022 • is reliably present in Roboto and DroidSans.
                        # Deeper levels use ASCII so no glyph-missing warnings occur.
                        markers = ["•", "*", "+"]
                        marker  = markers[(depth - 1) % len(markers)]
                    else:
                        marker = f"{count}."

                    pdf.set_font(self._body_font_name, size=self.font_size)
                    pdf.set_xy(marker_x, pdf.get_y())
                    pdf.cell(text_x - marker_x, line_h, marker, align="R")
                    pdf.set_x(text_x)

                self._render_inline(pdf, inline.children or [], md_dir, indent, blockquote_depth > 0)
                pdf.ln(self.font_size * 0.4 +1)
                #pdf.ln(3)
                i += 3
                continue

            # ── Lists ────────────────────────────────────────────────────
            if tok.type == "bullet_list_open":
                list_stack.append(("bullet", 0))
                i += 1; continue
            if tok.type == "ordered_list_open":
                list_stack.append(("ordered", 0))
                i += 1; continue
            if tok.type in ("bullet_list_close", "ordered_list_close"):
                if list_stack: list_stack.pop()
                i += 1; continue
            if tok.type == "list_item_open":
                if list_stack:
                    kind, count = list_stack[-1]
                    list_stack[-1] = (kind, count + 1)
                i += 1; continue
            if tok.type == "list_item_close":
                i += 1; continue

            # ── Blockquotes ──────────────────────────────────────────────
            if tok.type == "blockquote_open":
                blockquote_depth += 1
                i += 1; continue
            if tok.type == "blockquote_close":
                blockquote_depth = max(0, blockquote_depth - 1)
                i += 1; continue

            # ── Fenced code blocks (including mermaid) ───────────────────
            if tok.type == "fence":
                lang = (tok.info or "").strip().lower()
                code = tok.content.rstrip("\n")
                if lang == "mermaid":
                    img_path = self._render_mermaid(code, out_dir)
                    if img_path:
                        self._embed_image(pdf, img_path, label=None)
                        try: os.remove(img_path)
                        except OSError: pass
                    else:
                        self._draw_placeholder(pdf, "Mermaid diagram (offline)")
                else:
                    self._render_code_block(pdf, code, language=lang)
                pdf.ln(3)
                i += 1; continue

            # ── Indented code blocks ─────────────────────────────────────
            if tok.type == "code_block":
                self._render_code_block(pdf, tok.content.rstrip("\n"))
                pdf.ln(3)
                i += 1; continue

            # ── Shortcodes via HTML comments ─────────────────────────────
            # <!--pb-->      : page break
            # <!--b N-->     : blank vertical space of N mm (default 10)
            if tok.type in ("html_block", "html_inline"):
                raw = tok.content.strip()
                if re.search(r'<!--\s*pb\s*-->', raw):
                    pdf.add_page()
                else:
                    m = re.search(r'<!--\s*b\s*(\d+(?:\.\d+)?)\s*-->', raw)
                    if m:
                        pdf.ln(float(m.group(1)))
                    # Ignore all other HTML — we don't render arbitrary HTML
                i += 1; continue

            # ── Horizontal rules ─────────────────────────────────────────
            if tok.type == "hr":
                pdf.set_draw_color(180, 180, 180)
                pdf.set_line_width(0.3)
                y = pdf.get_y() + 2
                pdf.line(self.margin, y, self.margin + self.available_width, y)
                pdf.set_line_width(0.2)
                pdf.set_draw_color(0, 0, 0)
                pdf.ln(5)
                i += 1; continue

            # ── Tables ───────────────────────────────────────────────────
            if tok.type == "table_open":
                table_rows, table_alignments, in_table = [], [], True
                i += 1; continue
            if tok.type == "table_close":
                in_table = False
                self._render_table(pdf, table_rows, table_alignments)
                pdf.ln(3)
                table_rows = []
                i += 1; continue
            if tok.type in ("thead_open", "thead_close", "tbody_open", "tbody_close"):
                i += 1; continue
            if tok.type == "tr_open":
                table_rows.append([])
                i += 1; continue
            if tok.type == "tr_close":
                i += 1; continue
            if tok.type in ("th_open", "td_open"):
                style_attr = (tok.attrs or {}).get("style", "")
                if "right" in style_attr:   align = "R"
                elif "center" in style_attr: align = "C"
                else:                        align = "L"
                if len(table_alignments) < len(table_rows[-1]) + 1:
                    table_alignments.append(align)
                i += 1; continue
            if tok.type in ("th_close", "td_close"):
                i += 1; continue
            if tok.type == "inline" and in_table:
                if table_rows:
                    table_rows[-1].append(self._inline_text(tok.children or []))
                i += 1; continue

            # ── Stray inline (e.g. standalone image paragraph) ───────────
            if tok.type == "inline":
                self._render_inline(pdf, tok.children or [], md_dir, 0, False)
                pdf.ln(2)
                i += 1; continue

            i += 1

        pdf.output(str(out_path))

    # ------------------------------------------------------------------
    # Inline rendering
    # ------------------------------------------------------------------

    def _inline_text(self, children: list) -> str:
        """Plain-text extraction from inline token children (no formatting)."""
        parts = []
        for child in children:
            if child.type in ("text", "code_inline"):
                parts.append(child.content)
            elif child.type in ("softbreak", "hardbreak"):
                parts.append(" ")
        return "".join(parts)

    def _collect_runs(self, children: list) -> list[dict]:
        """
        Convert inline token children into a flat list of run dicts:
          {"type": "text",  "text": str, "style": ""|"B"|"I"|"BI",
           "code": bool, "strike": bool, "link": bool, "href": str}
          {"type": "break"}
          {"type": "image", "src": str, "alt": str}
        """
        runs        = []
        style_stack = []
        in_link     = False
        href        = ""
        in_strike   = False

        for tok in children:
            t = tok.type
            if t == "strong_open":
                style_stack.append("B")
            elif t == "strong_close":
                if "B" in style_stack: style_stack.remove("B")
            elif t == "em_open":
                style_stack.append("I")
            elif t == "em_close":
                if "I" in style_stack: style_stack.remove("I")
            elif t == "s_open":
                in_strike = True
            elif t == "s_close":
                in_strike = False
            elif t == "link_open":
                in_link = True
                href = (tok.attrs or {}).get("href", "")
            elif t == "link_close":
                in_link = False
                href = ""
            elif t == "image":
                src = (tok.attrs or {}).get("src", "")
                runs.append({"type": "image", "src": src, "alt": tok.content or ""})
            elif t in ("softbreak", "hardbreak"):
                runs.append({"type": "break"})
            elif t == "code_inline":
                runs.append({"type": "text", "text": tok.content, "style": "",
                             "code": True, "strike": False, "link": False, "href": ""})
            elif t == "text":
                has_b = "B" in style_stack
                has_i = "I" in style_stack
                style = ("BI" if has_b and has_i else
                         "B"  if has_b else
                         "I"  if has_i else "")
                runs.append({"type": "text", "text": tok.content, "style": style,
                             "code": False, "strike": in_strike,
                             "link": in_link, "href": href})
        return runs

    def _render_inline(self, pdf: FPDF, children: list, md_dir: Path,
                       indent_mm: float, blockquote: bool):
        """
        Render a sequence of inline tokens onto the PDF using pdf.write() for
        continuous flow.  Handles bold, italic, bold-italic, inline code,
        strikethrough, and links (visual + clickable URI annotation).

        Blockquote bar: drawn line-by-line so it correctly spans page breaks.
        Before each write() we snapshot y; after it we draw a bar segment
        covering that line's y range.  A page break is detected when y
        decreases (fpdf2 reset the cursor to the new page's top margin).
        """
        x_start = self.margin + indent_mm
        line_h  = self.font_size * 0.4 + 1

        # Set the effective left margin so fpdf2's write() wraps continuation
        # lines back to x_start instead of self.margin.  Restored at the end.
        pdf.set_left_margin(x_start)

        if blockquote:
            bar_x = x_start - BLOCKQUOTE_BAR_W - 1

        def _draw_bar_segment(y_top, y_bot):
            """Draw one bar segment; skipped if zero height."""
            if y_bot > y_top:
                pdf.set_fill_color(180, 180, 180)
                pdf.rect(bar_x, y_top, BLOCKQUOTE_BAR_W, y_bot - y_top, style="F")
                pdf.set_fill_color(255, 255, 255)

        pdf.set_x(x_start)
        seg_top = pdf.get_y()   # top of current bar segment

        for run in self._collect_runs(children):
            rtype = run["type"]

            if rtype == "break":
                if blockquote:
                    _draw_bar_segment(seg_top, pdf.get_y() + line_h)
                pdf.ln(line_h)
                pdf.set_x(x_start)
                seg_top = pdf.get_y()
                continue

            if rtype == "image":
                img_path = self._resolve_image(run["src"], md_dir)
                if img_path:
                    self._embed_image(pdf, img_path, label=run["alt"])
                elif self.on_missing_image == "placeholder":
                    self._draw_placeholder(pdf, run["alt"] or run["src"])
                continue

            # ── Text run ────────────────────────────────────────────────
            text     = run["text"]
            style    = run["style"]
            is_code  = run["code"]
            is_strike= run["strike"]
            is_link  = run["link"]
            href     = run["href"]

            y_before = pdf.get_y()

            if is_code:
                # Inline code: monospaced font, tinted background rect.
                # We measure tw for the rect geometry but emit text via write()
                # so that fpdf2 manages cursor advancement from its own glyph
                # metrics — avoiding the rounding drift that cell(tw,...) causes.
                pdf.set_font(self._code_font_name, size=self.font_size - 1)
                tw = pdf.get_string_width(text)
                cx, cy = pdf.get_x(), pdf.get_y()
                if cx + tw > self.margin + self.available_width:
                    pdf.ln(line_h)
                    pdf.set_x(x_start)
                    cx, cy = pdf.get_x(), pdf.get_y()
                r, g, b = self.code_bg_color
                pdf.set_fill_color(r, g, b)
                pdf.rect(cx - 0.5, cy + 0.3, tw + 1.0, line_h + 0.2, style="F")
                pdf.set_x(cx)
                pdf.write(line_h, text)
                pdf.set_font(self._body_font_name, size=self.font_size)

            elif is_link:
                display = f"{text} ({href})" if href else text
                pdf.set_font(self._body_font_name, style=style + "U", size=self.font_size)
                pdf.set_text_color(0, 0, 200)
                pdf.write(line_h, display, link=href)
                pdf.set_text_color(0, 0, 0)
                pdf.set_font(self._body_font_name, style=style, size=self.font_size)

            elif is_strike:
                pdf.set_font(self._body_font_name, style=style, size=self.font_size)
                for wi, word in enumerate(text.split(" ")):
                    w_text = word + (" " if wi < len(text.split(" ")) - 1 else "")
                    sx, sy = pdf.get_x(), pdf.get_y()
                    tw = pdf.get_string_width(w_text)
                    if sx + tw > self.margin + self.available_width:
                        pdf.ln(line_h)
                        pdf.set_x(x_start)
                        sx, sy = pdf.get_x(), pdf.get_y()
                    pdf.write(line_h, w_text)
                    pdf.set_draw_color(0, 0, 0)
                    pdf.set_line_width(0.2)
                    pdf.line(sx, sy + line_h * 0.55, sx + tw, sy + line_h * 0.55)

            else:
                pdf.set_font(self._body_font_name, style=style, size=self.font_size)
                pdf.write(line_h, text)

            # ── Bar segment tracking ─────────────────────────────────────
            if blockquote:
                y_after = pdf.get_y()
                if y_after < y_before:
                    # Page break occurred mid-run: close segment at old page bottom,
                    # start new segment at new page top.
                    _draw_bar_segment(seg_top, self.page_h - self.margin - 8)
                    seg_top = y_after
                # else: segment continues on same page

        # Close final bar segment
        if blockquote:
            _draw_bar_segment(seg_top, pdf.get_y() + line_h)

        # Restore the document left margin so subsequent elements render correctly.
        pdf.set_left_margin(self.margin)
        pdf.ln(line_h * 0.3)

    # ------------------------------------------------------------------
    # Code block rendering
    # ------------------------------------------------------------------

    def _fit_code_font_size(self, lines: list[str], pdf: FPDF) -> tuple[int, list[str]]:
        """
        Find the largest font size (≤ code_font_size, ≥ MIN_CODE_FONT_SIZE) at
        which the longest line fits the inner box width.  If it still overflows
        at the minimum size, wrap long lines with a ↵ continuation marker.
        Returns (chosen_size, final_lines).
        """
        aw = self.available_width - 2 * TABLE_PADDING

        chosen = MIN_CODE_FONT_SIZE
        for size in range(self.code_font_size, MIN_CODE_FONT_SIZE - 1, -1):
            pdf.set_font(self._code_font_name, size=size)
            if max((pdf.get_string_width(ln) for ln in lines), default=0) <= aw:
                chosen = size
                break

        pdf.set_font(self._code_font_name, size=chosen)
        final_lines = []
        for ln in lines:
            if pdf.get_string_width(ln) <= aw:
                final_lines.append(ln)
            else:
                # Approximate column count from average character width
                char_w = pdf.get_string_width("x")
                cols   = max(int(aw / char_w) - 1, 1)
                chunks = textwrap.wrap(ln, width=cols, break_long_words=True,
                                       break_on_hyphens=False)
                for k, chunk in enumerate(chunks):
                    final_lines.append(chunk + (CODE_CONTINUATION if k < len(chunks) - 1 else ""))
        return chosen, final_lines

    def _render_code_block(self, pdf: FPDF, code: str, language: str = ""):
        """
        Render a fenced code block with a tinted background box.

        Syntax highlighting is applied when:
          • self.syntax_highlight is True
          • Pygments is installed
          • the language tag is known to Pygments
        Falls back to plain monochrome text silently in all other cases.

        The font used is self._code_font_name (Courier or the registered TTF).
        """
        lines = code.split("\n")
        chosen_size, final_lines = self._fit_code_font_size(lines, pdf)

        pdf.set_font(self._code_font_name, size=chosen_size)
        line_h  = chosen_size * 0.4 + 0.8
        total_h = line_h * len(final_lines) + TABLE_PADDING * 2

        r, g, b = self.code_bg_color
        pdf.set_fill_color(r, g, b)

        # If the whole block fits on one page but not in the remaining space,
        # start a new page so the block isn't split unnecessarily.
        remaining = self.page_h - self.margin - 8 - pdf.get_y()
        if total_h > remaining and total_h < (self.page_h - 2 * self.margin - 8):
            pdf.add_page()

        box_top = pdf.get_y()
        pdf.rect(self.margin, box_top, self.available_width, total_h, style="F")
        pdf.set_y(box_top + TABLE_PADDING)

        # Build per-line highlight data if Pygments is available
        token_lines = None
        if _PYGMENTS and self.syntax_highlight and language:
            try:
                lexer       = get_lexer_by_name(language, stripall=False)
                raw_tokens  = list(lex(code, lexer))
                token_lines = self._tokens_to_lines(raw_tokens)
            except ClassNotFound:
                pass  # unknown language → plain text

        inner_w = self.available_width - 2 * TABLE_PADDING
        for line_idx, ln in enumerate(final_lines):
            pdf.set_x(self.margin + TABLE_PADDING)
            if token_lines and line_idx < len(token_lines):
                self._write_highlighted_line(pdf, token_lines[line_idx], line_h, inner_w)
            else:
                pdf.set_text_color(30, 30, 30)
                pdf.cell(inner_w, line_h, ln)
            pdf.ln(line_h)

        pdf.set_text_color(0, 0, 0)
        pdf.set_font(self._body_font_name, size=self.font_size)

    def _tokens_to_lines(self, raw_tokens: list) -> list[list[tuple]]:
        """
        Split a flat Pygments token list into per-line segment lists.
        Each entry is a list of (text, rgb) tuples for one source line.
        """
        lines = [[]]
        for ttype, value in raw_tokens:
            color = _highlight_color(ttype)
            parts = value.split("\n")
            for k, part in enumerate(parts):
                if part:
                    lines[-1].append((part, color))
                if k < len(parts) - 1:
                    lines.append([])
        # Pygments always appends a trailing newline → drop trailing empty entry
        if lines and not lines[-1]:
            lines.pop()
        return lines

    def _write_highlighted_line(self, pdf: FPDF, segments: list[tuple],
                                 line_h: float, max_w: float):
        """
        Write one line of (text, rgb) segments using write() per segment.
        write() lets fpdf2 advance the cursor from its own glyph metrics,
        avoiding the rounding drift that accumulates with cell(tw,...) calls.
        Overflow guard is checked before each segment; wrapping is already
        handled upstream in _fit_code_font_size.
        """
        x0 = pdf.get_x()
        for text, (r, g, b) in segments:
            tw = pdf.get_string_width(text)
            if pdf.get_x() - x0 + tw > max_w:
                break
            pdf.set_text_color(r, g, b)
            pdf.write(line_h, text)

    # ------------------------------------------------------------------
    # Table rendering
    # ------------------------------------------------------------------

    def _render_table(self, pdf: FPDF, rows: list[list[str]], alignments: list[str]):
        """Equal-width columns, L/C/R alignment, header row, alternating fill."""
        if not rows:
            return
        n_cols = max(len(row) for row in rows)
        cell_w = self.available_width / max(n_cols, 1)
        line_h = self.font_size * 0.4 + 1.5

        # Keep header + at least one data row together: if the table (or just
        # those two rows) won't fit in the remaining space, start a new page.
        min_h   = line_h * min(2, len(rows))  # header + 1 row minimum
        total_h = line_h * len(rows)
        remaining = self.page_h - self.margin - 8 - pdf.get_y()
        if min_h > remaining:
            pdf.add_page()
        elif total_h <= (self.page_h - 2 * self.margin - 8) and total_h > remaining:
            pdf.add_page()

        for row_idx, row in enumerate(rows):
            if row_idx == 0:
                pdf.set_fill_color(220, 220, 220)
                pdf.set_font(self._body_font_name, style="B", size=self.font_size)
            else:
                pdf.set_fill_color(*(248, 248, 248) if row_idx % 2 == 0 else (255, 255, 255))
                pdf.set_font(self._body_font_name, size=self.font_size)

            for col_idx in range(n_cols):
                text  = row[col_idx] if col_idx < len(row) else ""
                align = alignments[col_idx] if col_idx < len(alignments) else "L"
                pdf.cell(cell_w, line_h, text, border=1, align=align, fill=True)
            pdf.ln(line_h)

        pdf.set_font(self._body_font_name, size=self.font_size)

    # ------------------------------------------------------------------
    # Image / placeholder helpers
    # ------------------------------------------------------------------

    def _resolve_image(self, src: str, md_dir: Path) -> str | None:
        """
        Locate an image file.  Resolution order:
          1. Absolute path as written
          2. Relative to the .md file's directory
          3. Scan image_search_paths by filename
          4. Apply on_missing_image policy
        Returns an absolute path string or None.
        """
        p = Path(src)
        if p.is_absolute() and p.exists():
            return str(p)
        candidate = md_dir / src
        if candidate.exists():
            return str(candidate)
        fname = p.name
        for search_dir in self.image_search_paths:
            candidate = Path(search_dir) / fname
            if candidate.exists():
                return str(candidate)
        return self._handle_missing_image(src)

    def _handle_missing_image(self, label: str) -> str | None:
        policy = self.on_missing_image
        if policy == "prompt":
            print(f"[IMAGE NOT FOUND] Cannot locate: {label}")
            answer = input("Paste the full path (or Enter to skip): ").strip()
            if answer and Path(answer).exists():
                return answer
            print(f"  → Skipping: {label}")
            return None
        elif policy == "skip":
            return None
        else:  # "placeholder"
            return None  # caller draws the grey box

    def _embed_image(self, pdf: FPDF, img_path: str, label: str | None):
        """Embed image scaled to available_width, preserving aspect ratio."""
        try:
            pdf.image(img_path, x=self.margin, w=self.available_width)
        except Exception as exc:
            print(f"[WARN] Could not embed image {img_path}: {exc}")
            self._draw_placeholder(pdf, label or img_path)

    def _draw_placeholder(self, pdf: FPDF, label: str):
        """Grey box with a centred label, used for missing images/diagrams."""
        box_h = 20
        pdf.set_fill_color(200, 200, 200)
        pdf.set_draw_color(150, 150, 150)
        pdf.rect(self.margin, pdf.get_y(), self.available_width, box_h, style="FD")
        pdf.set_font(self._body_font_name, style="I", size=self.font_size - 1)
        pdf.set_text_color(80, 80, 80)
        pdf.set_xy(self.margin, pdf.get_y() + box_h / 2 - 2)
        pdf.cell(self.available_width, 4, f"[Image: {label}]", align="C")
        pdf.ln(box_h / 2 + 2)
        pdf.set_text_color(0, 0, 0)
        pdf.set_draw_color(0, 0, 0)
        pdf.set_font(self._body_font_name, size=self.font_size)

    # ------------------------------------------------------------------
    # Mermaid
    # ------------------------------------------------------------------

    def _render_mermaid(self, code: str, out_dir: Path) -> str | None:
        """
        Fetch a PNG from mermaid.ink for the given diagram source.
        Returns a temp file path on success, None on network failure.
        Caller must delete the temp file after embedding.

        mermaid.ink supports two URL schemes; we try both:
          /img/<base64>        — original scheme
          /img/<base64>?type=png — explicit type, needed on some versions
        A 403 on the first attempt usually means the API moved; we retry
        with the explicit type parameter before giving up.
        """
        import base64
        encoded  = base64.urlsafe_b64encode(code.encode("utf-8")).decode("ascii")
        base_url = self.mermaid_ink_url.rstrip("/")
        urls = [
            f"{base_url}/{encoded}?type=png",   # newer API
            f"{base_url}/{encoded}",             # original
        ]
        tmp_path = out_dir / f"_mermaid_{abs(hash(code))}.png"
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "MDToPDFConverter/1.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    tmp_path.write_bytes(resp.read())
                return str(tmp_path)
            except Exception as exc:
                print(f"[WARN] Mermaid URL failed ({url}): {exc}")
        print("[WARN] All Mermaid URLs failed — applying on_missing_image policy.")
        return None


# ---------------------------------------------------------------------------
# FPDF subclass — header and footer
# ---------------------------------------------------------------------------

class _DocumentPDF(FPDF):
    """Adds a document-title header and page-number footer to every page."""

    def __init__(self, doc_title, margin, font_size, hf_font_size, heading_color, body_font_name, **kwargs):
        super().__init__(**kwargs)
        self._doc_title     = doc_title
        self._margin        = margin
        self._font_size     = font_size
        self._hf_font_size =  hf_font_size
        self._heading_color = heading_color
        self._body_font     = body_font_name

    def header(self):
        self.set_font(self._body_font, style="I", size=self._hf_font_size)
        r, g, b = self._heading_color
        self.set_text_color(r, g, b)
        self.set_y(4)
        self.cell(0, 8, self._doc_title, align="L")
        self.set_text_color(0, 0, 0)
        self.set_y(self._margin +4)

    def footer(self):
        self.set_y(-12)
        self.set_font(self._body_font, style="I", size=self._hf_font_size)
        self.set_text_color(120, 120, 120)
        self.cell(0, 6, f"Page {self.page_no()}", align="C")
        self.set_text_color(0, 0, 0)
