# MD to PDF Converter

This Python class was built for three reasons:

1. I needed a test project of a simple enough task that can be fully generated without creating a hot mess.
2. I had the annoying problem.that none of the md to pdf conversion methods to be found on google playstore for the phone worked with embedded images or mermaid diagrams.
3. I currently don't have the time for concentrated uninterrupted work on the notebook to develop a proper app, so PyDroid on the phone had to do.

**So as a fair warning:** I don't consider this production ready code, for a variety of reasons. The tool works just fine, but is intended for users who know what they are doing. No overwrite checks, only the most critical parts are reversed engineered, checking the output for rendering problems is highly recommended (although currently so far it works surprisingly well).

The methodology used is mainly single stepped agentic code emulation, no unit tests (pdfs need visual inspection as test anyways), short iterations, frequent session renewals (clean prompt).

I deviated from that once and got classical cat-herding issues with the model reintroducing fixed bugs and duct-tape fixing instead of clean problem solutions, so I rolled back to a known state and stuck to the short iterations.

The result is currently stable, performant and in use, overall a satisfying result for a script language.

I am considering to refactor to a proper config object instead of nested dictionaries, aware that this is "unpythonic" and a lot less robust as in strong typed languages, but I still see value in terms of intuitivity and usability. Opinions are welcome (as long as they are not zealous).

The [runner_template](runner_template.py) currently contains the configuration and usage example:

``` python

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
converter.convert("/storage/emulated/0/Documents/my_document.md")
```

Please see the [NOTICE](NOTICE.md) for code provenance concerns.