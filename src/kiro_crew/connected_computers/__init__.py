"""Connected computers: a chat's workspace tools run on the user's own computer.

Terms used across this package (see ``docs/system-specs/modules/providers.md``,
"Connected computers"):

* **local machine** -- the user's execution device: its files, tools, shell and the
  local MCP servers the user explicitly exposed. It runs a *connector* under the
  local gateway (the *local controller*), which owns consent, the selected root
  and the credential.
* **remote machine** -- the always-on Kiro Crew gateway that runs the model
  session, the agent loop, memory and approvals.

The local machine dials OUT: its connector opens one WebSocket to the remote
gateway through the Remote Crew SSH local forward it already has, and every
request and result rides that socket. Nothing listens on the local machine for
the gateway, and no relay is added.

Everything here is behind the ``connected_computers`` feature preview, default
off, on both machines (:mod:`kiro_crew.connected_computers.flag`).
"""
