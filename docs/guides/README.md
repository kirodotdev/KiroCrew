# Guides

Task-oriented documentation: how to install, run, and operate Kiro Crew. For how the
system is built, see [../architecture/](../architecture/README.md).

| Guide | Covers |
|---|---|
| [install.md](install.md) | Installing and building Kiro Crew: source, wheel, and first run. |
| [worktree-verification-recipes.md](worktree-verification-recipes.md) | Copy-pasteable backend, frontend, agent-driving, diagnosis, and reclamation traces against an isolated worktree pod. |
| [windows-install.md](windows-install.md) | Native Windows setup, and the per-feature status on Windows. |
| [macos-troubleshooting.md](macos-troubleshooting.md) | macOS desktop app issues — a CLI that resolves in Terminal but is `command not found` inside the app, and the `launchctl setenv PATH` recipe that fixes it. |
| [docker.md](docker.md) | Running Kiro Crew as a container. |
| [docker-troubleshooting.md](docker-troubleshooting.md) | Diagnosing common Docker deployment issues. |
| [remote-and-mobile.md](remote-and-mobile.md) | Running 24/7 on a remote host, keeping it alive as a service, and reaching it from a phone over a tunnel. |
| [cloud-instance-ssm-vs-ssh.md](cloud-instance-ssm-vs-ssh.md) | How a cloud-launched instance is reached through the Instances hub: the native AWS SSM transport vs the legacy SSH-over-`ProxyCommand` path. |
| [remote-crew-on-ec2.md](remote-crew-on-ec2.md) | Reaching a Remote Instance gateway on EC2 over SSH or AWS SSM, plus the common EC2 setup gotchas (sandbox backend, linger, port/tunnel matching). |
| [slack-setup.md](slack-setup.md) | Creating and configuring the Slack app. |
| [enterprise-mcp-governance.md](enterprise-mcp-governance.md) | Running Kiro Crew on an enterprise Kiro account (IAM Identity Center / API key) whose administrator allow-lists MCP servers through a registry: why features go silently missing, and the two-sided fix. Also the admin-facing rollout for **central policy distribution** — publishing one `security_policy.json` that every host fetches, caches and re-fetches. |
| [connecting-remote-oauth-mcp-server.md](connecting-remote-oauth-mcp-server.md) | Adding and authenticating a remote HTTP/OAuth MCP server (worked example: Miro) end to end: the right config for your agent, the `includeMcpJson: false` gotcha, the inline Authorize banner that carries the consent URL, the browser flow (including the paste-back relay a remote gateway needs), the `oauth_endpoints.json` allowlist for unrecognized hosts, and why registering the new tools takes a drained warm pool, not just a new chat. |
| [secrets-env.md](secrets-env.md) | Passing secrets (API keys, tokens) to MCP servers via systemd environment directives or a shell wrapper — interim workarounds pending the encrypted vault. |
| [oauth-app-registration/README.md](oauth-app-registration/README.md) | Index of the per-provider OAuth app registration runbooks for Connections providers that do not support Dynamic Client Registration: which redirect URI each provider gets, and where the Client ID / secret go (**Settings → OAuth Apps**). |
| [oauth-app-registration/github.md](oauth-app-registration/github.md) | Registering a GitHub OAuth app for the remote GitHub MCP server: redirect URI `http://127.0.0.1:48101/callback`, organization-owner approval, and entering the credentials in Kiro Crew. |
| [oauth-app-registration/asana.md](oauth-app-registration/asana.md) | Registering an Asana developer app for the Asana V2 MCP server: redirect URI `http://127.0.0.1:48102/callback`, domain approval mode, and entering the credentials in Kiro Crew. |
| [oauth-app-registration/google-drive.md](oauth-app-registration/google-drive.md) | Creating a Google Cloud OAuth client for the Developer Preview Google Drive MCP server: redirect URI `http://127.0.0.1:48103/callback`, program enrollment, consent-screen and scope-class setup. |
| [oauth-app-registration/gmail.md](oauth-app-registration/gmail.md) | Creating a Google Cloud OAuth client for the Developer Preview Gmail MCP server: redirect URI `http://127.0.0.1:48104/callback`, program enrollment, and the restricted-scope implications. |
| [oauth-app-registration/google-calendar.md](oauth-app-registration/google-calendar.md) | Creating a Google Cloud OAuth client for the Developer Preview Google Calendar MCP server: redirect URI `http://127.0.0.1:48105/callback`, program enrollment, and read-only scope setup. |
| [oauth-app-registration/slack.md](oauth-app-registration/slack.md) | Registering a Slack app for the Slack MCP server: enabling PKCE, redirect URI `http://localhost:48106/callback` (Slack's only plain-http exception), user scopes, workspace approval, and the 30-day refresh-token limit. |
| [oauth-app-registration/hubspot.md](oauth-app-registration/hubspot.md) | Creating a HubSpot MCP auth app for the remote HubSpot MCP server: redirect URI `http://localhost:48107/callback` (HubSpot refuses IP-literal hosts), automatic scopes, and installer permissions. |
| [oauth-app-registration/box.md](oauth-app-registration/box.md) | Registering a Box custom MCP client through the Admin Console: redirect URI `http://127.0.0.1:48108/callback` and entering the credentials in Kiro Crew. |
| [oauth-app-registration/microsoft-365.md](oauth-app-registration/microsoft-365.md) | Why there is no Microsoft 365 Connections entry today, what Microsoft offers instead, and the optional Microsoft Entra ID app registration to prepare in advance. |
| [telemetry-otlp-export.md](telemetry-otlp-export.md) | Pushing Kiro Crew's OpenTelemetry metrics to a collector and on to CloudWatch, Datadog, or any OTLP-compatible backend: the two separate consent switches, collector config samples, the temporality setting that decides whether a backend accepts the data, what is and is not exported, and how to verify the path end to end. |
| — | Other chat channels (Discord, Telegram, Teams, Webex, WeCom, WeChat) are documented in [../../src/kiro_crew/docs/](../../src/kiro_crew/docs/README.md); the channel-neutral transport contract is [messaging.md](../system-specs/modules/messaging.md). |

`assets/` holds the copy-pasteable service unit, launchd plist, and setup script
that [remote-and-mobile.md](remote-and-mobile.md) refers to, plus an example
`security_policy.json` that
[governance.md](../system-specs/modules/governance.md) and
[enterprise-mcp-governance.md](enterprise-mcp-governance.md) refer to.

End-user feature documentation is not here: it ships in the package under
[`../../src/kiro_crew/docs/`](../../src/kiro_crew/docs/README.md).
