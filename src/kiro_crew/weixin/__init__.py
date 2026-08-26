"""Weixin (personal WeChat) channel via Tencent's iLink Bot API.

DM-first, long-poll transport against ``https://ilinkai.weixin.qq.com``. This is
the *personal* WeChat path (QR-login bot identity, e.g. ``...@im.bot``) and is
distinct from the enterprise WeCom channel in :mod:`kiro_crew.wechat`
(``wss://openws.work.weixin.qq.com``).

Setup is dashboard-driven (Settings > Channels > WeChat): the user scans a QR
code, the server persists the returned bot token + account id into the shared
credential store, and the transport reads them on boot -- mirroring every other
channel (no terminal wizard).

Protocol layer ported from Nous Research's Hermes Agent ``weixin`` adapter
(MIT License, https://github.com/NousResearch/hermes-agent) and re-expressed
against KiroCrew's :class:`~kiro_crew.messaging.transport.MessagingTransport`
contract.
"""
