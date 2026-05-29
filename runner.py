"""
runner_template.py - configure this file for your paths and preferences.
This is the only file you need to edit; md_to_pdf.py stays untouched.

Updated: session 3 - MD2PdfConfig API, image fit-to-page, cfg shortcodes.
"""

from md_to_pdf import MD2PdfConfig, MDToPDFConverter

cfg = MD2PdfConfig()

cfg.page.setSize("A5").setMargins("small")

cfg.style.setFontSize(11).setHfFontSize(9).setCodeFontSize(9)
cfg.style.setHeadingColor(0, 0, 128)
cfg.style.setCodeBgColor(240, 240, 240)
cfg.style.setSyntaxHighlight(True)

# Uncomment to set explicit font paths; otherwise auto-detected from /system/fonts/
# cfg.style.setBodyFontPath("/system/fonts/Roboto-Regular.ttf")
# cfg.style.setCodeFontPath("/system/fonts/DroidSansMono.ttf")

cfg.image.setMaxWidthFraction(1.0)
cfg.image.setMaxHeightFraction(1.0)
cfg.image.setAlign("center")
cfg.image.setExpand(False)
cfg.image.setFitRatio(0.50)

cfg.setImageSearchPaths([
    "/storage/emulated/0/Download",
    "/storage/emulated/0/DCIM",
    "/storage/emulated/0/Pictures",
])

cfg.setOutputDir("/storage/emulated/0/Documents/PDF")
cfg.setOnMissingImage("placeholder")   # "prompt" | "skip" | "placeholder"

# Uncomment to use a self-hosted mermaid.ink instance:
# cfg.setMermaidInkUrl("https://mermaid.ink/img/")

converter = MDToPDFConverter(cfg)

# Change the path below to your Markdown file:
converter.convert("/storage/emulated/0/Documents/markor/gpt_limitations_transcript.md")
converter.convert("/storage/emulated/0/Python/MD2PDF/test_output/mermaid.md")
