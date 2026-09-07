FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY jobbot ./jobbot
RUN pip install -r requirements.lock && pip install --no-deps . \
    && useradd --create-home --uid 10001 jobbot \
    && mkdir /app/data && chown jobbot:jobbot /app/data
COPY config ./config
USER jobbot
CMD ["jobbot", "run"]
