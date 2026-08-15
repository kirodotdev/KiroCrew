"""WhatsApp channel via a QR-linked personal account (WhatsApp Web protocol).

The channel pairs as a *linked device* on the operator's own WhatsApp account
(no bot identity, no Business API): scan a QR code from Settings > Channels
once, and the session persists in a local database. Inbound and outbound ride
the shared ``kiro_crew.messaging`` pipeline; the protocol layer is `neonize
<https://github.com/krypton-byte/neonize>`_ (Apache-2.0 Python bindings to
whatsmeow), an optional dependency installed with ``kirocrew[whatsapp]``.

Because the agent sends *as the operator*, two properties are load-bearing:

- **Echo discipline** (:mod:`kiro_crew.whatsapp.echo`): the account's own
  messages come back with ``from_me=True`` both when the operator types and
  when this channel sends. Sent-message-ID tracking is what tells them apart;
  see the module docstring for the contract.
- **Deny-by-default reach** (:mod:`kiro_crew.whatsapp.transport`): only the
  operator (dm_policy ``self``, the default) commands the agent; groups are
  ignored unless explicitly configured, and non-operator senders never gain
  tool approval or session control (:mod:`kiro_crew.whatsapp.group_gate`).

Uses the unofficial WhatsApp Web protocol: automation on a personal account is
against WhatsApp's Terms of Service and carries a small ban risk for the
linked number. The channel keeps volumes personal-scale (no broadcast paths)
and the Settings panel discloses the risk.

Dependency direction is ``whatsapp -> messaging`` (allowed); ``messaging``
never imports ``whatsapp``.
"""
