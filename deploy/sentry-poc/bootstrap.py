# Creates the demo project, an admin API token and the elastic-evals internal integration (a Sentry App
# with a webhook to our bridge, issue + error subscriptions and an issue-link component). Prints JSON.
import json
from sentry.models.organization import Organization
from sentry.models.project import Project
from sentry.models.projectkey import ProjectKey
from sentry.models.team import Team
from sentry.users.models.user import User
from sentry.models.apitoken import ApiToken
from sentry.sentry_apps.logic import SentryAppCreator
from sentry.sentry_apps.models.sentry_app import SentryApp
from sentry.sentry_apps.models.sentry_app_installation import SentryAppInstallation

org = Organization.objects.get(slug="sentry")
user = User.objects.get(email="admin@example.com")
team = Team.objects.filter(organization=org).first() or Team.objects.create(organization=org, slug="ee", name="ee")
project, _ = Project.objects.get_or_create(organization=org, slug="bank-agent", defaults={"name": "bank-agent"})
project.add_team(team)
key = ProjectKey.objects.filter(project=project).first() or ProjectKey.objects.create(project=project)
token = ApiToken.objects.create(user_id=user.id, scope_list=["org:admin", "project:admin", "event:admin", "member:read", "team:read"])
app = SentryApp.objects.filter(slug__startswith="elastic-evals").first()
if app is None:
    app = SentryAppCreator(
        name="elastic-evals", author="elastic-evals", organization_id=org.id, is_internal=True,
        scopes=["org:read", "project:read", "event:read", "event:write"],
        events=["issue", "error"], webhook_url="http://127.0.0.1:8001/v1/sentry/webhook",
        schema={"elements": [{"type": "issue-link", "link": {"uri": "/v1/sentry/issue-link", "required_fields": [
            {"type": "text", "name": "trace", "label": "Trace id"}]}, "create": {"uri": "/v1/sentry/issue-link", "required_fields": [
            {"type": "text", "name": "trace", "label": "Trace id"}]}}]},
        verify_install=False, is_alertable=True,
    ).run(user=user)
inst = SentryAppInstallation.objects.get(sentry_app=app)
from sentry.sentry_apps.installations import SentryAppInstallationTokenCreator
app_token = SentryAppInstallationTokenCreator(sentry_app_installation=inst).run(user=user, request=None)
print("EE_BOOTSTRAP " + json.dumps({"dsn": key.get_dsn(public=True), "token": token.plaintext_token,
                  "app_token": app_token.plaintext_token, "installation": inst.uuid,
                  "client_secret": app.application.client_secret, "project": project.slug, "org": org.slug}))
