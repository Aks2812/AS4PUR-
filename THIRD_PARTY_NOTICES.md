# Third-party notices

AS4PUR itself is licensed under the MIT licence (see `LICENSE`). The files listed here are bundled with it and
come from other projects. For each one: what it is, where it came from, the licence, and the evidence for that,
found in the file itself or checked against the project it came from.

Only files with evidence for their licence are listed. This list is not a legal opinion.

## Fonts: Plus Jakarta Sans

| | |
|---|---|
| Files | `app/static/fonts/PlusJakartaSans-latin.woff2` (variable font, Latin subset) and `app/static/fonts/PlusJakartaSans-Italic-latin.woff2` (Medium Italic, static, Latin subset) |
| Licence | SIL Open Font License, Version 1.1 |
| Licence text | `app/static/fonts/OFL.txt`, stored next to the fonts |
| Source | https://github.com/tokotype/PlusJakartaSans |

Evidence:

- Copyright string in the `name` table of both font files: "Copyright 2020 The Plus Jakarta Sans Project Authors (https://github.com/tokotype/PlusJakartaSans)".
- Licence URL in the `name` table of both font files (name ID 14): https://scripts.sil.org/OFL.
- Version string in both files: "Version 2.071;gftools[0.9.30]".
- `app/static/fonts/OFL.txt` is the licence file published for this font in the font project's own repository and in the
  Google Fonts repository (`ofl/plusjakartasans/OFL.txt`); the two copies are byte-identical, and its first line matches the
  copyright string in the font files.
- `app/static/css/style.css` (header comment) says both files were taken from Google's font service, Latin subset only.

## Flag images

| | |
|---|---|
| Files | `app/static/icons/flags/gb.svg`, `app/static/icons/flags/id.svg` |
| Licence | MIT |
| Source | flag-icons, https://github.com/lipis/flag-icons (the `flags/4x3` set) |

Evidence:

- Each file's root element carries the attribute `id="flag-icons-<code>"`.
- Both files are byte-for-byte identical to `flags/4x3/gb.svg` and `flags/4x3/id.svg` in the flag-icons repository.
- `app/templates/landing.html` (comment) names flag-icons and its MIT licence.
- The licence text below is the flag-icons repository's `LICENSE` file.

<details>
<summary>flag-icons licence text</summary>

```
The MIT License (MIT)

Copyright (c) 2013 Panayiotis Lipiridis

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

</details>

## Icons

| | |
|---|---|
| Where | inline SVG path data in `app/templates/partials/icons.html` (the `ti_icon` macro), 17 icons |
| Licence | MIT |
| Source | Tabler Icons, outline set, https://tabler.io/icons (https://github.com/tabler/tabler-icons) |

Evidence:

- The comment above the macro in `icons.html` names Tabler Icons and the MIT licence.
- All 17 icons have path data identical, character for character, to the outline icon of the same name in a published Tabler Icons release
  (`info` is `info-circle` upstream). Releases compared: v3.35.0, v3.36.0, v3.36.1, v3.37.0 and v3.48.0 (the latest when checked, 2026-10-05).
- 16 of the 17 are identical in v3.36.1, v3.37.0 and v3.48.0. `apps` is identical to v3.35.0 and v3.36.0 only: Tabler rewrote the path data of
  many icons in v3.36.1 ("icon optimizations"), and `apps` still has the earlier text.
- The attributes the macro puts on each icon (`viewBox="0 0 24 24"`, `fill="none"`, `stroke="currentColor"`, `stroke-width="2"`, round line caps and joins)
  are the ones Tabler's own outline SVG files carry.
- The licence text below is the Tabler Icons repository's `LICENSE` file.

<details>
<summary>Tabler Icons licence text</summary>

```
MIT License

Copyright (c) 2020-2026 Paweł Kuna

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

</details>
