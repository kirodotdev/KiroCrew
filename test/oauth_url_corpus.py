"""Corpus of real-world OAuth/OIDC authorization URLs that MUST never be
rejected by KiroCrew's MCP OAuth-banner safety check.

When an MCP server asks KiroCrew to authenticate, kiro-cli forwards the
provider's *authorization* (consent) URL and KiroCrew renders it as a
clickable banner.  These URLs legitimately carry high-entropy params
(``state``, ``code_challenge`` …) and frequently exceed 200 chars — so the
generic data-exfiltration heuristic must not fire on them.  A regression here
silently breaks sign-in for that provider ("authentication failed: URL
contained credential or exfiltration pattern").

This corpus is the contract: every entry is a *real* provider URL shape
(host + parameter set taken from the provider's own OAuth docs) and
``security.oauth_url_contains_credential`` must return False for all of them.

**Adding a provider:** when KiroCrew gains/observes a new MCP OAuth provider,
add a representative authorize URL here.  If any param it uses isn't yet in
``_OAUTH_QUERY_PARAMS`` (kiro_crew/security.py), add it there too
— and confirm the value is benign (not a real secret) before exempting it.

Values use realistic-but-fake identifiers; PKCE ``code_challenge`` is a real
43-char base64url SHA-256 sample.  Sources noted per entry.
"""

from __future__ import annotations

# Each item: (provider_label, authorization_url)
LEGIT_OAUTH_URLS: list[tuple[str, str]] = [
    # Asana V2 MCP OAuth authorization + PKCE.
    # developers.asana.com/docs/integrating-with-asanas-mcp-server
    (
        "asana-mcp-v2",
        "https://app.asana.com/-/oauth_authorize"
        "?client_id=1234567890123456"
        "&redirect_uri=http%3A%2F%2F127.0.0.1%3A33418%2Fcallback"
        "&response_type=code"
        "&resource=https%3A%2F%2Fmcp.asana.com%2Fv2"
        "&state=af0ifjsldkj"
        "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        "&code_challenge_method=S256",
    ),
    # GitHub OAuth apps + PKCE.
    # docs.github.com/.../authorizing-oauth-apps
    (
        "github",
        "https://github.com/login/oauth/authorize"
        "?client_id=Iv1.a1b2c3d4e5f6g7h8"
        "&redirect_uri=https%3A%2F%2Fexample.com%2Fcallback"
        "&scope=repo%20gist"
        "&state=xyz789randomstring"
        "&login=octocat"
        "&allow_signup=true"
        "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        "&code_challenge_method=S256",
    ),
    # GitHub loopback redirect (native/desktop app) — short variant.
    (
        "github-loopback",
        "https://github.com/login/oauth/authorize"
        "?client_id=Iv1.0123456789abcdef"
        "&redirect_uri=http%3A%2F%2F127.0.0.1%3A33418%2Fcallback"
        "&scope=read%3Aorg"
        "&state=af0ifjsldkj"
        "&code_challenge=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        "&code_challenge_method=S256&response_type=code",
    ),
    # Google OAuth 2.0 for installed apps + PKCE.
    # developers.google.com/identity/protocols/oauth2/native-app
    (
        "google",
        "https://accounts.google.com/o/oauth2/v2/auth"
        "?client_id=123456789-abc.apps.googleusercontent.com"
        "&redirect_uri=com.example.app%3A%2Foauth2redirect"
        "&response_type=code"
        "&scope=email%20profile%20openid"
        "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        "&code_challenge_method=S256"
        "&state=af0ifjsldkj"
        "&login_hint=user%40example.com"
        "&access_type=offline",
    ),
    # Microsoft identity platform (Entra) v2.0 auth code + PKCE.
    # learn.microsoft.com/.../v2-oauth2-auth-code-flow
    (
        "microsoft",
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
        "?client_id=00001111-aaaa-2222-bbbb-3333cccc4444"
        "&response_type=code"
        "&redirect_uri=http%3A%2F%2Flocalhost%2Fmyapp%2F"
        "&response_mode=query"
        "&scope=openid%20offline_access%20https%3A%2F%2Fgraph.microsoft.com%2Fmail.read"
        "&state=12345"
        "&prompt=select_account"
        "&domain_hint=contoso.com"
        "&code_challenge=YTFjNjI1OWYzMzA3MTI4ZDY2Njg5M2RkNmVjNDE5YmEyZGRhOGYyM2IzNjdmZWFhMTQ1ODg3NDcxY2Nl"
        "&code_challenge_method=S256",
    ),
    # Microsoft hybrid flow (adds id_token + nonce) — exercises a longer query.
    (
        "microsoft-hybrid",
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
        "?client_id=00001111-aaaa-2222-bbbb-3333cccc4444"
        "&response_type=code%20id_token"
        "&redirect_uri=http%3A%2F%2Flocalhost%2Fmyapp%2F"
        "&response_mode=fragment"
        "&scope=openid%20offline_access%20https%3A%2F%2Fgraph.microsoft.com%2Fuser.read"
        "&state=12345&nonce=abcde"
        "&code_challenge=YTFjNjI1OWYzMzA3MTI4ZDY2Njg5M2RkNmVjNDE5YmEyZGRhOGYyM2IzNjdmZWFhMTQ1ODg3NDcxY2Nl"
        "&code_challenge_method=S256",
    ),
    # Slack OAuth v2 (bot + user scopes, workspace pin).
    # docs.slack.dev/authentication/installing-with-oauth
    (
        "slack",
        "https://slack.com/oauth/v2/authorize"
        "?client_id=3336676.569200954261"
        "&scope=incoming-webhook%2Ccommands%2Cchat%3Awrite"
        "&user_scope=search%3Aread%2Cchannels%3Ahistory"
        "&redirect_uri=https%3A%2F%2Fexample.com%2Fslack%2Fauth"
        "&state=abc123xyz"
        "&team=T9TK3CUKW",
    ),
    # Linear (a common MCP server) — generic OAuth 2.0 authorize shape.
    (
        "linear",
        "https://linear.app/oauth/authorize"
        "?client_id=abcdef0123456789"
        "&redirect_uri=https%3A%2F%2Fmcp.linear.app%2Fcallback"
        "&response_type=code"
        "&scope=read%20write"
        "&state=9f8e7d6c5b4a",
    ),
    # Atlassian (Jira/Confluence MCP) — uses audience + prompt=consent.
    (
        "atlassian",
        "https://auth.atlassian.com/authorize"
        "?audience=api.atlassian.com"
        "&client_id=abc123DEF456ghi789"
        "&scope=read%3Ajira-work%20offline_access"
        "&redirect_uri=https%3A%2F%2Fexample.com%2Fcallback"
        "&state=somerandomstate"
        "&response_type=code&prompt=consent",
    ),
    # Notion OAuth + long-state/PKCE. This exact endpoint is owned by the
    # Connections registry and exercises the parameter-level entropy exemption.
    (
        "notion-long-state",
        "https://api.notion.com/v1/oauth/authorize"
        "?client_id=client123&response_type=code"
        "&redirect_uri=http%3A%2F%2Flocalhost%3A8080%2Fcb"
        "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        "&code_challenge_method=S256"
        "&state=" + ("a1B2c3D4" * 16),  # 128-char opaque state
    ),
    # Superhuman Mail MCP OAuth + PKCE. Endpoint taken from Superhuman's own
    # RFC 8414 metadata (authorization_servers -> mcp.auth.mail.superhuman.com,
    # authorization_endpoint /oauth2/authorize), which the Connections registry
    # pins as the provider's issuer. kiro-cli generates the opaque state and
    # S256 code_challenge, identical in shape to the other MCP-server providers
    # in the builtin set.
    (
        "superhuman-mail-mcp",
        "https://mcp.auth.mail.superhuman.com/oauth2/authorize"
        "?client_id=sh_mcp_client_0123456789"
        "&response_type=code"
        "&redirect_uri=http%3A%2F%2F127.0.0.1%3A33418%2Fcallback"
        "&scope=email+offline_access"
        "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        "&code_challenge_method=S256"
        "&state=" + ("Kp7mQ2xR" * 12),  # 96-char opaque state
    ),
    # Miro remote MCP server — authorization endpoint verified via RFC 8414
    # metadata by the reporter of issue #7578; the fail-closed banner blocked
    # every attempt to connect it before the endpoint was allowlisted.
    (
        "miro-mcp",
        "https://mcp.miro.com/authorize"
        "?client_id=3458764514956732000"
        "&redirect_uri=http%3A%2F%2F127.0.0.1%3A33418%2Fcallback"
        "&response_type=code"
        "&state=af0ifjsldkj"
        "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        "&code_challenge_method=S256",
    ),
]

# Consent URLs that the ACP banner-safety gate
# (``security.oauth_url_contains_credential``) must pass ONLY once the operator
# has allowlisted the endpoint in the keystone ``oauth_endpoints.json`` — they
# are NOT in ``_OAUTH_AUTHORIZATION_ENDPOINTS`` and must stay rejected with
# default config. Do NOT move an entry into ``LEGIT_OAUTH_URLS``: that list
# asserts default-config behavior. Each item:
# (provider_label, authorization_url, (host, path) the operator must allowlist).
OPERATOR_EXTENSION_OAUTH_URLS: list[tuple[str, str, tuple[str, str]]] = [
    # Generic long-state OIDC at an arbitrary identity provider — the exact
    # shape that trips the >=200-char query heuristic at any endpoint outside
    # the builtin set. Restored here under the operator-extension contract.
    (
        "oidc-generic-idp-long-state",
        "https://id.example-idp.com/authorize"
        "?client_id=client123&response_type=code"
        "&redirect_uri=http%3A%2F%2Flocalhost%3A8080%2Fcb"
        "&scope=openid%20profile%20email%20offline_access"
        "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        "&code_challenge_method=S256"
        "&state=" + ("a1B2c3D4" * 16),  # 128-char opaque state
        ("id.example-idp.com", "/authorize"),
    ),
    # Okta org-hosted authorization server — the canonical "my IdP is not in
    # the launch set" case from the field.
    (
        "okta-org",
        "https://acme.okta.com/oauth2/v1/authorize"
        "?client_id=0oabcde12345FGHIJ697"
        "&response_type=code"
        "&scope=openid%20profile%20email%20offline_access"
        "&redirect_uri=https%3A%2F%2Fexample.com%2Fcallback"
        "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        "&code_challenge_method=S256"
        "&state=" + ("Zx9yW8vU" * 12),
        ("acme.okta.com", "/oauth2/v1/authorize"),
    ),
    # Tenant-scoped Microsoft Entra authorize endpoint — a per-tenant path the
    # builtin ``/common/…`` entry deliberately does not cover.
    (
        "entra-tenant",
        "https://login.microsoftonline.com/11112222-aaaa-3333-bbbb-4444cccc5555"
        "/oauth2/v2.0/authorize"
        "?client_id=00001111-aaaa-2222-bbbb-3333cccc4444"
        "&response_type=code"
        "&redirect_uri=http%3A%2F%2Flocalhost%2Fmyapp%2F"
        "&response_mode=query"
        "&scope=openid%20offline_access%20https%3A%2F%2Fgraph.microsoft.com%2Fmail.read"
        "&state=12345"
        "&code_challenge=YTFjNjI1OWYzMzA3MTI4ZDY2Njg5M2RkNmVjNDE5YmEyZGRhOGYyM2IzNjdmZWFhMTQ1ODg3NDcxY2Nl"
        "&code_challenge_method=S256",
        (
            "login.microsoftonline.com",
            "/11112222-aaaa-3333-bbbb-4444cccc5555/oauth2/v2.0/authorize",
        ),
    ),
]
