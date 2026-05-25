"""
runner_template.py — configure this file for your paths and preferences.
This is the only file you need to edit; md_to_pdf.py stays untouched.

Updated: session 2 — documents all current config keys.
"""

from md_to_pdf import MDToPDFConverter

config = {
    "page": {
        "size": "A5",         # "A4" or "A5"
        "margins": "small",  # "small" (10mm), "normal" (20mm), "wide" (30mm)
    },

    "image_search_paths": [
        "/storage/emulated/0/Download",
        "/storage/emulated/0/DCIM",
        "/storage/emulated/0/Pictures",
    ],

    "output_dir": "/storage/emulated/0/Documents/PDF",

    "on_missing_image": "placeholder",  # "prompt" | "skip" | "placeholder"

    "style": {
    	"hf_font_size": 9,
        "font_size": 12,
        "code_font_size": 9,
        "heading_color": (0, 0, 128),       # RGB — dark blue default
        "code_bg_color": (240, 240, 240),   # RGB — light grey

        # TTF font paths — auto-detected from /system/fonts/ if omitted.
        # Roboto and DroidSansMono are the auto-detected defaults on Android.
        # Uncomment and set explicitly if you want a different font:
        # "body_font_path": "/system/fonts/SourceSansPro-Regular.ttf",
        # "code_font_path": "/system/fonts/DroidSansMono.ttf",

        "syntax_highlight": True,   # requires: pip install pygments
    },

    "mermaid_ink_url": "https://mermaid.ink/img/",
}

converter = MDToPDFConverter(config)

# Change the path below to your Markdown file:
converter.convert("/storage/emulated/0/Documents/markor/gpt_limitations_transcript.md")
