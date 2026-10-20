# Minimal errors-only self-hosted Sentry for the elastic-evals phase-5 proof of concept.
# Every service runs with host networking next to the local Postgres and Kafka.
from sentry.conf.server import *  # noqa: F401,F403

DATABASES = {"default": {"ENGINE": "sentry.db.postgres", "NAME": "sentry", "USER": "postgres", "PASSWORD": "",
                         "HOST": "127.0.0.1", "PORT": "5432"}}
SENTRY_USE_BIG_INTS = True
SENTRY_SINGLE_ORGANIZATION = True
SENTRY_OPTIONS["system.event-retention-days"] = 90
SENTRY_OPTIONS["system.secret-key"] = "ee-poc-not-a-secret-0123456789abcdefghijklmnopqrstuvwxyz"
SENTRY_OPTIONS["system.url-prefix"] = "http://localhost:9100"
SENTRY_OPTIONS["system.internal-url-prefix"] = "http://127.0.0.1:9100"
SENTRY_OPTIONS["mail.backend"] = "dummy"
SENTRY_OPTIONS["redis.clusters"] = {"default": {"hosts": {0: {"host": "127.0.0.1", "password": "", "port": "6379", "db": "0"}}}}
SENTRY_OPTIONS["filestore.backend"] = "filesystem"
SENTRY_OPTIONS["filestore.options"] = {"location": "/data/files"}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
SENTRY_CACHE = "sentry.cache.redis.RedisCache"
BROKER_URL = "redis://127.0.0.1:6379/1"
CELERY_BROKER_URL = BROKER_URL

DEFAULT_KAFKA_OPTIONS = {"bootstrap.servers": "127.0.0.1:9092", "message.max.bytes": 50000000, "socket.timeout.ms": 1000}
SENTRY_EVENTSTREAM = "sentry.eventstream.kafka.KafkaEventStream"
SENTRY_EVENTSTREAM_OPTIONS = {"producer_configuration": DEFAULT_KAFKA_OPTIONS}
KAFKA_CLUSTERS["default"] = DEFAULT_KAFKA_OPTIONS

SENTRY_RATELIMITER = "sentry.ratelimits.redis.RedisRateLimiter"
SENTRY_BUFFER = "sentry.buffer.redis.RedisBuffer"
SENTRY_QUOTAS = "sentry.quotas.redis.RedisQuota"
SENTRY_TSDB = "sentry.tsdb.redissnuba.RedisSnubaTSDB"
SENTRY_DIGESTS = "sentry.digests.backends.redis.RedisBackend"
SENTRY_SEARCH = "sentry.search.snuba.EventsDatasetSnubaSearchBackend"
SENTRY_SEARCH_OPTIONS = {}
SENTRY_TAGSTORE_OPTIONS = {}
SNUBA = "http://127.0.0.1:1218"
SENTRY_RELAY_OPEN_REGISTRATION = True

SENTRY_WEB_HOST = "0.0.0.0"
SENTRY_WEB_PORT = 9100
SENTRY_WEB_OPTIONS = {"http": "%s:%s" % (SENTRY_WEB_HOST, SENTRY_WEB_PORT), "protocol": "uwsgi", "uwsgi-socket": None,
                      "http-chunked-input": True, "memory-report": False,
                      "workers": 2, "threads": 4}
SENTRY_FEATURES["projects:sample-events"] = False
SENTRY_FEATURES.update({"organizations:integrations-issue-basic": True, "organizations:integrations-issue-sync": True,
                        "organizations:integrations-alert-rule": True, "organizations:integrations-chat-unfurl": True,
                        "organizations:integrations-incident-management": True, "organizations:integrations-ticket-rules": True,
                        "organizations:integrations-event-hooks": True, "organizations:integrations-stacktrace-link": True,
                        "organizations:sentry-app-ui-components": True})
CSRF_TRUSTED_ORIGINS = ["http://localhost:9100", "http://127.0.0.1:9100"]
