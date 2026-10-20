#!/usr/bin/env bash
# Minimal errors-only self-hosted Sentry 25.7.0 for the phase-5 proof of concept (DESIGN.md §12),
# next to the elastic-evals dev stack: it reuses the local Postgres (database "sentry") and Kafka
# (localhost:9092), and runs every service with host networking:
#   web :9100   relay :3001   snuba api :1218   clickhouse :8123/:9000   redis :6379
# Writes the DSN, tokens and integration ids that experiments/sentry_poc.py reads to $OUT.
#
#   deploy/sentry-poc/setup.sh [out.json]       then   python experiments/sentry_poc.py --sentry out.json
#
# Not for production: use getsentry/self-hosted (it puts nginx in front of web, see below).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=${EE_SENTRY_POC_DIR:-$HOME/sentry-poc}
OUT=${1:-$WORK/sentry_poc.json}
SENTRY=getsentry/sentry:25.7.0 SNUBA=getsentry/snuba:25.7.0 RELAY=getsentry/relay:25.7.0
CH=altinity/clickhouse-server:23.8.16.42.altinitystable   # Snuba 25.7 needs ClickHouse >= 23.8.11.29
mkdir -p "$WORK"/{etc,relay,files}
cp "$HERE"/sentry.conf.py "$HERE"/config.yml "$HERE"/bootstrap.py "$HERE"/opts.py "$WORK"/etc/
cp "$HERE"/relay.yml "$WORK"/relay/config.yml
chmod 777 "$WORK"/relay
S=(docker run --network host -e SENTRY_CONF=/etc/sentry -v "$WORK"/etc:/etc/sentry -v "$WORK"/files:/data/files)
SN=(docker run --network host --env-file "$HERE"/snuba.env)

psql -h localhost -U postgres -qc "CREATE DATABASE sentry" 2>/dev/null || true
docker run -d --name sentry-redis --network host redis:6.2.14-alpine redis-server --port 6379 --save "" >/dev/null
docker run -d --name sentry-clickhouse --network host $CH >/dev/null
until curl -sf localhost:8123/ping >/dev/null; do sleep 2; done
# Sentry's consumers subscribe before anything produces: create their topics up front
for t in ingest-events ingest-attachments ingest-transactions ingest-monitors ingest-occurrences ingest-feedback-events \
         ingest-metrics ingest-performance-metrics ingest-replay-events ingest-replay-recordings ingest-spans outcomes \
         outcomes-billing events transactions event-replacements generic-events snuba-commit-log profiles processed-profiles \
         snuba-spans buffered-segments uptime-results; do
  docker exec kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --create --if-not-exists --topic $t >/dev/null
done
"${SN[@]}" --rm $SNUBA bootstrap --force >/dev/null
"${S[@]}" --rm $SENTRY upgrade --noinput >/dev/null
"${S[@]}" --rm $SENTRY createuser --email admin@example.com --password ee-poc-admin --superuser --no-input >/dev/null
"${S[@]}" --rm $SENTRY exec /etc/sentry/opts.py >/dev/null

"${SN[@]}" -d --name snuba-api $SNUBA api >/dev/null
"${SN[@]}" -d --name snuba-errors $SNUBA rust-consumer --storage errors --consumer-group snuba-consumers \
  --auto-offset-reset=latest --max-batch-time-ms 750 --no-strict-offset-reset >/dev/null
"${S[@]}" -d --name sentry-web $SENTRY run web >/dev/null
"${S[@]}" -d --name sentry-worker $SENTRY run worker -c 2 >/dev/null
"${S[@]}" -d --name sentry-ingest $SENTRY run consumer ingest-events --consumer-group ingest-consumer >/dev/null
"${S[@]}" -d --name sentry-ppf $SENTRY run consumer post-process-forwarder-errors --consumer-group post-process-forwarder \
  --synchronize-commit-log-topic=snuba-commit-log --synchronize-commit-group=snuba-consumers >/dev/null
until curl -sf localhost:9100/_health/ >/dev/null; do sleep 3; done

# Relay registers with web; uWSGI's HTTP router drops kept-alive connections, which is why
# sentry.conf.py turns keep-alive off (self-hosted puts nginx in front instead)
docker run --rm --network host $RELAY credentials generate --stdout > "$WORK"/creds.tmp
mv "$WORK"/creds.tmp "$WORK"/relay/credentials.json
docker run -d --name sentry-relay --network host -v "$WORK"/relay:/work/.relay $RELAY run >/dev/null
until curl -sf localhost:3001/api/relay/healthcheck/ready/ | grep -q true; do sleep 3; done

# project, admin token and the elastic-evals internal integration (webhook -> :8001)
"${S[@]}" --rm $SENTRY exec /etc/sentry/bootstrap.py 2>/dev/null | sed -n 's/^EE_BOOTSTRAP //p' | python3 -c '
import json, sys
d = json.load(sys.stdin)
d["dsn"] = d["dsn"].replace(":9100/", ":3001/")  # events go to Relay
d.update(url="http://localhost:9100", user="admin@example.com", password="ee-poc-admin")
print(json.dumps(d))' > "$OUT"
echo "Sentry at http://localhost:9100 (admin@example.com / ee-poc-admin); integration details in $OUT"
