# ---- Stage 1: build the web UI ----
FROM node:20-slim AS web
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---- Stage 2: runtime — Python + XeLaTeX ----
FROM python:3.12-slim-bookworm

# TeX Live with XeLaTeX plus everything the bundled templates load, and
# Noto CJK so Chinese decks come out in Source Han faces rather than Fandol.
RUN apt-get update && apt-get install -y --no-install-recommends \
        texlive-xetex texlive-latex-recommended texlive-latex-extra \
        texlive-pictures texlive-fonts-extra texlive-lang-chinese \
        tex-gyre fonts-noto-cjk fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/texdeck
COPY server/pyproject.toml server/pyproject.toml
COPY server/app server/app
RUN pip install --no-cache-dir ./server
COPY templates templates
COPY --from=web /build/dist web/dist

# Bake-time smoke test: one bundled template through the real toolchain, so a
# broken TeX package set fails the build instead of the first user request.
RUN cd templates/navy \
    && xelatex -interaction=nonstopmode -halt-on-error main.tex >/dev/null \
    && xelatex -interaction=nonstopmode -halt-on-error main.tex >/dev/null \
    && rm -f main.aux main.log main.nav main.out main.pdf main.snm main.toc main.synctex.gz

ENV TEXDECK_ARTIFACT_ROOT=/data/artifacts \
    TEXDECK_CHATGPT_AUTH_FILE=/data/state/chatgpt-auth.json \
    TEXDECK_MODEL_STATE_FILE=/data/state/model-state.json \
    TEXDECK_MEMORY_FILE=/data/state/memory.json \
    TEXDECK_USERS_FILE=/data/state/users.json \
    TEXDECK_CUSTOM_TEMPLATES_DIR=/data/state/custom-templates
VOLUME /data/artifacts /data/state
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--app-dir", "server", "--host", "0.0.0.0", "--port", "8000"]
