English | [简体中文](README.zh-CN.md)

# TeXDeck

<p align="center">
  <img src="docs/promo.webp" alt="TeXDeck: a PDF goes in, a Beamer deck comes out" width="100%">
</p>

Drop in a PDF, get back a Beamer deck: the compiled PDF, the `.tex` source, and a zip that opens in Overleaf.

<p align="center">
  <img src="docs/hero.png" alt="A source PDF page on the left, the generated Beamer slide on the right" width="100%">
</p>

A vision model reads your pages as images and writes a complete `main.tex` against the template you pick. It takes about a minute. You can then rewrite one slide, or the whole deck, with a single instruction. The source on the left is [`docs/demo/gold-price-2024.pdf`](docs/demo/gold-price-2024.pdf), so you can try it yourself. Slides follow the language of your material unless you pick English or Chinese.

**No API key needed:** it runs on the ChatGPT or Claude subscription you already pay for. An API key works too.

## Install

**Desktop app.** Download it from [Releases](https://github.com/HuanPP/TeXDeck/releases) (macOS Apple silicon, Windows, Linux). It bundles its own TeX engine, so you don't need a TeX install. The builds are unsigned: on macOS, right-click the app and choose Open; on Windows, choose More info, then Run anyway.

**Docker.**

```bash
git clone https://github.com/HuanPP/TeXDeck.git && cd TeXDeck
docker compose up --build        # then open http://localhost:8000
```

**From source.** You need Python 3.12+, Node 20+, and XeLaTeX or [Tectonic](https://tectonic-typesetting.github.io).

```bash
uv venv .venv --python 3.12 && uv pip install -e server --python .venv/bin/python
cp server/.env.example server/.env && ./server/run.sh     # API on :8000
cd web && npm install && npm run dev                      # UI on :5173
```

**On a server.** Set `TEXDECK_REGISTRATION=true` and sign up first with the invite code `hi_my_texdeck`; that account is the admin and hands out one-use invite codes, and every account connects its own ChatGPT, Claude or API key. Or add accounts on the server (`python -m app.auth add <username>`). Either way the page then asks for a sign-in and the API refuses requests without one. [`docs/deploy`](docs/deploy) has the full setup behind Caddy and Cloudflare, with example service files.

## Pick a model

Click the model button in the top bar. It lists three ways to run the model; you can switch between them, and pick the model for each, at any time:

**ChatGPT**, on your ChatGPT plan. Click Connect, sign in, then pick one of the models your account offers.

**Claude**, on your Claude plan. Needs [Claude Code](https://code.claude.com/docs/en/setup) installed on the computer running TeXDeck. If Claude Code is already signed in, Connect is all it takes. Otherwise TeXDeck starts `claude auth login`, shows the sign-in link, and you paste the code from that page back in.

**API key**, for any OpenAI-compatible vision model: enter the key, base URL and model name, e.g. `https://generativelanguage.googleapis.com/v1beta/openai/` with `gemini-pro-latest` (recommended; Kimi vision models also work). Text-only models won't work, because pages are sent as images.

With Claude, TeXDeck drives your own Claude Code CLI in headless mode (`claude -p`, no tools, CLAUDE.md and plugins off). The login belongs to Claude Code and TeXDeck never sees the credentials; switching away leaves it signed in. Pick Fable, Opus, Sonnet or Haiku in the panel (Sonnet by default); whether your plan includes a model is up to Anthropic, and Claude Code says so if it does not. If `ANTHROPIC_API_KEY` is set in the environment, Claude Code bills the API instead of your plan. The Docker image has no Claude Code, so Claude is for the desktop app and source installs.

The choice and any API key entered there are saved next to the other app data. `.env` (or `texdeck.env` in the desktop app's data folder) can still preset the API key with `TEXDECK_LLM_API_KEY`, `TEXDECK_LLM_BASE_URL` and `TEXDECK_LLM_MODEL`.

## Templates

<p align="center">
  <img src="docs/templates.png" alt="Title pages of the seven bundled templates" width="100%">
</p>

The bundle has seven templates: Academic, Emerald, Graphite, Navy, Scholar, SimplePlus and Execushares. All of them handle Chinese. Licenses are listed in [NOTICE](NOTICE).

To restyle one without touching LaTeX, click New template (or the palette icon on Academic, Emerald, Graphite, Navy or Scholar). The editor shows the sample deck recompiled as you change it. Pick the colours, upload a logo, and place any picture as the title-page background: move, scale, stretch and rotate it on the 16:9 page, with a blurred copy, a colour or the original background behind it. Saved templates are per account and only store the changes; the layout still comes from the built-in template.

There is no prompt library. Each template's `main.tex` is a complete example deck, and the model copies it. To make your own, copy `templates/academic`, change `key` and `name` in `template.json`, and edit `main.tex`. Real charts in the example mean real charts in the output.

## Good to know

If the model writes TeX that won't compile even after the automatic repair pass, the job fails and shows the real log. You never get a half-broken deck. Press Regenerate and it usually works. Slide counts are approximate. Input is limited to 10 PDFs and 200 pages in total.

```bash
cd server && ../.venv/bin/python -m pytest      # includes a real compile of every template
python desktop/build.py                         # build the desktop app for this machine
```

## License

The application code is MIT. Bundled themes keep their own licenses; see [NOTICE](NOTICE).
