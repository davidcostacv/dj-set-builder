# djset, hosted — step W6.
#
# One process, one worker, one SQLite file on a mounted volume. That is not a
# simplification to be undone later: the job runner holds a single slot in
# process memory and the pending OAuth flows live there too, so a second worker
# would run a second enrichment against the same database and lose every
# callback that landed on the other process. Scaling this app means a bigger
# box, not more processes.

FROM python:3.13-slim AS base

# libsndfile is what soundfile links against, and the DSP source needs it to
# decode preview MP3s. Without it enrichment still runs, but --dsp silently
# resolves nothing, which is the worst way to find out a dependency is missing.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a source edit does not reinstall librosa every build.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir . \
 && pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30" librosa

# The library lives on a volume mounted here. Without one, the database is
# recreated empty on every deploy and a ten-hour enrichment is thrown away.
ENV DJSET_DB_PATH=/data/djset.sqlite3
VOLUME ["/data"]

# Hosting platforms hand the port over at runtime.
ENV PORT=8000
EXPOSE 8000

# --proxy-headers because the platform terminates TLS: without it the app sees
# a plain-http request to an internal address, derives that as its OAuth
# redirect URI, and Spotify refuses it. forwarded-allow-ips is '*' because the
# proxy's address is not knowable here — the container must not be reachable
# except through that proxy.
CMD ["sh", "-c", "exec djset serve --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips '*'"]
