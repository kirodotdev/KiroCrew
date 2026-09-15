# Microsoft 365 and Kiro Crew Connections: status and Entra app registration

*Who this is for: anyone who expected a Microsoft 365 card under Kiro Crew's Connections and wants to know why there is none, what Microsoft offers instead, and what to set up in Microsoft Entra ID so the day a card appears you only have to paste two values. Expected time: 10 minutes to read; 15 minutes for the optional Entra registration.*

## Status: not available as a Connections entry

Kiro Crew Connections has no Microsoft 365 provider, no registry entry, and no reserved callback port. There is nothing to configure under **Settings → OAuth Apps** for Microsoft 365 today. Do not set `KIROCREW_CONNECTIONS_MICROSOFT_365_*` environment variables; nothing reads them.

This is a statement about the Connections feature, which manages a fixed list of vendor MCP servers with dashboard-entered credentials. It says nothing about whether an individual agent could be pointed at one of the Microsoft endpoints below by other means; that path is outside this document and has not been tested.

## Why there is no registry entry

Every provider in the Connections registry is one fixed public MCP URL plus one OAuth authorization server that any Kiro Crew install can point at. Microsoft 365 does not fit that shape:

- **There is no fixed, tenant-agnostic Microsoft 365 MCP endpoint.** Microsoft's MCP servers for mail, calendar, files and Teams are exposed at URLs that embed the customer's tenant, for example `https://agent365.svc.cloud.microsoft/agents/tenants/{tenantId}/servers/mcp_MailTools`. A registry entry would need a different URL per install.
- **Access is licensed and admin-provisioned per tenant.** Microsoft: "You must have a Microsoft 365 Copilot license to use Work IQ MCP servers." An administrator must also allow the servers in the Microsoft 365 admin center and consent to the client application. Kiro Crew cannot make those choices for a customer.
- **The client is an Entra app registration in the customer's tenant, not a Kiro Crew-owned app.** Microsoft's own instructions for Claude Code and GitHub Copilot CLI have each organisation register its own public-client application and hand its `clientId` to the tool. There is no shared client to ship, and Kiro Crew's per-install client ID field would have to be paired with a per-install URL.
- **Everything is preview.** Microsoft marks the Work IQ MCP servers and the Graph-backed MCP Server for Enterprise as preview features "not meant for production use", with URLs, scopes and licensing that "may be substantially modified".

## What exists today on Microsoft's side

| Server | Endpoint | Auth | Usable by a generic MCP client such as kiro-cli? |
|--------|----------|------|----------------------------------------------------|
| Microsoft Learn MCP Server | `https://learn.microsoft.com/api/mcp` | None. "There's no authentication required." | Yes, but it serves public documentation only, not Microsoft 365 data. Not a Connections candidate because there is nothing to connect. |
| Work IQ MCP servers (Mail, Calendar, SharePoint, OneDrive, Teams, User, Word, Copilot) | `https://agent365.svc.cloud.microsoft/agents/tenants/{tenantId}/servers/mcp_<Name>` | Entra OAuth, public client with PKCE; Microsoft documents `http://localhost:8080/callback` and `http://127.0.0.1` redirect URIs and shows Claude Code, GitHub Copilot CLI and VS Code connecting. | Technically yes, per tenant, with a Microsoft 365 Copilot licence, an Entra app registration and admin-granted `WorkIQ-*` permissions. Preview. |
| Microsoft MCP Server for Enterprise (Microsoft Graph) | `https://mcp.svc.cloud.microsoft/enterprise` | Entra OAuth, delegated `MCP.*` scopes (for example `MCP.User.Read.All`), admin consent. Provisioned once per tenant with `Grant-EntraBetaMCPServerPermission`. | Yes for a custom client after tenant provisioning, but it is read-only Entra directory data (users, groups, devices, sign-ins), not mail or files. Preview. |
| Copilot Studio MCP tools | Configured inside Copilot Studio agents via connectors | Power Platform connections | No. This is Copilot Studio consuming MCP servers, not an endpoint a third-party client can call. |
| Microsoft 365 Agents Toolkit / agent connectors | App-manifest registration of *your* MCP server so Microsoft 365 Copilot can call it | n/a | No. Direction is reversed: Microsoft 365 is the client, your server is the tool. |
| Bring Your Own MCP server (Agent 365) | Registers *your* remote MCP server for tenant governance | n/a | No, same reason. Microsoft lists Copilot Studio, VS Code, Claude Code and GitHub Copilot CLI as the supported client surfaces. |

Microsoft's documentation does not state whether the Work IQ or Enterprise servers publish RFC 9728 protected-resource metadata at `/.well-known/oauth-protected-resource`, and the tenant-scoped URL means the discovery result would differ per tenant anyway. This document did not probe those endpoints.

What Microsoft does document for the Work IQ servers, as configured for Claude Code, GitHub Copilot CLI and VS Code:

- The client is an app registration the organisation creates itself ("you need to register an enterprise application which acts as a client and has the proper permissions to access the Work IQ MCP servers").
- The client passes only a `clientId` and a callback port; no client secret appears anywhere in the configuration examples.
- Accepted redirect URIs listed by Microsoft: `http://localhost:8080/callback`, `http://127.0.0.1`, `http://vscode.dev/redirect`, `https://localhost`, and the Windows broker URI `ms-appx-web://Microsoft.AAD.BrokerPlugin/...`.
- Each Work IQ server is a separate API permission on the client app (Microsoft's example: **WorkIQ-MailServer**), and each is a separate MCP URL ending in the server name (`mcp_MailTools` for mail).
- Tenant administrators allow or block servers under **Microsoft 365 admin center → Agents and Tools**; "If an MCP server is blocked, it's blocked for every user and every agent."

The Enterprise server differs: one URL for all tenants, but an administrator must provision it once per tenant with PowerShell, its scopes are named `MCP.{Graph scope}`, it is limited to 100 calls per minute per user, and it returns directory data only. Microsoft: "It focuses on Microsoft Entra identity and directory read-only scenarios."

## If you want to prepare: Entra ID app registration

These steps create a public-client app registration that matches what Microsoft documents for coding agents. Nothing in Kiro Crew consumes it yet; the redirect URI below is hypothetical because no port has been allocated. Only proceed if you have a Microsoft 365 Copilot licence in the tenant, or you will register an app that cannot reach any server.

1. Sign in to [https://entra.microsoft.com](https://entra.microsoft.com) as at least an **Application Developer**. If your tenant restricts app registration, an administrator must do this or delegate the role.
2. Browse to **Entra ID → App registrations** and click **New registration**.
3. **Name**: for example `Kiro Crew Connections (Microsoft 365)`.
4. **Supported account types**: choose **Single tenant only - <your tenant>**. Microsoft recommends single tenant for most applications, and the Work IQ URL is tenant-bound anyway. Do not pick a personal-accounts option: those registrations disallow query parameters in redirect URIs and are limited to 100 of them.
5. Leave **Redirect URI** empty for now and click **Register**. On the **Overview** page record the **Application (client) ID** and the **Directory (tenant) ID**.
6. Go to **Authentication → Add a platform → Mobile and desktop applications**. Add the custom redirect URI `http://127.0.0.1:<port>/callback`, where `<port>` is the value Kiro Crew will publish when a Microsoft 365 provider ships. The Entra portal's text box refuses an `http` URI with the literal `127.0.0.1` even though the platform accepts it; Microsoft's workaround is to edit the manifest: open **Manifest**, find `replyUrlsWithType`, and add `{ "url": "http://127.0.0.1:<port>/callback", "type": "InstalledClient" }`. If you prefer to avoid the manifest, `http://localhost:<port>/callback` is accepted in the text box and Microsoft ignores the port when matching a localhost redirect: "the port component (for example, `:5001` or `:443`) is ignored for the purposes of matching a localhost redirect URI." Microsoft nevertheless recommends `127.0.0.1` over `localhost` "to prevent your app from breaking due to misconfigured firewalls or renamed network interfaces".
7. Still under **Authentication**, scroll to **Advanced settings → Allow public client flows** and set it to **Yes**. Kiro Crew's callback listener is a native loopback client, which Entra treats as a public client; no client secret is created or needed.
8. Go to **API permissions → Add a permission**. For Work IQ servers, choose **APIs my organization uses**, search for the Work IQ server application (Microsoft's example is **WorkIQ-MailServer**) and add its delegated permission; repeat for Calendar, SharePoint, OneDrive or Teams as needed. For the Graph-backed Enterprise server, choose **Microsoft MCP Server for Enterprise** and add delegated scopes such as `MCP.User.Read.All`. Do not add application (app-only) permissions; Microsoft states the Enterprise server "supports only delegated permissions".
9. Click **Grant admin consent for <tenant>** and confirm. This step requires an administrator. Without it, users see a consent prompt they may not be allowed to accept.
10. For the Enterprise server only, an administrator must first run `Grant-EntraBetaMCPServerPermission` from the `Microsoft.Entra.Beta` PowerShell module to provision the server in the tenant; see Microsoft's get-started page. Work IQ servers are allowed or blocked under **Microsoft 365 admin center → Agents and Tools**.
11. Keep the client ID and tenant ID. When a Microsoft 365 provider appears in Kiro Crew you will enter the client ID in the provider card and replace `<port>` in the redirect URI with the published value.

## What Kiro Crew would need to build

- **A tenant-parameterised provider.** The registry entry would have to accept the tenant ID (and, for Work IQ, the server name) and build the MCP URL at connect time, and it would need to choose between the Work IQ family and the Enterprise server, which have different scopes and audiences.
- **A public-client OAuth path.** Every other Connections provider is a confidential client with a client secret. Microsoft's documented client is a secret-less public client with PKCE; the Connections card would need a mode with no **Client secret** field, and kiro-cli's callback flow would need to send `code_verifier` and no secret.
- **Per-tenant authority.** Token requests go to `https://login.microsoftonline.com/{tenantId}/oauth2/v2.0/token`, not to one global issuer, so issuer discovery and token refresh must be keyed by tenant.
- **Licence and admin-consent error handling.** A user without a Microsoft 365 Copilot licence, or in a tenant where the admin has not consented, fails after the browser step. The dashboard would need to explain that rather than show a generic OAuth error.
- **A stable target.** Both server families are preview. Kiro Crew should not ship a registry entry against URLs and scope names Microsoft says may change.

Until those exist, the honest status is: Microsoft 365 is not supported as a Connections entry today.

## Sources

- https://learn.microsoft.com/en-us/microsoft-agent-365/tooling-servers-overview (accessed 2026-09-10)
- https://learn.microsoft.com/en-us/graph/mcp-server/overview (accessed 2026-09-10)
- https://learn.microsoft.com/en-us/graph/mcp-server/get-started (accessed 2026-09-10)
- https://learn.microsoft.com/en-us/training/support/mcp (accessed 2026-09-10)
- https://learn.microsoft.com/en-us/microsoft-agent-365/bring-your-own-mcp (accessed 2026-09-10)
- https://learn.microsoft.com/en-us/microsoft-copilot-studio/agent-extend-action-mcp (accessed 2026-09-10)
- https://learn.microsoft.com/en-us/microsoftteams/platform/m365-apps/agent-connectors (accessed 2026-09-10)
- https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app (accessed 2026-09-10)
- https://learn.microsoft.com/en-us/entra/identity-platform/reply-url (accessed 2026-09-10)
- https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow (accessed 2026-09-10)
