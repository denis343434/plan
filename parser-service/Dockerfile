FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# libatomic1 нужен самому node-драйверу Playwright: на Debian 13 (trixie), который теперь
# лежит в основе python:3.12-slim, его в образе нет, и `playwright install` падает с
# "error while loading shared libraries: libatomic.so.1" ещё до того, как сможет
# доставить свои системные зависимости через --with-deps.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*
RUN playwright install --with-deps chromium

COPY . .
RUN chmod +x scripts/entrypoint.sh

ENTRYPOINT ["scripts/entrypoint.sh"]
