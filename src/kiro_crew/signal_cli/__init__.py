"""Signal channel -- a chat surface reached through ``signal-cli``.

The gateway spawns ``signal-cli -a <account> jsonRpc`` as a long-lived child and
speaks newline-framed JSON-RPC 2.0 to it over stdio (the iMessage bridge's
shape, and the same peer). No daemon, no port, no webhook, so no new inbound
network surface; signal-cli keeps the account's keys in its own store, so the
gateway holds no credential for this channel.

Two topologies work with the same code:

* **Dedicated number.** signal-cli is registered as its own account and the
  owner DMs it. Replies go back to the sender.
* **Linked device of the owner's own account.** signal-cli is linked to the
  owner's phone and the owner writes in "Note to Self". Those arrive as
  ``syncMessage.sentMessage`` envelopes addressed to the account itself, and
  replies go back to Note to Self.

v1 is DM-only (groups are refused), text-only, and deny-by-default: an empty
``signal.allowed_numbers`` answers nobody. The package is ``signal_cli`` rather
than ``signal`` because the latter would shadow the standard library module.
"""

from __future__ import annotations
