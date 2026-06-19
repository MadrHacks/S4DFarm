# The pinned C-extension deps (greenlet, gevent, psycopg2, cryptography, hiredis,
# cffi) ship no musllinux wheels, so a builder compiles them once against musl and
# only the result is copied into a small alpine runtime (~50MB vs ~150MB on slim).
FROM python:3.9-alpine AS build
RUN apk add --no-cache build-base postgresql-dev libffi-dev openssl-dev linux-headers
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.9-alpine
# Runtime libraries the compiled extensions link against, plus bash for the
# entrypoint.
RUN apk add --no-cache libpq libffi libstdc++ openssl bash
COPY --from=build /install /usr/local

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    FARM_DATA=/tmp

WORKDIR /app
# Bake the app so the image is self-contained for GHCR/production (the dev compose
# can still shadow /app with a bind mount).
COPY ./app /app

COPY ./docker/celery/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
