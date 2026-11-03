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
# ----------------------------------------------------------------------------------------------
# FILE OVERVIEW (for learners)
# Who calls it: chatbot_api.py creates one IdentityResolver at startup and calls resolve() on
#   every request (its who() helper) to learn the user id, role and platform.
# What it calls: only the standard library. Config: the `identity:` and `app:` sections.
# The resulting user_role is what access.py / guardrails.py use to decide what may be read.
# ----------------------------------------------------------------------------------------------
from __future__ import annotations

import hmac        # used for compare_digest: a constant-time string comparison (see resolve)
import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger("chatbot.identity")   # -> logs/backend_billing.log

# Add "tableau": "Tableau" here ONLY once a Tableau client exists and has been tested.
SUPPORTED_PLATFORMS = {"powerbi": "Power BI"}

# Matches ASCII control characters (newline, tab, NUL, ...). They are removed from header
# values so nobody can, for example, inject fake extra lines into the log file.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# Who is asking, as decided by IdentityResolver. frozen=True makes it read-only after creation.
#   verified  - True only when it came from the trusted gateway headers
#   demo_role - True when the role was picked in the visual under app.demo_mode
@dataclass(frozen=True)
class Identity:
    user_id: str | None = None
    user_role: str | None = None
    platform: str | None = None
    verified: bool = False
    demo_role: bool = False


# Strip control characters and spaces and cut to `limit` characters (the DB column size);
# an empty result becomes None.
def _clean(value, limit: int) -> str | None:
    if value is None:
        return None
    text = _CONTROL.sub("", str(value)).strip()[:limit]
    return text or None


class IdentityResolver:
    # cfg = the whole config dict. environ can be replaced by a plain dict in tests; normally
    # it is os.environ. The gateway SECRET itself is read from an environment variable whose
    # NAME is in config (identity.trusted_proxy.secret_env) - never from the config file.
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
        # Demo mode: the user may pick CEO / Developer / Analyst in the visual. Only allowed when
        # app.demo_mode is exactly true AND no real identity provider is configured.
        self.demo_role_selection = app_cfg.get("demo_mode") is True and configured_provider == "none"
        if self.provider == "trusted_proxy" and not self._secret:
            log.warning("identity.provider is trusted_proxy but the gateway secret environment variable "
                        "is not set: identity headers will be IGNORED.")

    # The client's claimed platform if it is a supported one, else the configured default.
    def platform_for(self, body) -> str:
        claimed = str((body or {}).get("platform") or "").strip().lower()
        return SUPPORTED_PLATFORMS.get(claimed, self.default_platform)

    # Main method. headers = the HTTP request headers, body = the JSON body (dict) or None.
    # Returns an Identity; when nothing can be trusted, user_id and user_role stay None.
    def resolve(self, headers, body=None) -> Identity:
        platform = self.platform_for(body)
        # Case 1 - demo mode: take the role the visual sent in "demo_role" (only the three
        # known roles; anything else gives role None, which access.py treats as "no access").
        if self.demo_role_selection:
            choices = {"ceo": "CEO", "developer": "Developer", "analyst": "Analyst"}
            claimed = (body or {}).get("demo_role")
            role = choices.get(claimed.strip().casefold()) if isinstance(claimed, str) else None
            return Identity(user_role=role, platform=platform, demo_role=True)
        # Case 2 - no trusted provider (or no secret configured): record no user at all.
        if self.provider != "trusted_proxy" or not self._secret or headers is None:
            return Identity(platform=platform)
        # Case 3 - trusted gateway: the request must carry the shared secret. compare_digest
        # takes the same time whether the first or the last character differs, so an attacker
        # cannot guess the secret character by character by measuring response times.
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
