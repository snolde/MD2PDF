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

MDToPDFConverter - converts Markdown files to PDF using fpdf2 + markdown-it-py.

Target environment: PyDroid 3 on Android (pure Python, no subprocess/shell, no tkinter).
Future migration: Keep this class free of Android-specific deps so it can be embedded
via Chaquopy or rewritten in Java (commonmark-java + iText).

Dependencies: fpdf2 >= 2.8.7, markdown-it-py, Pillow (transitively required by fpdf2).
Pygments is optional - syntax highlighting degrades gracefully if absent.

Design decisions:
- All sizing derived from available_width so page/margin config flows through cleanly.
- markdown-it-py tokenises the MD; we walk the flat token list with a state machine.
- fpdf2 is used for PDF generation (supports Unicode via built-in core fonts or TTF).
- Images are resolved through a priority chain; missing images follow on_missing_image policy.
- Images are sized at natural dimensions if they fit; scaled down proportionally if not.
- Mermaid diagrams are fetched as PNG via mermaid.ink; offline falls back to policy.
- No hardcoded paths anywhere in the class - all come from config or md_path at call time.
- Syntax highlighting via Pygments (optional; degrades gracefully if not installed).
- Mid-document config overrides via <!--cfg key=value--> shortcodes.
"""

import os
import re
import textwrap
import urllib.request
from pathlib import Path

from fpdf import FPDF
from fpdf.fonts import FontFace
from markdown_it import MarkdownIt
from PIL import Image as PilImage

try:
    from pygments import lex
    from pygments.lexers import get_lexer_by_name
    from pygments.util import ClassNotFound
    from pygments.token import Token

    _PYGMENTS = True

    HIGHLIGHT_THEME = {
        Token.Keyword:              (0,   96,  160),
        Token.Keyword.Constant:     (0,   96,  160),
        Token.Keyword.Declaration:  (0,   96,  160),
        Token.Keyword.Namespace:    (0,   96,  160),
        Token.Name.Builtin:         (100, 0,   160),
        Token.Name.Function:        (0,   128, 96),
        Token.Name.Class:           (0,   128, 96),
        Token.Name.Decorator:       (128, 64,  0),
        Token.Literal.String:       (160, 32,  32),
        Token.Literal.String.Doc:   (160, 32,  32),
        Token.Literal.Number:       (0,   128, 0),
        Token.Comment:              (128, 128, 128),
        Token.Operator:             (80,  80,  80),
        Token.Punctuation:          (60,  60,  60),
        Token.Error:                (200, 0,   0),
    }

    def _highlight_color(ttype):
        t = ttype
        while t:
            if t in HIGHLIGHT_THEME:
                return HIGHLIGHT_THEME[t]
            t = t.parent if hasattr(t, "parent") else None
        return (30, 30, 30)

except ImportError:
    _PYGMENTS = False
    HIGHLIGHT_THEME = {}

    def _highlight_color(ttype):
        return (30, 30, 30)


PAGE_SIZES = {
    "A4": (210, 297),
    "A5": (148, 210),
}

MARGIN_PRESETS = {
    "small":  10,
    "normal": 20,
    "wide":   30,
}

HEADING_SCALES = {1: 2.0, 2: 1.6, 3: 1.35, 4: 1.15, 5: 1.0, 6: 0.9}

MIN_CODE_FONT_SIZE = 6
CODE_CONTINUATION  = "\u21b5"
BLOCKQUOTE_BAR_W   = 1.5
TABLE_PADDING      = 2


# ---------------------------------------------------------------------------
# Configuration classes
# ---------------------------------------------------------------------------

class _PageConfig:
    def __init__(self):
        self.size    = "A4"
        self.margins = "normal"

    def setSize(self, size):
        s = size.upper()
        if s not in PAGE_SIZES:
            print(f"[WARN] Unknown page size '{size}' - keeping '{self.size}'.")
        else:
            self.size = s
        return self

    def setMargins(self, margins):
        m = margins.lower()
        if m not in MARGIN_PRESETS:
            print(f"[WARN] Unknown margin preset '{margins}' - keeping '{self.margins}'.")
        else:
            self.margins = m
        return self


class _StyleConfig:
    def __init__(self):
        self.fontSize        = 11
        self.hfFontSize      = 9
        self.codeFontSize    = 9
        self.headingColor    = (0, 0, 128)
        self.codeBgColor     = (240, 240, 240)
        self.bodyFontPath    = None
        self.codeFontPath    = None
        self.syntaxHighlight = True

    def setFontSize(self, size):
        self.fontSize = int(size); return self

    def setHfFontSize(self, size):
        self.hfFontSize = int(size); return self

    def setCodeFontSize(self, size):
        self.codeFontSize = int(size); return self

    def setHeadingColor(self, r, g, b):
        self.headingColor = (int(r), int(g), int(b)); return self

    def setCodeBgColor(self, r, g, b):
        self.codeBgColor = (int(r), int(g), int(b)); return self

    def setBodyFontPath(self, path):
        self.bodyFontPath = path; return self

    def setCodeFontPath(self, path):
        self.codeFontPath = path; return self

    def setSyntaxHighlight(self, enabled):
        self.syntaxHighlight = bool(enabled); return self


class _ImageConfig:
    ALIGN_VALUES = ("left", "center")

    def __init__(self):
        self.maxWidthFraction  = 1.0
        self.maxHeightFraction = 0.85
        self.align             = "center"
        self.expand            = False
        self.fitRatio          = 0.50  # min fraction of max width acceptable when shrinking
                                       # to fit remaining page space; below this, page break instead

    def setMaxWidthFraction(self, v):
        self.maxWidthFraction = max(0.0, min(1.0, float(v))); return self

    def setMaxHeightFraction(self, v):
        self.maxHeightFraction = max(0.0, min(1.0, float(v))); return self

    def setAlign(self, align):
        a = align.lower()
        if a not in self.ALIGN_VALUES:
            print(f"[WARN] Unknown image align '{align}' - keeping '{self.align}'.")
        else:
            self.align = a
        return self

    def setExpand(self, expand):
        self.expand = bool(expand); return self

    def setFitRatio(self, v):
        """
        Fraction of max_w at which shrinking to fit remaining page space is
        preferred over a page break.  Range 0.0-1.0.
          1.0 = never shrink, always page break if image doesn't fit remaining space
          0.0 = always shrink to fit, never page break for images
          0.75 (default) = shrink if result is >= 75% of max width, else page break
        """
        self.fitRatio = max(0.0, min(1.0, float(v))); return self


class MD2PdfConfig:
    """
    Top-level configuration for MDToPDFConverter.

    Usage:
        cfg = MD2PdfConfig()
        cfg.page.setSize("A5").setMargins("small")
        cfg.style.setFontSize(11).setHeadingColor(0, 0, 128)
        cfg.image.setMaxHeightFraction(0.85).setAlign("center")
        cfg.setOutputDir("/path/to/output")
        cfg.setImageSearchPaths(["/sdcard/Pictures"])

    Mid-document overrides in Markdown:
        <!--cfg image.align=left-->
        <!--cfg image.maxWidthFraction=0.5-->
        <!--cfg style.fontSize=10-->
        <!--cfg reset-->
    """

    def __init__(self):
        self.page             = _PageConfig()
        self.style            = _StyleConfig()
        self.image            = _ImageConfig()
        self.outputDir        = None
        self.imageSearchPaths = []
        self.onMissingImage   = "placeholder"
        self.mermaidInkUrl    = "https://mermaid.ink/img/"

    def setOutputDir(self, path):
        self.outputDir = path; return self

    def setImageSearchPaths(self, paths):
        self.imageSearchPaths = list(paths); return self

    def setOnMissingImage(self, policy):
        if policy not in ("prompt", "skip", "placeholder"):
            print(f"[WARN] Unknown onMissingImage policy '{policy}'.")
        else:
            self.onMissingImage = policy
        return self

    def setMermaidInkUrl(self, url):
        self.mermaidInkUrl = url; return self


# ---------------------------------------------------------------------------
# Shortcode config setter map
# ---------------------------------------------------------------------------

_CFG_SETTERS = {
    "page.size":               lambda c, v: c.page.setSize(v),
    "page.margins":            lambda c, v: c.page.setMargins(v),
    "style.fontSize":          lambda c, v: c.style.setFontSize(int(v)),
    "style.hfFontSize":        lambda c, v: c.style.setHfFontSize(int(v)),
    "style.codeFontSize":      lambda c, v: c.style.setCodeFontSize(int(v)),
    "style.syntaxHighlight":   lambda c, v: c.style.setSyntaxHighlight(v.lower() == "true"),
    "image.maxWidthFraction":  lambda c, v: c.image.setMaxWidthFraction(float(v)),
    "image.maxHeightFraction": lambda c, v: c.image.setMaxHeightFraction(float(v)),
    "image.align":             lambda c, v: c.image.setAlign(v),
    "image.expand":            lambda c, v: c.image.setExpand(v.lower() == "true"),
    "image.fitRatio":          lambda c, v: c.image.setFitRatio(float(v)),
    "onMissingImage":          lambda c, v: c.setOnMissingImage(v),
}


# ---------------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------------

class MDToPDFConverter:
    """Convert a Markdown file to PDF. Accepts MD2PdfConfig or legacy dict."""

    _MONO_CANDIDATES = [
        "/system/fonts/DroidSansMono.ttf",
        "/system/fonts/CutiveMono.ttf",
        "/system/fonts/NotoMono-Regular.ttf",
        "/system/fonts/RobotoMono-Regular.ttf",
        "/system/fonts/CutiveMono-Regular.ttf",
        "/system/fonts/SourceCodePro-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ]

    _BODY_CANDIDATES = [
        "/system/fonts/Roboto-Regular.ttf",
        "/system/fonts/SourceSansPro-Regular.ttf",
        "/system/fonts/DroidSans.ttf",
        "/system/fonts/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]

    def __init__(self, config):
        if isinstance(config, dict):
            self._cfg = self._config_from_dict(config)
        else:
            self._cfg = config
        self._apply_config(self._cfg)
        self._body_font_name = "Helvetica"
        self._code_font_name = "Courier"
        self._md = (
            MarkdownIt("commonmark")
            .enable("table")
            .enable("strikethrough")
        )

    def _apply_config(self, cfg):
        """Apply all config fields to instance variables. Used at init and by <!--cfg reset-->."""
        self.page_w, self.page_h = PAGE_SIZES.get(cfg.page.size, PAGE_SIZES["A4"])
        self.margin              = MARGIN_PRESETS.get(cfg.page.margins, 20)
        self.available_width     = self.page_w - 2 * self.margin
        self.font_size           = cfg.style.fontSize
        self.hf_font_size        = cfg.style.hfFontSize
        self.code_font_size      = cfg.style.codeFontSize
        self.heading_color       = cfg.style.headingColor
        self.code_bg_color       = cfg.style.codeBgColor
        self.body_font_path      = cfg.style.bodyFontPath
        self.code_font_path      = cfg.style.codeFontPath
        self.syntax_highlight    = cfg.style.syntaxHighlight
        self.img_max_w_frac      = cfg.image.maxWidthFraction
        self.img_max_h_frac      = cfg.image.maxHeightFraction
        self.img_align           = cfg.image.align
        self.img_expand          = cfg.image.expand
        self.img_fit_ratio       = cfg.image.fitRatio
        self.image_search_paths  = cfg.imageSearchPaths
        self.on_missing_image    = cfg.onMissingImage
        self.mermaid_ink_url     = cfg.mermaidInkUrl
        self.output_dir          = cfg.outputDir

    @staticmethod
    def _config_from_dict(d):
        """Convert legacy dict config to MD2PdfConfig."""
        cfg   = MD2PdfConfig()
        page  = d.get("page", {})
        style = d.get("style", {})
        img   = d.get("image", {})
        if "size"    in page:  cfg.page.setSize(page["size"])
        if "margins" in page:  cfg.page.setMargins(page["margins"])
        if "font_size"        in style: cfg.style.setFontSize(style["font_size"])
        if "hf_font_size"     in style: cfg.style.setHfFontSize(style["hf_font_size"])
        if "code_font_size"   in style: cfg.style.setCodeFontSize(style["code_font_size"])
        if "heading_color"    in style: cfg.style.headingColor = style["heading_color"]
        if "code_bg_color"    in style: cfg.style.codeBgColor  = style["code_bg_color"]
        if "body_font_path"   in style: cfg.style.setBodyFontPath(style["body_font_path"])
        if "code_font_path"   in style: cfg.style.setCodeFontPath(style["code_font_path"])
        if "syntax_highlight" in style: cfg.style.setSyntaxHighlight(style["syntax_highlight"])
        if "maxWidthFraction"  in img: cfg.image.setMaxWidthFraction(img["maxWidthFraction"])
        if "maxHeightFraction" in img: cfg.image.setMaxHeightFraction(img["maxHeightFraction"])
        if "align"             in img: cfg.image.setAlign(img["align"])
        if "expand"            in img: cfg.image.setExpand(img["expand"])
        if "image_search_paths" in d: cfg.setImageSearchPaths(d["image_search_paths"])
        if "output_dir"         in d: cfg.setOutputDir(d["output_dir"])
        if "on_missing_image"   in d: cfg.setOnMissingImage(d["on_missing_image"])
        if "mermaid_ink_url"    in d: cfg.setMermaidInkUrl(d["mermaid_ink_url"])
        return cfg

    @staticmethod
    def _find_font(explicit_path, candidates, label):
        if explicit_path:
            if Path(explicit_path).exists():
                return explicit_path
            print(f"[WARN] {label} font not found at {explicit_path} - auto-detecting.")
        for path in candidates:
            if Path(path).exists():
                print(f"[INFO] {label} font: {path}")
                return path
        print(f"[WARN] No {label} TTF found - falling back to core PDF font.")
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def convert(self, md_path):
        md_path = Path(md_path)
        if not md_path.exists():
            print(f"[ERROR] File not found: {md_path}")
            return None
        try:
            md_text = md_path.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"[ERROR] Cannot read {md_path}: {exc}")
            return None
        out_dir = Path(self.output_dir) if self.output_dir else md_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
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

    def _build_pdf(self, tokens, md_path, out_path):
        md_dir  = md_path.parent
        out_dir = out_path.parent

        # Restore document-level config so mid-document overrides from a
        # previous convert() call never bleed into the next one.
        self._apply_config(self._cfg)

        doc_title = md_path.stem
        for i, tok in enumerate(tokens):
            if tok.type == "heading_open" and tok.tag == "h1":
                if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                    doc_title = tokens[i + 1].content
                break

        self._body_font_name = "Helvetica"
        self._code_font_name = "Courier"
        body_path = self._find_font(self.body_font_path, self._BODY_CANDIDATES, "body")
        if body_path:
            self._body_font_name = "BodyFont"
        code_path = self._find_font(self.code_font_path, self._MONO_CANDIDATES, "code")
        if code_path:
            self._code_font_name = "CodeFont"

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

        if body_path:
            try:
                p = Path(body_path)
                stem   = p.stem
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
                print(f"[WARN] Could not register body font: {exc} - using Helvetica.")
                self._body_font_name = "Helvetica"

        if code_path:
            try:
                pdf.add_font("CodeFont", fname=code_path, uni=True)
            except Exception as exc:
                print(f"[WARN] Could not register code font: {exc} - using Courier.")
                self._code_font_name = "Courier"
        # Add fallback fonts
        fallbacks = []
        if os.path.exists("/system/fonts/NotoColorEmoji.ttf"):
            pdf.add_font("NotoEmoji", fname="/system/fonts/NotoColorEmoji.ttf", uni=True)
            fallbacks.append("NotoEmoji")
            print("Emoji added")        
        if os.path.exists("/system/fonts/NotoSansSymbols-Regular-Subsetted.ttf"):
            pdf.add_font("NotoSymbols", fname="/system/fonts/NotoSansSymbols-Regular-Subsetted.ttf", uni=True)
            fallbacks.append("NotoSymbols")
            print("Symbols added")
        if os.path.exists("/system/fonts/NotoSansSymbols-Regular-Subsetted2.ttf"):
            pdf.add_font("NotoSymbols2", fname="/system/fonts/NotoSansSymbols-Regular-Subsetted2.ttf", uni=True)
            fallbacks.append("NotoSymbols2")
            print("Symbols2 added")       
        if fallbacks:
            pdf.set_fallback_fonts(fallbacks, exact_match=False)
            pdf.set_auto_page_break(auto=True, margin=self.margin +8)
        pdf.add_page()
        pdf.set_margins(self.margin, self.margin, self.margin)

        i                = 0
        list_stack       = []
        blockquote_depth = 0
        in_table         = False
        table_rows       = []
        table_alignments = []

        while i < len(tokens):
            tok = tokens[i]
            # Headings
            if tok.type == "heading_open":
                level  = int(tok.tag[1])
                text   = self._inline_text(tokens[i + 1].children or [])
                h_size = round(self.font_size * HEADING_SCALES.get(level, 1.0))
                line_h = h_size * 0.4 + 2
                if pdf.get_y() + line_h * 2 > self.page_h - self.margin - 8:
                    pdf.add_page()
                r, g, b = self.heading_color
                pdf.set_font(self._body_font_name, style="B", size=h_size)
                pdf.set_text_color(r, g, b)
                pdf.ln(3 if level > 1 else 5)
                pdf.multi_cell(self.available_width, line_h, text, align="L")
                pdf.ln(2)
                pdf.set_text_color(0, 0, 0)
                i += 3; continue

            # Paragraphs
            if tok.type == "paragraph_open":
                inline = tokens[i + 1]
                indent = len(list_stack) * 6 if list_stack else blockquote_depth * 8
                if list_stack:
                    kind, count = list_stack[-1]
                    line_h   = self.font_size * 0.4 + 1
                    depth    = len(list_stack)
                    marker_x = self.margin + (depth - 1) * 6
                    text_x   = self.margin + depth * 6
                    markers  = ["•", "*", "+"]
                    marker   = markers[(depth - 1) % len(markers)] if kind == "bullet" else f"{count}."
                    pdf.set_font(self._body_font_name, size=self.font_size)
                    pdf.set_xy(marker_x, pdf.get_y())
                    pdf.cell(text_x - marker_x, line_h, marker, align="R")
                    pdf.set_x(text_x)
                self._render_inline(pdf, inline.children or [], md_dir, indent, blockquote_depth > 0)
                pdf.ln(self.font_size * 0.4 + 1)
                i += 3; continue

            # Lists
            if tok.type == "bullet_list_open":
                list_stack.append(("bullet", 0)); i += 1; continue
            if tok.type == "ordered_list_open":
                list_stack.append(("ordered", 0)); i += 1; continue
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

            # Blockquotes
            if tok.type == "blockquote_open":
                blockquote_depth += 1; i += 1; continue
            if tok.type == "blockquote_close":
                blockquote_depth = max(0, blockquote_depth - 1); i += 1; continue

            # Fenced code / mermaid
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
                pdf.ln(3); i += 1; continue

            # Indented code blocks
            if tok.type == "code_block":
                self._render_code_block(pdf, tok.content.rstrip("\n"))
                pdf.ln(3); i += 1; continue

            # Shortcodes
            if tok.type in ("html_block", "html_inline"):
                raw = tok.content.strip()
                if re.search(r'<!--\s*pb\s*-->', raw):
                    pdf.add_page()
                else:
                    m = re.search(r'<!--\s*b\s*(\d+(?:\.\d+)?)\s*-->', raw)
                    if m:
                        pdf.ln(float(m.group(1)))
                    else:
                        m = re.search(r'<!--\s*cfg\s+(.+?)\s*-->', raw)
                        if m:
                            self._apply_cfg_shortcode(m.group(1).strip())
                i += 1; continue

            # Horizontal rules
            if tok.type == "hr":
                pdf.set_draw_color(180, 180, 180)
                pdf.set_line_width(0.3)
                y = pdf.get_y() + 2
                pdf.line(self.margin, y, self.margin + self.available_width, y)
                pdf.set_line_width(0.2)
                pdf.set_draw_color(0, 0, 0)
                pdf.ln(5); i += 1; continue

            # Tables
            if tok.type == "table_open":
                table_rows, table_alignments, in_table = [], [], True
                i += 1; continue
            if tok.type == "table_close":
                in_table = False
                self._render_table(pdf, table_rows, table_alignments)
                pdf.ln(3); table_rows = []; i += 1; continue
            if tok.type in ("thead_open", "thead_close", "tbody_open", "tbody_close"):
                i += 1; continue
            if tok.type == "tr_open":
                table_rows.append([]); i += 1; continue
            if tok.type == "tr_close":
                i += 1; continue
            if tok.type in ("th_open", "td_open"):
                style_attr = (tok.attrs or {}).get("style", "")
                if "right"  in style_attr:  align = "R"
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

            if tok.type == "inline":
                self._render_inline(pdf, tok.children or [], md_dir, 0, False)
                pdf.ln(2); i += 1; continue

            i += 1

        pdf.output(str(out_path))

    # ------------------------------------------------------------------
    # Mid-document config shortcode
    # ------------------------------------------------------------------

    def _apply_cfg_shortcode(self, expr):
        if expr.lower() == "reset":
            self._apply_config(self._cfg)
            return
        m = re.match(r'([\w.]+)\s*=\s*(.+)', expr)
        if not m:
            print(f"[WARN] Unrecognised cfg shortcode: '{expr}'")
            return
        key, value = m.group(1).strip(), m.group(2).strip()
        setter = _CFG_SETTERS.get(key)
        if setter is None:
            print(f"[WARN] Unknown cfg key '{key}' - ignored.")
            return
        try:
            setter(self._cfg, value)
            self._apply_config(self._cfg)
        except Exception as exc:
            print(f"[WARN] cfg shortcode '{expr}' failed: {exc}")

    # ------------------------------------------------------------------
    # Inline rendering
    # ------------------------------------------------------------------

    def _inline_text(self, children):
        parts = []
        for child in children:
            if child.type in ("text", "code_inline"):
                parts.append(child.content)
            elif child.type in ("softbreak", "hardbreak"):
                parts.append(" ")
        return "".join(parts)

    def _collect_runs(self, children):
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
                in_link = False; href = ""
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
                style = ("BI" if has_b and has_i else "B" if has_b else "I" if has_i else "")
                runs.append({"type": "text", "text": tok.content, "style": style,
                             "code": False, "strike": in_strike, "link": in_link, "href": href})
        return runs

    def _render_inline(self, pdf, children, md_dir, indent_mm, blockquote):
        x_start = self.margin + indent_mm
        line_h  = self.font_size * 0.4 + 1

        # Set left margin so write() wraps continuation lines to x_start.
        pdf.set_left_margin(x_start)

        if blockquote:
            bar_x = x_start - BLOCKQUOTE_BAR_W - 1

        def _draw_bar_segment(y_top, y_bot):
            if y_bot > y_top:
                pdf.set_fill_color(180, 180, 180)
                pdf.rect(bar_x, y_top, BLOCKQUOTE_BAR_W, y_bot - y_top, style="F")
                pdf.set_fill_color(255, 255, 255)

        pdf.set_x(x_start)
        seg_top = pdf.get_y()

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

            text      = run["text"]
            style     = run["style"]
            is_code   = run["code"]
            is_strike = run["strike"]
            is_link   = run["link"]
            href      = run["href"]
            y_before  = pdf.get_y()

            if is_code:
                pdf.set_font(self._code_font_name, size=self.font_size - 1)
                tw = pdf.get_string_width(text)
                cx, cy = pdf.get_x(), pdf.get_y()
                if cx + tw > self.margin + self.available_width:
                    pdf.ln(line_h); pdf.set_x(x_start)
                    cx, cy = pdf.get_x(), pdf.get_y()
                r, g, b = self.code_bg_color
                pdf.set_fill_color(r, g, b)
                # GPT adjustments sucked. Corrected
                pdf.rect(cx + 1, cy + 0.3, tw, line_h + 0.2, style="F")
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
                        pdf.ln(line_h); pdf.set_x(x_start)
                        sx, sy = pdf.get_x(), pdf.get_y()
                    pdf.write(line_h, w_text)
                    pdf.set_draw_color(0, 0, 0)
                    pdf.set_line_width(0.2)
                    pdf.line(sx, sy + line_h * 0.55, sx + tw, sy + line_h * 0.55)

            else:
                pdf.set_font(self._body_font_name, style=style, size=self.font_size)
                pdf.write(line_h, text)

            if blockquote:
                y_after = pdf.get_y()
                if y_after < y_before:
                    _draw_bar_segment(seg_top, self.page_h - self.margin - 8)
                    seg_top = y_after

        if blockquote:
            _draw_bar_segment(seg_top, pdf.get_y() + line_h)

        # Restore document left margin.
        pdf.set_left_margin(self.margin)
        pdf.ln(line_h * 0.3)

    # ------------------------------------------------------------------
    # Code block rendering
    # ------------------------------------------------------------------

    def _fit_code_font_size(self, lines, pdf):
        aw     = self.available_width - 2 * TABLE_PADDING
        chosen = MIN_CODE_FONT_SIZE
        for size in range(self.code_font_size, MIN_CODE_FONT_SIZE - 1, -1):
            pdf.set_font(self._code_font_name, size=size)
            if max((pdf.get_string_width(ln) for ln in lines), default=0) <= aw:
                chosen = size; break
        pdf.set_font(self._code_font_name, size=chosen)
        final_lines = []
        for ln in lines:
            if pdf.get_string_width(ln) <= aw:
                final_lines.append(ln)
            else:
                char_w = pdf.get_string_width("x")
                cols   = max(int(aw / char_w) - 1, 1)
                chunks = textwrap.wrap(ln, width=cols, break_long_words=True,
                                       break_on_hyphens=False)
                for k, chunk in enumerate(chunks):
                    final_lines.append(chunk + (CODE_CONTINUATION if k < len(chunks) - 1 else ""))
        return chosen, final_lines

    def _render_code_block(self, pdf, code, language=""):
        lines = code.split("\n")
        chosen_size, final_lines = self._fit_code_font_size(lines, pdf)
        pdf.set_font(self._code_font_name, size=chosen_size)
        line_h  = chosen_size * 0.4 + 0.8
        total_h = line_h * len(final_lines) + TABLE_PADDING * 2
        r, g, b = self.code_bg_color
        page_body_h = self.page_h - 2 * self.margin - 8

        # Pre-compute syntax token lines if applicable.
        token_lines = None
        if _PYGMENTS and self.syntax_highlight and language:
            try:
                lexer      = get_lexer_by_name(language, stripall=False)
                raw_tokens = list(lex(code, lexer))
                token_lines = self._tokens_to_lines(raw_tokens)
            except ClassNotFound:
                pass

        def _remaining():
            return self.page_h - self.margin - 8 - pdf.get_y()

        def _draw_bg(top_y, n_lines, extra_bottom=TABLE_PADDING):
            h = line_h * n_lines + extra_bottom
            pdf.set_fill_color(r, g, b)
            pdf.rect(self.margin, top_y, self.available_width, h, style="F")

        # If the whole block fits on a fresh page and does not fit remaining
        # space, pre-emptively page-break so the first rect covers it all.
        if total_h > _remaining() and total_h <= page_body_h:
            pdf.add_page()

        inner_w  = self.available_width - 2 * TABLE_PADDING
        n        = len(final_lines)
        line_idx = 0

        while line_idx < n:
            # How many lines fit in remaining space on this page?
            avail     = _remaining()
            fit_lines = max(0, int((avail - TABLE_PADDING) / line_h))
            remaining_total = n - line_idx

            if fit_lines <= 0:
                # No room at all - just break.
                pdf.add_page()
                fit_lines = max(0, int((_remaining() - TABLE_PADDING) / line_h))

            chunk = min(fit_lines, remaining_total)
            is_last_chunk = (line_idx + chunk >= n)

            # Draw background rect for this chunk.
            box_top = pdf.get_y()
            extra   = TABLE_PADDING if is_last_chunk else 0
            _draw_bg(box_top, chunk, extra_bottom=extra)

            # Render lines in this chunk.
            #pdf.set_y(box_top + (TABLE_PADDING if line_idx == 0 else 0))
            pdf.set_y(box_top)
            for k in range(chunk):
                idx = line_idx + k
                pdf.set_x(self.margin + TABLE_PADDING)
                if token_lines and idx < len(token_lines):
                    self._write_highlighted_line(pdf, token_lines[idx], line_h, inner_w)
                else:
                    pdf.set_text_color(30, 30, 30)
                    pdf.cell(inner_w, line_h, final_lines[idx])
                pdf.set_y(pdf.get_y() + line_h)

            line_idx += chunk

            if line_idx < n:
                pdf.add_page()

        pdf.set_text_color(0, 0, 0)
        pdf.set_font(self._body_font_name, size=self.font_size)

    def _tokens_to_lines(self, raw_tokens):
        lines = [[]]
        for ttype, value in raw_tokens:
            color = _highlight_color(ttype)
            parts = value.split("\n")
            for k, part in enumerate(parts):
                if part:
                    lines[-1].append((part, color))
                if k < len(parts) - 1:
                    lines.append([])
        if lines and not lines[-1]:
            lines.pop()
        return lines

    def _write_highlighted_line(self, pdf, segments, line_h, max_w):
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

    def _render_table(self, pdf, rows, alignments):
        if not rows:
            return
        n_cols = max(len(row) for row in rows)

        # Build column alignment list for fpdf2 table()
        col_aligns = []
        for i in range(n_cols):
            a = alignments[i] if i < len(alignments) else "L"
            col_aligns.append(a)

        # Style for header row
        header_style = FontFace(
            emphasis="BOLD",
            fill_color=(220, 220, 220),
        )

        pdf.set_font(self._body_font_name, size=self.font_size)
        with pdf.table(
            col_widths=tuple([self.available_width / n_cols] * n_cols),
            text_align=tuple(col_aligns),
            line_height=self.font_size * 0.4 + 1.5,
            borders_layout="ALL",
            first_row_as_headings=True,
            headings_style=header_style,
        ) as table:
            for row_idx, row in enumerate(rows):
                # Alternating row fill for body rows
                if row_idx > 0:
                    fill = (248, 248, 248) if row_idx % 2 == 0 else (255, 255, 255)
                    row_style = FontFace(fill_color=fill)
                else:
                    row_style = None
                t_row = table.row(style=row_style)
                for col_idx in range(n_cols):
                    text = row[col_idx] if col_idx < len(row) else ""
                    t_row.cell(text)

        pdf.set_font(self._body_font_name, size=self.font_size)

    # ------------------------------------------------------------------
    # Image helpers
    # ------------------------------------------------------------------

    def _resolve_image(self, src, md_dir):
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

    def _handle_missing_image(self, label):
        policy = self.on_missing_image
        if policy == "prompt":
            print(f"[IMAGE NOT FOUND] Cannot locate: {label}")
            answer = input("Paste the full path (or Enter to skip): ").strip()
            if answer and Path(answer).exists():
                return answer
            print(f"  -> Skipping: {label}")
            return None
        elif policy == "skip":
            return None
        else:
            return None

    def _strip_icc_profile(self, data : bytes):
        """
        Remove ICC profile APP2 markers from raw JPEG bytes.
        Returns BytesIO of the cleaned image.
        JPEG structure: SOI (0xFFD8), then markers: 0xFF <type> <2-byte length> <data>
        ICC profile markers are APP2 (0xFFE2) starting with b'ICC_PROFILE\x00'
        """
        ICC_MARKER = b'\xff\xe2'
        ICC_SIG    = b'ICC_PROFILE\x00'
        out = bytearray()
        i   = 0
    # copy SOI (first 2 bytes) unconditionally
        if data[:2] != b'\xff\xd8':
            raise ValueError("Not a JPEG file")
        out += data[:2]
        i = 2
        while i < len(data):
            if data[i] != 0xFF:
                break  # not a marker, rest is entropy-coded image data
            marker = data[i:i+2]
            # markers without a length field (SOI, EOI, RST*)
            if marker[1] in (0xD8, 0xD9) or (0xD0 <= marker[1] <= 0xD7):
                out += marker
                i += 2
                continue
            if i + 4 > len(data):
                break
            length = (data[i+2] << 8) | data[i+3]  # includes the 2 length bytes
            segment = data[i:i+2+length]
            # skip APP2 segments that carry ICC profile data
            if (marker == ICC_MARKER and
                    segment[4:4+len(ICC_SIG)] == ICC_SIG):
                i += 2 + length
                continue
            out += segment
            i += 2 + length
        # append any remaining entropy-coded data
        out += data[i:]
        from io import BytesIO
        buf = BytesIO(bytes(out))
        return buf
        
    def _get_viewport(self, pdf):
        height = self.page_h - pdf.get_y()
        weight = self.page_w - 2 * self.margin       
        return width, height
    
    def _embed_image(self, pdf, img_path, label):
        """
        Embed image with smart sizing:
          - Natural size if it fits within width and height constraints.
          - Scale down proportionally if either dimension is exceeded.
          - Expand to maxWidthFraction if image.expand is True.
          - Align left or center per image.align.
        Pillow is used to read natural dimensions (lazy header read, no pixel load).
        """
        do_embed = True
        try:
            with PilImage.open(img_path) as im:
                px_w, px_h = im.size
                dpi   = im.info.get("dpi", (96, 96))
                dpi_x = dpi[0] if isinstance(dpi, (tuple, list)) and dpi[0] > 0 else 96

            # Natural size in mm
            nat_w_mm = px_w / dpi_x * 25.4
            nat_h_mm = px_h / dpi_x * 25.4
            print(f"dbg: image {nat_w_mm} x {nat_h_mm}mm")

            # Configured max box
            max_w_mm  = self.available_width * self.img_max_w_frac
            full_h_mm = (self.page_h - 2 * self.margin - 8)
            max_h_mm  = full_h_mm * self.img_max_h_frac
            print(f"dbg: max box {max_w_mm} x {max_h_mm}mm")

            # Aspect ratio of the image
            img_ratio = nat_h_mm / nat_w_mm if nat_w_mm > 0 else 1.0
            print(f"dbg: img_ratio (l/w) {img_ratio:.3f}")

            # Step 1: determine render_w from expand setting, clamped to max_w
            render_w = max_w_mm if self.img_expand else min(nat_w_mm, max_w_mm)

            # Step 2: case 3 — image taller than a full page at max width;
            # must reduce width so height fits within full page viewport.
            # This is unconditional — no page break can help here.
            full_page_ratio = full_h_mm / max_w_mm if max_w_mm > 0 else 1.0
            if img_ratio > full_page_ratio:
                render_w = full_h_mm / img_ratio

            # Step 3: clamp height to max_h (respects maxHeightFraction)
            render_h = render_w * img_ratio
            if render_h > max_h_mm:
                render_w = max_h_mm / img_ratio
                render_h = max_h_mm

            # Step 4: decide whether to fit into remaining space or page break.
            remaining = self.page_h - self.margin - 8 - pdf.get_y()
            effective_remaining = min(remaining, max_h_mm)
            remaining_ratio     = effective_remaining / max_w_mm if max_w_mm > 0 else 0.0
            print(f"dbg: viewport {max_w_mm:.1f} x {effective_remaining:.1f}")

            if img_ratio <= remaining_ratio:
                # Case 1: image fits in remaining space at full render_w — no action needed.
                print("natural fit")
                pass
            else:
                # Case 2: image doesn't fit remaining space at current render_w.
                # Calculate what width would be needed to fit the height into remaining.
                w_if_fitted = effective_remaining / img_ratio if img_ratio > 0 else effective_remaining
                if w_if_fitted / max_w_mm >= self.img_fit_ratio:
                    # Shrinking is acceptable — fit into remaining space.
                    print("fitting accepted")
                    render_w = w_if_fitted
                    render_h = effective_remaining
                else:
                # Shrinking would make image too small — page break and keep size.
                    print("page break keep size")
                    pdf.add_page()

            # Horizontal position
            if self.img_align == "center":
                x = self.margin + (self.available_width - render_w) / 2
            else:
                x = self.margin

            print(f"dbg: embed w={render_w:.1f} h={render_w*img_ratio:.1f}")
            # Disable auto page break for the image call: _embed_image manages
            # page placement manually above, and fpdf2's auto break fires on the
            # cursor-advancing ln() inside pdf.image(), which would push the cursor
            # to a new (blank) page even though the image itself fitted correctly.
            pdf.set_auto_page_break(auto=False)
            try:
                pdf.image(img_path, x=x, w=render_w)
            except ImportError:
                with open(img_path, 'rb') as f:
                    raw = f.read()
                    buf = self._strip_icc_profile(raw)
                    buf.seek(0)
                pdf.image(buf, x=x, w=render_w)
            finally:
                pdf.set_auto_page_break(auto=True, margin=self.margin + 8)
        except Exception as exc:
            print(f"[WARN] Could not embed image {img_path}: {exc}")
            self._draw_placeholder(pdf, label or img_path)

    def _draw_placeholder(self, pdf, label):
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

    def _render_mermaid(self, code, out_dir):
        import base64
        encoded  = base64.urlsafe_b64encode(code.encode("utf-8")).decode("ascii")
        base_url = self.mermaid_ink_url.rstrip("/")
        urls = [
            f"{base_url}/{encoded}?type=png",
            f"{base_url}/{encoded}",
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
        print("[WARN] All Mermaid URLs failed - applying on_missing_image policy.")
        return None


# ---------------------------------------------------------------------------
# FPDF subclass - header and footer
# ---------------------------------------------------------------------------

class _DocumentPDF(FPDF):
    def __init__(self, doc_title, margin, font_size, hf_font_size,
                 heading_color, body_font_name, **kwargs):
        super().__init__(**kwargs)
        self._doc_title          = doc_title
        self._margin             = margin
        self._font_size          = font_size
        self._hf_font_size       = hf_font_size
        self._heading_color      = heading_color
        self._body_font          = body_font_name
    def header(self):
        self.set_font(self._body_font, style="I", size=self._hf_font_size)
        r, g, b = self._heading_color
        self.set_text_color(r, g, b)
        self.set_y(4)
        self.cell(0, 8, self._doc_title, align="L")
        self.set_text_color(0, 0, 0)
        self.set_y(self._margin + 4)

    def footer(self):
        self.set_y(-self._hf_font_size-2)
        self.set_font(self._body_font, style="I", size=self._hf_font_size)
        self.set_text_color(120, 120, 120)
        self.cell(0, 6, f"Page {self.page_no()}", align="C")
        self.set_text_color(0, 0, 0)
