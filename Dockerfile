FROM python:3.13.2-alpine

WORKDIR /app

COPY poetry.lock pyproject.toml /app/
COPY src/ /app/src

RUN apk update \
    && apk upgrade --no-cache \
    && apk add --no-cache --virtual build-dependencies build-base curl \
    && pip install --no-cache-dir --upgrade pip setuptools wheel \
    && curl -sSL https://install.python-poetry.org | POETRY_HOME=/etc/poetry python - \
    && ln -s /etc/poetry/bin/poetry /usr/local/bin/poetry \
    && poetry run pip install --upgrade pip setuptools wheel \
    && MAKEFLAGS="-j" poetry install \
    && poetry run python -m compileall -j 0 src \
    && rm -rf /root/.cache/pip \
    && rm -rf /root/.cache/pypoetry/artifacts /root/.cache/pypoetry/cache \
    && rm -rf /etc/poetry/lib/poetry/_vendor/py3.13 \
    && apk del --no-cache build-dependencies

ENTRYPOINT ["poetry", "run", "sanitizer"]
