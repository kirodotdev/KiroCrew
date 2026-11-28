# A same-channel signed update installs with one click

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-26
Supersedes: 2026-11-10-updates-never-restart-a-running-gateway.md (in part: a clicked same-channel update builds beside the running gateway; nothing restarts it)

## Decision

On a managed-venv install, the dashboard owner's Update installs a newer release
of the same channel, from the official update CDN, with that one click: it is
built beside the running gateway and starts at the next restart a person
chooses, and the running gateway is never restarted for it. The host approval
(`kirocrew update approve` on the gateway host) stays for everything else: a
channel move, a downgrade, an install whose update source is overridden
(`KIROCREW_CDN_BASE`), and a host whose updates a policy manages.

## Why

The maintainer's ruling: a dashboard sign-in can already turn on "Update the
gateway automatically", which installs exactly these releases with no approval
at all, so the host step protects nothing for this case. The cases that keep it
are the ones automatic updates never take. Telling a local browser from a remote
one is not part of the ruling: SSH and SSM port forwards, cloud dev-box port
proxies and remote relays all reach the gateway from loopback, so the peer
address cannot tell them apart.

It replaces one part of the decision entry "updates never restart a running
gateway" (`docs/decisions/2026-11-10-updates-never-restart-a-running-gateway.md`):
a clicked Update no longer only arms the update for the next start when it is a
same-channel release on a managed venv; it builds that release beside the
running gateway. The rest of that entry stands: nothing restarts the running
gateway, on any path.

## Evidence

- The maintainer's ruling, section 1:
  https://github.com/kirodotdev/KiroCrew/issues/43374
