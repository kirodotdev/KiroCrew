"""Feishu (Lark / 飞书) channel.

The package is import-safe WITHOUT the optional ``lark-oapi`` dependency: the
SDK is imported lazily inside :mod:`kiro_crew.feishu.client` methods, so the
channel roster in :mod:`kiro_crew.channels` can import ``maybe_start_feishu``
on any build. See ``src/kiro_crew/docs/feishu-integration.md``.
"""
