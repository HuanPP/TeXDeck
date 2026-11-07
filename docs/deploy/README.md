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

## 2. An account

Sign-in turns on as soon as one account exists. Until then anyone who reaches the port can use the app.

```bash
cd /srv/texdeck/app && set -a && . ../texdeck.env && set +a
.venv/bin/python -m app.auth add <username>      # also: passwd, remove, list
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

The model connection belongs to the server, not to an account: whatever ChatGPT sign-in, Claude Code login or API key is connected there is what every signed-in user runs on. Keep accounts to people who may use it. On a remote server, finish the ChatGPT sign-in by pasting the address the browser ends on back into the panel. Claude Code works if the service runs as a user whose `claude` CLI is signed in.
