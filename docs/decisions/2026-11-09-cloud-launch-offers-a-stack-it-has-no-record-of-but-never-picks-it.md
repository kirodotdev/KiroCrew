# Cloud launch offers a stack it has no record of only when it can resume it, and never picks it for you

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-09

## Decision

When `kirocrew cloud launch` has no usable saved launch and finds `kirocrew-<tag>` stacks, it
offers one only when that stack is neither failed nor in progress and a live Kiro Crew instance
carries its tag, and it names every other stack as not offered with the
`kirocrew cloud status --tag <tag>` for the Region and profile it searched; "Create a new
installation" is the default, `--yes` keeps no found stack, and a saved launch keeps its
default.

## Why

- Pressing Enter resumed a stack whose instance was gone, one another run was still creating,
  or one merely named `kirocrew-…`.
- Nothing yet tells this installation's own stack whose record was lost from another person's,
  and keeping one makes it the stack `destroy` acts on.
- A stack left out is still named, because it may be billing or be this installation's own
  interrupted launch.
- Linking stacks to the installation that launched them, the launcher policy condition and the
  dashboard's view of other instances wait for a design sign-off.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32071#issuecomment-6731761616 -- the
  maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/32071 -- the issue.
