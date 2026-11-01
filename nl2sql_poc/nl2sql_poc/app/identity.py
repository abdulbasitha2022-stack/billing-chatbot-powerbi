"""Who is asking? Only identities we can TRUST are recorded.

The Power BI visual runs in the user's browser, so anything it sends (a name, a role) can be
forged by anyone who can reach the backend. Therefore:

* user_id / user_role are taken ONLY from a trusted source:
    identity.provider: trusted_proxy   -> headers added by an authenticating gateway placed in
                                          front of this backend (IIS/ARR + Windows auth, nginx +
                                          OIDC, an API gateway, ...). They are accepted ONLY if
                                          the request also carries the gateway's shared secret.
    identity.provider: none (default)  -> no user is recorded (NULL). Nothing is invented.
* Whatever the visual sends as a user name (the old `requested_by` field) is ignored.
* platform is a label, not a credential: the client may say "powerbi", but only values in
  SUPPORTED_PLATFORMS are accepted; anything else falls back to config app.platform.

NOT implemented (needs work outside this POC): validating a Microsoft Entra ID token sent by the
visual (`host.acquireAADTokenService`), or reading the signed-in Power BI user in any other way.
"""
from __future__ import annotations

import hmac
import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger("chatbot.identity")

# Add "tableau": "Tableau" here ONLY once a Tableau client exists and has been tested.
SUPPORTED_PLATFORMS = {"powerbi": "Power BI"}

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Identity:
    user_id: str | None = None
    user_role: str | None = None
    platform: str | None = None
    verified: bool = False
    demo_role: bool = False


def _clean(value, limit: int) -> str | None:
    if value is None:
        return None
    text = _CONTROL.sub("", str(value)).strip()[:limit]
    return text or None


class IdentityResolver:
    def __init__(self, cfg: dict, environ=None):
        env = os.environ if environ is None else environ
        app_cfg = cfg.get("app") or {}
        self.default_platform = _clean(app_cfg.get("platform"), 50) or "Power BI"
        ident = cfg.get("identity") or {}
        configured_provider = str(ident.get("provider") or "none").strip().lower()
        self.provider = configured_provider
        proxy = ident.get("trusted_proxy") or {}
        self.secret_header = str(proxy.get("secret_header") or "X-Gateway-Secret")
        self.user_header = str(proxy.get("user_header") or "X-Authenticated-User")
        self.role_header = str(proxy.get("role_header") or "X-Authenticated-Role")
        self._secret = env.get(str(proxy.get("secret_env") or "CHATBOT_GATEWAY_SECRET")) or None
        if self.provider not in ("none", "trusted_proxy"):
            log.warning("Unknown identity.provider %r: no user identity will be recorded.", self.provider)
            self.provider = "none"
        self.demo_role_selection = app_cfg.get("demo_mode") is True and configured_provider == "none"
        if self.provider == "trusted_proxy" and not self._secret:
            log.warning("identity.provider is trusted_proxy but the gateway secret environment variable "
                        "is not set: identity headers will be IGNORED.")

    def platform_for(self, body) -> str:
        claimed = str((body or {}).get("platform") or "").strip().lower()
        return SUPPORTED_PLATFORMS.get(claimed, self.default_platform)

    def resolve(self, headers, body=None) -> Identity:
        platform = self.platform_for(body)
        if self.demo_role_selection:
            choices = {"ceo": "CEO", "developer": "Developer", "analyst": "Analyst"}
            claimed = (body or {}).get("demo_role")
            role = choices.get(claimed.strip().casefold()) if isinstance(claimed, str) else None
            return Identity(user_role=role, platform=platform, demo_role=True)
        if self.provider != "trusted_proxy" or not self._secret or headers is None:
            return Identity(platform=platform)
        presented = headers.get(self.secret_header) or ""
        if not hmac.compare_digest(presented.encode("utf-8"), self._secret.encode("utf-8")):
            if headers.get(self.user_header):
                log.warning("Identity headers ignored: missing or wrong gateway secret.")
            return Identity(platform=platform)
        user = _clean(headers.get(self.user_header), 255)
        if not user:
            return Identity(platform=platform)
        return Identity(user_id=user, user_role=_clean(headers.get(self.role_header), 100),
                        platform=platform, verified=True)
