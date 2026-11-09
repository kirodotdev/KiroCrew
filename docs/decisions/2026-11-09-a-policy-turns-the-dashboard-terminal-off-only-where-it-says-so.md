# A security policy turns the dashboard terminal off only where it says so

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-09

## Decision

A security policy whose `boot.allow_terminal` is `false`, or any value that is not a JSON boolean, refuses the dashboard terminal the way `dashboard.terminal.enabled: false` does; a policy that leaves the key out has no opinion, and a policy translated from an organisation's enterprise-management settings does not state the key.

## Why

- The setting binds exactly where an administrator wrote it.
- No host whose policy never mentioned the key loses the terminal: the shipped example policy and every translated enterprise policy leave it out.
- `require_sandbox` and `fail_closed` are unchanged. Nothing enforces `require_sandbox` yet, and a later change that binds it must leave a policy that omits the key behaving as it does today.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/30367#issuecomment-6746444559 -- the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/30367 -- the report: a written `allow_terminal: false` did not stop the terminal.
- https://github.com/kirodotdev/KiroCrew/pull/3121 -- the earlier design note that absence should mean no opinion.
