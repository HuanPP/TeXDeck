English | [简体中文](README.zh-CN.md)

# TeXDeck

<p align="center">
  <img src="docs/social-preview.png" alt="TeXDeck — point it at a PDF, get back a finished Beamer deck" width="100%">
</p>

Group meetings, conference talks, thesis proposals, project updates. Point it at a quarterly report instead and it won't complain, but slides for research work are what it was built around.

<p align="center">
  <img src="docs/hero.png" alt="A source PDF page on the left, the generated Beamer slide on the right" width="100%">
</p>

The document on the left of that picture is [`docs/demo/gold-price-2024.pdf`](docs/demo/gold-price-2024.pdf), shipped with the repo so you can run the same thing yourself.

You upload a paper — or several PDFs that read in order as one pack — or just paste text, pick a template, and a vision model reads the pages and writes a complete `main.tex` against that template. XeLaTeX compiles it. About thirty to sixty seconds later you have a PDF, the `.tex` behind it, and a zip you can drop straight into Overleaf. Slides come back in English whatever language the source is in; ask for another language in the instructions and you get that instead.

## Seven templates

<p align="center">
  <img src="docs/templates.png" alt="Title pages of the seven bundled templates" width="100%">
</p>

| Key | Name | Origin | License |
|---|---|---|---|
| `academic` | Academic (Palatino) | collegeBeamer derivative | CC BY 4.0 |
| `emerald` | Emerald | collegeBeamer derivative | CC BY 4.0 |
| `graphite` | Graphite | collegeBeamer derivative | CC BY 4.0 |
| `navy` | Navy | collegeBeamer derivative | CC BY 4.0 |
| `scholar` | Scholar | collegeBeamer derivative | CC BY 4.0 |
| `simpleplus` | SimplePlus | [pm25/SimplePlus-BeamerTheme](https://github.com/pm25/SimplePlus-BeamerTheme) | Unlicense |
| `execushares` | Execushares | [hamaluik/Beamer-Theme-Execushares](https://github.com/hamaluik/Beamer-Theme-Execushares) | MIT |

The collegeBeamer packs share an ancestor but are distinct families now: Academic is a university-press book plate (centered small caps between double hairlines, roman-numeral dividers), Emerald is editorial (display serif over flowing curves, full-bleed part pages with ghost numerals), Graphite is Swiss typographic (sans-first Heros, hard grid, monochrome charts), Navy is a boardroom deck (banded white-on-navy headers, split-panel agendas, a footer progress line), and Scholar keeps the line's original composition — a full-page engraved plate beside the title block. SimplePlus and Execushares keep their own upstream identities. All seven compile with XeLaTeX on a plain TeX Live install with no extra fonts registered — the faces each theme needs ship inside its pack or resolve from TeX Live by name. Chinese input works in every pack. None of them ship anyone's logo — the images under `src/brand/` are neutral placeholders meant to be replaced.

## The template file is the prompt

This is the one idea the project is built on, and it is worth stating plainly because it explains most of the code that isn't here.

Every template directory contains a `main.tex` that is a complete, compilable presentation: a title page, a table of contents, worked examples of each layout, real `pgfplots` charts, and a comment block at the top addressed to the model. That file is handed to the model verbatim. There is no rule sheet next to it, no style DSL, no prompt library. The model is shown a deck that looks right and asked to write another one.

This has a consequence worth knowing before you edit a template. When the layout examples contained an empty rectangle captioned "place a chart here", every generated deck came back with an empty rectangle captioned with the topic. When one layout put text where a figure belonged, the model invented an `\includegraphics` pointing at a file that did not exist. Neither was solved by adding instructions; both were solved by making the example better. The model is not disobeying — it is copying, accurately, whatever it was shown. Put a real chart in your template and you get real charts back.

An earlier version of this idea went the other way. It grew to about 31,000 lines, 82 routes and a five-stage pipeline that summarised the PDF, then summarised the summary, then truncated the result to fit a prompt budget. What actually reached the model was around 2.2 KB of text per document. It scored well on its own metrics and produced decks nobody wanted. TeXDeck is the reaction: keep the handoff to the model honest, and keep the code small enough that the handoff is still the whole product.

Two smaller rules follow from the same instinct. Source pages go to the model as images, with no intermediate summary and no silent truncation. And when something fails — the model returns unusable TeX, or XeLaTeX still fails after its repair attempts — the job fails with the real error. There is no fallback deck.

## Getting started

### Desktop app

Download the build for your system from [Releases](https://github.com/HuanPP/TeXDeck/releases): a `.dmg` for Apple-silicon Macs, a setup `.exe` for Windows, a `.tar.gz` for Linux. It carries its own TeX engine ([Tectonic](https://tectonic-typesetting.github.io)), so there is no TeX Live to install. The first deck takes a minute or two longer than the rest, because Tectonic downloads the LaTeX packages it needs once and caches them.

Open it and click **Sign in with ChatGPT** in the top bar, or put an API key in `texdeck.env` in the data folder and restart. The data folder is `~/Library/Application Support/TeXDeck` on macOS, `%APPDATA%\TeXDeck` on Windows and `~/.local/share/TeXDeck` on Linux; finished decks are kept there too.

The builds are not code-signed. On macOS the first launch is blocked: right-click the app and choose Open, or allow it under System Settings > Privacy & Security. On Windows, SmartScreen asks once; choose More info, then Run anyway. The Linux build has no window of its own: run `./TeXDeck` from a terminal, it opens in your browser, and Ctrl+C stops it.

### Docker

Nothing to install but Docker and a key:

```bash
git clone https://github.com/HuanPP/TeXDeck.git && cd TeXDeck
cat > .env <<'ENV'
TEXDECK_LLM_API_KEY=your-key-here
# Any OpenAI-compatible vision endpoint works; these are the defaults:
TEXDECK_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
TEXDECK_LLM_MODEL=gemini-pro-latest
ENV
docker compose up --build
```

The first build downloads TeX Live, so it takes a while; after that, open http://localhost:8000. Finished decks land in `./artifacts`. The image bundles Noto CJK, so Chinese output uses Source Han faces out of the box. The key is optional if you sign in with ChatGPT instead (see below); the sign-in is kept in `./state`.

### Running from source

You need Python 3.12+, Node 20+, a TeX engine, and either an API key for a vision-capable model or a ChatGPT plan. For the engine, a TeX Live install with XeLaTeX is used when `xelatex` is on your PATH; otherwise TeXDeck falls back to [Tectonic](https://tectonic-typesetting.github.io) if that is installed. `TEXDECK_TEX_ENGINE` picks one explicitly.

```bash
git clone https://github.com/HuanPP/TeXDeck.git && cd TeXDeck

uv venv .venv --python 3.12
uv pip install -e server --python .venv/bin/python

cp server/.env.example server/.env   # then set TEXDECK_LLM_API_KEY
./server/run.sh                      # API on :8000
```

In a second shell:

```bash
cd web && npm install && npm run dev  # UI on :5173
```

<p align="center">
  <img src="docs/ui.png" alt="The TeXDeck web interface" width="100%">
</p>

The model has to be able to read images — PDF pages are sent as page renders, not as extracted text. Anything with an OpenAI-compatible endpoint works; point `TEXDECK_LLM_BASE_URL` at it.

Gemini is the recommendation if you have a key. Its multimodal reading is the best of the options here, and it handles a long run of page images without losing the thread:

```
TEXDECK_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
TEXDECK_LLM_MODEL=gemini-pro-latest
```

`gemini-pro-latest` is an alias that tracks Google's current Pro model, so this keeps working as they ship new ones. Pin a specific version instead (`gemini-2.5-pro`, `gemini-3.1-pro-preview`) if you would rather it never change under you.

Moonshot's Kimi works too, and is what this was developed against:

```
TEXDECK_LLM_BASE_URL=https://api.moonshot.cn/v1
TEXDECK_LLM_MODEL=moonshot-v1-128k-vision-preview
```

Two things that will not work: text-only models, and the Moonshot `kimi-k2.6` / `kimi-k3` families, which reject every temperature except 1 and so refuse the calls this makes.

### Signing in with ChatGPT

A ChatGPT plan works in place of an API key. Click **Sign in with ChatGPT** in the top bar and approve TeXDeck on OpenAI's page; from then on generation runs on your plan, with `gpt-6.1-sol` unless you set `TEXDECK_CHATGPT_MODEL`. This is OpenAI's token-sharing sign-in for third-party apps, the same flow opencode uses. The tokens sit in one file (`server/.chatgpt-auth.json` from source, the data folder in the desktop app, `./state` under Docker) and go nowhere but OpenAI.

When the browser and TeXDeck are on different machines, or TeXDeck runs in Docker, OpenAI's page finishes on an address that will not load. Copy that address from the browser's address bar into the box TeXDeck shows and the sign-in completes.

With both a sign-in and a key configured, the sign-in wins; `TEXDECK_LLM_PROVIDER=api` prefers the key. The sign-in is for your own use, so do not leave a server signed in to your plan where other people can reach it.

## Making it yours

Copy `templates/academic` to `templates/mylab`, change the `key` and `name` in its `template.json`, and drop your own logo and background into `src/brand/` over the three placeholder files. Restart the server and your template is in the picker.

If you want it to look like something else entirely, edit `main.tex` — the layouts, the colours, the examples, the comment block at the top telling the model what to do. Whatever you put in that file is what the model will imitate, so it is worth making the examples good. That is the whole extension mechanism; there is nothing else to learn.

## Things worth knowing

Generation can fail. The model occasionally writes LaTeX that will not compile — a table row with one cell too many is the classic — and while there is an automatic repair pass, it does not always land. When that happens you get the error and the compile log rather than a half-broken deck. Running it again usually works — the Regenerate button re-runs the same inputs in one click. Better models fail less.

Editing comes in two sizes. Per-slide edit rewrites one frame from one instruction. Deck-wide refine takes one instruction against the whole deck — "cut it to ten slides", "make the charts bigger", "switch it to Chinese" — through the same validate-compile-repair machinery, and rolls back if the result will not build.

Slide count is a request, not a guarantee. Ask for eight and you will often get ten.

PDFs are capped at 200 pages combined, up to ten files per run. Past 40, the model is shown thumbnails of everything first and asked which pages carry the argument; only those are sent at full resolution.

Per-slide editing maps a PDF page back to its `\begin{frame}` by reading the synctex file the engine writes next to the PDF. No separate `synctex` tool is needed, so it works the same under TeX Live and Tectonic.

## Tests

```bash
cd server && ../.venv/bin/python -m pytest      # includes a real XeLaTeX compile of every template
cd server && ../.venv/bin/python -m pytest -m "not slow"
cd server && TEXDECK_TEX_ENGINE=tectonic ../.venv/bin/python -m pytest -m slow   # same, through Tectonic
```

The compile suite is the one worth keeping. It renders every bundled template through the real toolchain, which is the only check that catches a template that has quietly stopped working.

## Building the desktop app

```bash
cd web && npm ci && npm run build && cd ..
python -m pip install ./server pyinstaller pywebview
python desktop/build.py
```

That freezes the app for the machine you run it on, has the frozen app render a template preview through its bundled Tectonic as a smoke test, and writes the package to `desktop/dist/release/`. Pushing a `v*` tag runs the same thing on macOS, Windows and Linux in GitHub Actions and attaches the results to a release.

## License

MIT for the application code. The bundled themes carry their own licenses and attribution — see [NOTICE](NOTICE) before redistributing.
