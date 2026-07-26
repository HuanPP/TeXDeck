# TeXDeck Templates

Six Beamer packs ship with TeXDeck. Each is a directory under `templates/` with
at least `template.json` and `main.tex`.

| key | name | lineage | license |
|-----|------|---------|---------|
| `academic` | Academic (Palatino) | collegeBeamer derivative | CC-BY-4.0 |
| `emerald` | Emerald | collegeBeamer derivative | CC-BY-4.0 |
| `graphite` | Graphite | collegeBeamer derivative | CC-BY-4.0 |
| `navy` | Navy | collegeBeamer derivative | CC-BY-4.0 |
| `scholar` | Scholar | collegeBeamer derivative (upstream layout) | CC-BY-4.0 |
| `simpleplus` | SimplePlus | [pm25/SimplePlus-BeamerTheme](https://github.com/pm25/SimplePlus-BeamerTheme) | Unlicense |
| `execushares` | Execushares | [hamaluik/Beamer-Theme-Execushares](https://github.com/hamaluik/Beamer-Theme-Execushares) | MIT |

`academic` / `emerald` / `graphite` / `navy` / `scholar` share one ancestor
([college-beamer](https://github.com/liu-qilong/college-beamer) by Qilong Liu,
upstream SINTEF Presentation by Federico Zenith). Four have been redesigned
into their own families: book-plate scholarly (academic), editorial serif
(emerald), Swiss typographic (graphite), and banded boardroom (navy);
`scholar` keeps the line's original composition — a full-page plate
illustration beside the title block — with neutral placeholder art. The
shared ancestry survives in `collegebeamer.sty` plumbing and licensing, not
in how the decks look.

These packs ship **neutral placeholder brand assets only** where a brand path
is used. They do **not** contain any real institutional logos, trademarks, or
commercial background artwork. Full attribution is in the repository root
`NOTICE` file.

## Layout of a pack

```
templates/<key>/
  template.json     # {"key": "...", "name": "..."}
  main.tex          # sample deck; top comment block = full model brief
  *.sty             # theme / style files loaded by main.tex
  src/              # optional assets
  font/             # optional fonts
```

The comment block at the top of `main.tex` is the entire instruction set given
to the model. There is no separate prompt file.

## Replacing the placeholders with your own brand

collegeBeamer-derived packs (`academic`, `emerald`, `graphite`, `navy`) each
have a `src/brand/` directory with three files:

| File | Role |
|------|------|
| `color-logo.png` | Logo used on light / white slides |
| `trans-logo.png` | Logo used on dark / colored slides (transparent background) |
| `background.png` | Title-page background image (16:9) |

To use your own identity, replace those three files with your own images under
the **same file names**. Keep reasonable sizes (a few hundred KB total is fine).

No other path changes are required: each pack's `collegebeamer.sty` already
points at `src/brand/`.

`simpleplus` and `execushares` do not use `src/brand/` logos; brand them by
editing the theme / `main.tex` as needed.

## Adding a new pack

1. Create `templates/<your-key>/` with `template.json`, a compilable `main.tex`,
   and every `.sty` the sample loads.
2. Put model-facing rules in the top comment block of `main.tex`.
3. Compile by hand with `xelatex` in that directory before relying on generation.
4. Restart the backend; the pack appears on `GET /api/templates`.

See the repository root README for a fuller write-up.

## License note

- `collegebeamer.sty` packs: derivatives of
  [college-beamer](https://github.com/liu-qilong/college-beamer) (CC BY 4.0),
  itself based on Federico Zenith's SINTEF Presentation (CC BY 4.0).
- `simpleplus/`: Unlicense; original text in `simpleplus/LICENSE`.
- `execushares/`: MIT, Copyright (c) 2014 Kenton Hamaluik; original text in
  `execushares/LICENSE`. TeXDeck's only change is loading Source Pro fonts by
  file name via fontspec so a plain TeX Live install can compile the theme;
  font files are not redistributed here.

See the repository root `NOTICE` file for full attribution and modification notes.
