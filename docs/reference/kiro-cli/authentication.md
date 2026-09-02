# Authentication

Source: https://kiro.dev/docs/cli/authentication/

Providers: GitHub, Google, AWS Builder ID, AWS IAM Identity Center, external IdP (Entra ID, Okta).

## Sign in

```bash
kiro-cli          # or kiro-cli login
```

Opens browser for authentication.

## Remote machine sign-in

Builder ID / IAM Identity Center: device code auth — CLI shows URL + code for local browser.

Social login (Google/GitHub): requires SSH port forwarding:

```bash
# 1. Run kiro-cli login on remote, note the port (e.g. 49153)
# 2. On local machine:
ssh -L 49153:localhost:49153 -N user@remote-host
# 3. Press Enter in CLI, open URL in local browser
```

## Sign out

```bash
kiro-cli logout
```
