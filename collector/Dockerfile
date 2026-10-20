# The elastic-evals OpenTelemetry Collector distribution (gateway and ingest use the same binary).
ARG REGISTRY=mirror.gcr.io/library
FROM ${REGISTRY}/golang:1.26 AS build
WORKDIR /src
# behind a TLS-intercepting proxy: docker build --secret id=extra_ca,src=/path/to/ca.crt ...
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then cp /run/secrets/extra_ca /usr/local/share/ca-certificates/extra.crt \
      && update-ca-certificates >/dev/null; fi
RUN go install go.opentelemetry.io/collector/cmd/builder@v0.163.0
COPY . .
RUN CGO_ENABLED=0 builder --config builder-config.yaml

FROM gcr.io/distroless/static-debian12
COPY --from=build /src/_build/eecol /eecol
USER 10001
ENTRYPOINT ["/eecol"]
