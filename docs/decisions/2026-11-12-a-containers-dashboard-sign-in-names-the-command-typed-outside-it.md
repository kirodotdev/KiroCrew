# A container's dashboard sign-in names the command as typed outside the container

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-12

## Decision

On a container install, a dashboard tab's sign-in instructions name the
command as typed on the machine that runs the container,
`docker exec kirocrew kirocrew token`, instead of `kirocrew token`; every
other install keeps today's text, and the sign-in page served before sign-in
keeps the plain command so it does not tell a visitor how the gateway is
installed.

## Why

- On a container install the host has no `kirocrew token`, so the plain
  command sent a Docker user to a command that does not exist where they type
  it.
- `kirocrew` is the name the README and the compose file give the container.
- The page served before sign-in would otherwise tell a visitor how the
  gateway is installed.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32364#issuecomment-6815855426 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/32364 -- the issue.
