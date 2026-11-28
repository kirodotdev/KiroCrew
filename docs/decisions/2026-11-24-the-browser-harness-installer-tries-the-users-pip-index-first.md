# The browser harness installer tries the user's pip index first, wheels only, then pypi.org

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24

## Decision

When the user has pip index settings, the browser harness installer's pip step runs first with them (the same five keys the Feishu SDK installer reads, read by the pip that installs) and with `--only-binary :all:`, and if that attempt fails (any failed exit, or a spawn that cannot start) or times out, runs once more exactly as it does today, against pypi.org; with no settings it runs once, as before.

## Why

- A network that reaches only a PyPI mirror had no way to install the browser engine's harness from the app, while the Feishu SDK installer already used the user's index.
- Wheels only on that attempt means pip builds nothing from the user's index: every installed file is a locked wheel whose sha256 the shipped lock pins.
- Falling back on any failure, rather than only on a download failure as the Feishu installer does, means a stale or wrong index costs one attempt, never a failure the run with no settings would not also have.
- An agent-written pip config file can choose that attempt's index host, TLS trust and client certificate; this is an accepted residual, stated in the browser spec's threat table. It does not supersede [the entry on files the gateway trusts](2026-11-20-agents-ask-before-changing-gateway-trusted-files.md) because (a) pip's config decides neither what runs nor any content the gateway accepts: every installed byte is checked against the shipped lock and nothing is built on that attempt; (b) what it does decide (the index host, TLS trust and client certificate for the first attempt) is that residual, stated; (c) the Feishu installer's read of pip's config predates that entry and is not in its follow-up list.

## Evidence

- Maintainer decision: https://github.com/kirodotdev/KiroCrew/issues/39392#issuecomment-7144342918
- Issue: https://github.com/kirodotdev/KiroCrew/issues/39392
- Maintainer restatement of the accepted residual and of why this does not supersede the 2026-11-20 entry: https://github.com/kirodotdev/KiroCrew/issues/39392#issuecomment-7155952782
