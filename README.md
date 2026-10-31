# Stykhiia Text

A glyphic sans serif with flared stroke endings for the weather website **Стихія (Stykhiia)**.
Шрифт з розширеними кінцями штрихів для погодного сайту «Стихія».

- Coverage: GF Latin Core, GF Cyrillic Core, Latin Extended-A (494 glyphs)
- Features: kern, mark/mkmk, ccmp (soft-dotted i/j), tnum (tabular figures)
- Weather icons (PUA U+E000–E004) are NOT included in the Google Fonts build; build the site version with `--with-icons`
- License: SIL Open Font License 1.1 (see OFL.txt)

## Building

    pip install -r requirements.txt
    cd sources/generator
    python build_ufo.py ../../build       # Google Fonts version (no PUA icons); add --with-icons for the site version
    # writes the UFO, builds TTF with fontmake, autohints, makes WOFF2

`sources/StykhiiaText.ufo` is the editable source (cubic Bézier outlines, components and anchors).
The Python generator in `sources/generator` is how the UFO was produced.

## Quality

- FontBakery (googlefonts profile): 0 FAIL, 0 ERROR — see `documentation/fontbakery-report.md`
- Uniqueness vs 87 popular fonts (skeleton comparison): see `documentation/uniqueness-report.txt`

