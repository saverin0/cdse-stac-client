# The client: migrate, fetch, load, validate. Python 3.13.15 is the version the code was tested on.
# The base image is pinned by its content digest, so a re-published tag cannot change what is built.
# To move to a newer base: docker buildx imagetools inspect python:3.13.15-slim, then copy the Digest line.
FROM python:3.13.15-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Dependencies first, so a code change does not reinstall them. Every package is pinned and
# checked against its sha256 hash; only what the program needs, no test tools. Downloads are kept in a
# build cache outside the image, so a changed lock does not download everything again.
COPY requirements.lock .
RUN --mount=type=cache,target=/root/.cache/pip pip install --require-hashes -r requirements.lock

COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY migrations ./migrations
COPY sql ./sql
COPY config ./config

# Run as an ordinary user. Only the run folder is writable.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data/runs \
    && chown -R app:app /app/data
USER app

ENTRYPOINT ["python", "-m", "cdse_stac"]
CMD ["--help"]


# The same image plus the test tools and the tests: docker compose run --rm tests
FROM runtime AS test
USER root
COPY requirements.txt requirements-dev.txt requirements-dev.lock ./
RUN --mount=type=cache,target=/root/.cache/pip pip install --require-hashes -r requirements-dev.lock
COPY tests ./tests
USER app
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q", "-rs", "-p", "no:cacheprovider"]
