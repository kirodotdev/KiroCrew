# Authentication

Source: https://kiro.dev/docs/getting-started/authentication/ (fetched 2026-09-06)

Providers: GitHub, Google, AWS Builder ID, AWS IAM Identity Center, external IdP
(Entra ID, Okta). API key is CLI-only, and the CLI is the only surface that has it.

## Sign in

```bash
kiro-cli          # or kiro-cli login
```

Prompts you to press Enter, then completes in the browser. IAM Identity Center
asks for a Start URL and a Region first.

## Remote machine sign-in

Device flow covers Builder ID, IAM Identity Center, Google and GitHub: the CLI
prints a URL and a one-time code you enter in any browser, and no port
forwarding is needed. External IdP login does not support device flow.

```bash
kiro-cli login    # pick a method, then open the printed URL and enter the code
```

## API key (CI and headless)

Pro, Pro+, Pro Max and Power subscriptions only; an admin-managed subscription
needs API key authentication enabled first. Generate the key in the API Keys
section of app.kiro.dev, where the full value is shown once.

```bash
export KIRO_API_KEY=ksk_xxxxxxxx
kiro-cli chat --no-interactive "your prompt here"
```

Precedence: an active browser session from `kiro-cli login` wins over
`KIRO_API_KEY`, and with neither the CLI prompts you to sign in. `kiro-cli
whoami` reports which one is active. Key usage draws on the same subscription
credits.

## AWS GovCloud (US)

IAM Identity Center and external IdPs only; the social providers are not
available there. The Start URL must contain `us-gov-home`, and IAM Identity
Center routes to the GovCloud region from the same installer.

## Sign out

```bash
kiro-cli logout
```
