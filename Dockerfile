# Fuseshot: finds the panoramas and bursts in your photo library and stitches them.
# Stitching tools (Hugin, enblend) are included; splats need COLMAP and Brush on a GPU host.
FROM python:3.12-slim-trixie

ARG EXTRAS=""

RUN apt-get update \
 && apt-get install -y --no-install-recommends hugin-tools enblend enfuse \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY.md ./
COPY fuseshot ./fuseshot
RUN uv sync --frozen --no-dev --no-editable $(for e in $EXTRAS; do printf -- '--extra %s ' "$e"; done) \
 && rm -rf /root/.cache

ENV PATH="/app/.venv/bin:$PATH" \
    FUSESHOT_HOME=/data \
    FUSESHOT_REVIEW_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765

RUN useradd --uid 1000 --create-home fuseshot && mkdir -p /data && chown fuseshot /data
USER fuseshot

ENTRYPOINT ["fuseshot"]
CMD ["review"]
