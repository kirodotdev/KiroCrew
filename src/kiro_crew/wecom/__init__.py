"""WeCom channel for KiroCrew via WeCom (企业微信) AI-bot.

DM-only, outbound WebSocket long-connection to ``wss://openws.work.weixin.qq.com``.
Reuses the existing session machinery (SessionManager / provider.stream); this
package only does WeCom I/O and maps it onto a small render layer.
"""

from __future__ import annotations
