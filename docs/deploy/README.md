# Running TeXDeck on a server

This is the setup behind a personal TeXDeck on a Linux VM: the app on localhost, Caddy in front for TLS, and Cloudflare in front of Caddy. Swap `texdeck.example.com`, the paths and the ports for your own.

```
browser ── HTTPS ──> Cloudflare (proxied, Origin Rule: port 2083) ──> Caddy :2083 ──> uvicorn 127.0.0.1:8790
```

## 1. App

```bash
git clone https://github.com/HuanPP/TeXDeck.git /srv/texdeck/app && cd /srv/texdeck/app
uv venv .venv --python 3.12 && uv pip install -e server --python .venv/bin/python
(cd web && npm ci --include=dev && npm run build)
```

TeX: install XeLaTeX, or download the [Tectonic](https://github.com/tectonic-typesetting/tectonic/releases) binary and point `TEXDECK_TECTONIC_COMMAND` at it. Tectonic fetches packages on its first compile, so the first deck takes a few minutes.

Put settings in [`texdeck.env`](texdeck.env). Keep the data directory outside the code so redeploys leave it alone.

## 2. Accounts

To let people sign up with invite codes, set `TEXDECK_REGISTRATION=true` in `texdeck.env`. Sign-in is then required from the start. The first person to sign up uses the code `hi_my_texdeck`, which stops working once used, and becomes the admin; the admin hands out one-use codes from the Invites button at the top of the page (or `python -m app.auth invite` on the server). Do the first sign-up yourself right after starting the service.

Without registration, sign-in turns on as soon as one account exists, and accounts are made on the server. Until then anyone who reaches the port can use the app.

```bash
cd /srv/texdeck/app && set -a && . ../texdeck.env && set +a
.venv/bin/python -m app.auth add <username>      # also: passwd, remove, invite, list
```

## 3. Services

[`texdeck.service`](texdeck.service) runs the app and [`texdeck-caddy.service`](texdeck-caddy.service) runs Caddy with [`Caddyfile`](Caddyfile), both as systemd user services:

```bash
cp texdeck.service texdeck-caddy.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now texdeck texdeck-caddy
loginctl enable-linger "$USER"     # keep them running after you log out
```

## 4. Cloudflare

1. DNS: an `A` record for `texdeck` pointing at the server, proxied.
2. Rules, Origin Rules: when the hostname equals `texdeck.example.com`, rewrite the destination port to `2083`.
3. SSL/TLS: mode Full (strict). Under Origin Server, create an origin certificate for the hostname and save it as the `tls` files in the Caddyfile.
4. On the server's firewall, open `2083` to [Cloudflare's IP ranges](https://www.cloudflare.com/ips/) only.

Cloudflare keeps WebSockets open (generation progress uses one) and limits uploads to 100 MB on the free plan, which matches TeXDeck's own limit.

## Notes

Each account connects its own model and sees only its own decks: its ChatGPT sign-in, Claude sign-in and API key are kept in `accounts/<name>/` next to the users file. An API key in `texdeck.env` is not shared with accounts. Claude needs the `claude` CLI installed on the server; each account signs in to its own Claude (the CLI keeps that login under `accounts/<name>/claude`). On a remote server, finish the ChatGPT sign-in by pasting the address the browser ends on back into the panel. Decks made before accounts existed belong to the admin (the first account).
