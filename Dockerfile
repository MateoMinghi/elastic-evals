# elastic-evals Python image: API + UI, tracker, workers, CLI.
#   docker build -t elastic-evals .                         # with local heads (CPU torch)
#   docker build --build-arg EXTRAS=service,otel,sentry .   # API/tracker only, no local models
ARG REGISTRY=mirror.gcr.io/library

FROM ${REGISTRY}/node:22-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
# behind a TLS-intercepting proxy: docker build --secret id=extra_ca,src=/path/to/ca.crt ...
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi; \
    npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

FROM ${REGISTRY}/python:3.13-slim
ARG EXTRAS=service,otel,sentry,local
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 EE_UI_DIR=/app/ui/dist HF_HOME=/models
WORKDIR /app
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then cp /run/secrets/extra_ca /usr/local/share/ca-certificates/extra.crt \
      && update-ca-certificates >/dev/null; fi
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
RUN if echo "$EXTRAS" | grep -q local; then \
      pip install --index-url https://download.pytorch.org/whl/cpu torch; fi
COPY pyproject.toml README.md ./
COPY elastic_evals/ elastic_evals/
RUN pip install ".[${EXTRAS}]"
# bake the default encoder into the image so workers start without network access
RUN if echo "$EXTRAS" | grep -q local; then \
      python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('intfloat/multilingual-e5-small')"; fi
COPY --from=ui /ui/dist /app/ui/dist
COPY examples/ examples/
RUN useradd -r -u 10001 ee && mkdir -p /artifacts && chown ee /artifacts /models -R
USER ee
EXPOSE 8000
ENTRYPOINT ["python", "-m", "elastic_evals"]
CMD ["serve", "--port", "8000"]
