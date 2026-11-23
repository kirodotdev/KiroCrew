# The README and the installers' closing steps say how to stop Kiro Crew

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22

## Decision

The README's install section and the closing steps of `cli.sh` and
`install.sh` name `kirocrew stop`, and the README and `cli.sh` say that a
service starts again at the next boot or login until
`kirocrew service uninstall` removes it.

## Why

- A first-time user could not work out how to stop the background gateway:
  the steps were only in the install guide.
- `kirocrew stop` on an installed service lasts only until the next boot or
  login, so naming `kirocrew stop` alone would mislead a service user.
- The install guide still owns the full removal steps; the README links to
  them.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/38467#issuecomment-7075366841 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/38467 -- the issue.
