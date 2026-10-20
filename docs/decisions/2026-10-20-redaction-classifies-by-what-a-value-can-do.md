# Redaction classifies every value by what it can do: Secret, Bearer link or Identifier

Decided by: Bolin Chen (maintainer)
Date: 2026-10-20

## Decision
Credential redaction sorts every value into one of three tiers by what the value
can do alone or combined only with public data, redacts Secrets and Bearer links,
and never redacts an Identifier.

| Tier | Test | Examples | Action |
| --- | --- | --- | --- |
| Secret | Authenticates or signs | Cloud secret access keys, session tokens, private keys, vendor API tokens, passwords in URIs, JWTs, Bearer and Basic headers, OAuth client secrets, webhook signing secrets | Always redact the value |
| Bearer link | A URL that grants access by itself until it expires | Presigned URLs, `?token=` links, webhook URLs | Redact or block, keyed on the signing part |
| Identifier | Names or describes, cannot authenticate | Cloud access key IDs (`AKIA`/`ASIA`), account IDs, ARNs, request IDs, git SHAs, digests, UUIDs, resource IDs | Never redact |

The rules that come with the tiers:

- A value is redacted for its tier, not its shape. An Identifier inside a Bearer
  link stays visible; the signature goes. A presigned URL is keyed on its
  signature, not on the key ID it carries.
- Session tokens are Secret: they authenticate together with the secret key and
  almost always appear next to it.
- `ABIA` and `ACCA` key IDs are pending: they stay as they are until the
  security owner confirms their tier.
- The value is replaced and the label is kept: `api_key = [REDACTED: credential]`.
- One rule table holds every tier and pattern, and egress code reaches it through
  one composed entry point. No surface keeps its own copy.
- The exfiltration and decisions gates block on the Secret and Bearer link tiers
  only. An Identifier is never grounds to block.
- The entropy guess for a bare secret is removed, once case-insensitive labels
  and key-ID pairing (an unlabelled 40-character value next to a key ID is a
  Secret) are in place. Some secrets that carry no context will then go
  uncaught; that residual is accepted.
- Transcripts already written stay as they are. Nothing rewrites history.
- The tiers take effect phase by phase, as the implementation pull requests of
  [#22552](https://github.com/kirodotdev/KiroCrew/issues/22552) land. Until a phase lands, the behaviour it changes stands
  as it is, and is not a regression of this entry.

## Why
Each rule decided sensitivity on its own, by shape, so identifiers that cannot
authenticate were removed along with real secrets. The clearest case is the cloud
access key ID: it is sent in cleartext in every signed request and is the join key
of every audit log row, so removing it stops an investigation without protecting
anything. The entropy guess cannot tell a random secret from a random digest, so
no threshold makes it precise; labels, vendor prefixes and key-ID pairing are the
control instead.

## Evidence
- Issue [#22552](https://github.com/kirodotdev/KiroCrew/issues/22552), opened by
  the maintainer, carrying the tiers, the design rules and the phased delivery.
- [The maintainer's design update on that issue](https://github.com/kirodotdev/KiroCrew/issues/22552#issuecomment-6301939162),
  answering its open questions: session tokens, presigned URLs, key IDs, and
  removing the entropy guess rather than narrowing it.
