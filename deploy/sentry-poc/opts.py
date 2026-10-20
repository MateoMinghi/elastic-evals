# Marks the install as configured so the first-run wizard does not block the UI (run once, after createuser).
from sentry import options
import sentry
for k, v in {"system.admin-email": "admin@example.com", "auth.allow-registration": False, "beacon.anonymous": True,
             "beacon.record_cpu_ram_usage": False, "system.url-prefix": "http://localhost:9100"}.items():
    try:
        options.set(k, v)
    except Exception as e:
        print("skip", k, e)
options.set("sentry:version-configured", sentry.get_version())
print("version-configured", options.get("sentry:version-configured"))
