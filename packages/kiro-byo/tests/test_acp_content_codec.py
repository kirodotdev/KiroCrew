"""Codec contract tests: ACP prompt conversion and recovery stay independent.

These tests deliberately use the public AcpServer delegating surface as well as
the underlying codec so later session/control-plane refactors cannot drift.
"""
from __future__ import annotations

import itertools
import unittest

from kiro_byo.acp_content import (
    prompt_blocks_from_user_message,
    prompt_message,
    prompt_text,
)
from kiro_byo.acp_server import AcpServer


class AcpContentCodecTests(unittest.TestCase):
    def test_plain_text_join_and_non_text_items(self) -> None:
        params = {"prompt": [
            None,
            {"type": "text", "text": "你好 "},
            {"type": "text", "text": "world"},
            {"type": "text", "text": 8},
            {"type": "unknown", "text": "ignored"},
        ]}
        self.assertEqual(prompt_text(params), "你好 world")
        self.assertEqual(prompt_message(params), ({"role": "user", "content": "你好 world"}, False))
        self.assertEqual(AcpServer.prompt_text(params), prompt_text(params))
        self.assertEqual(AcpServer.prompt_message(params), prompt_message(params))
        self.assertEqual(prompt_message({}), ({"role": "user", "content": ""}, False))

    def test_resource_link_and_embedded_resource_baseline(self) -> None:
        params = {"prompt": [
            {"type": "resource_link", "name": "doc", "uri": "file:///tmp/doc.txt", "description": "reference"},
            {"type": "resource_link", "name": "missing", "uri": "  "},
            {"type": "resource", "resource": {"uri": "notes", "text": "embedded facts", "mimeType": "text/plain"}},
            {"type": "resource", "resource": {"uri": "file:///binary", "blob": "AAAA", "mimeType": "application/pdf"}},
        ]}
        message, has_image = prompt_message(params)
        self.assertFalse(has_image)
        self.assertEqual(message, {
            "role": "user",
            "content": "[Referenced resource: doc] file:///tmp/doc.txt -- reference"
                       "[Embedded resource: notes]\nembedded facts"
                       "[Unsupported embedded binary resource: file:///binary (application/pdf)]",
        })

    def test_mixed_text_image_and_embedded_image_preserve_order(self) -> None:
        params = {"prompt": [
            {"type": "text", "text": "first"},
            {"type": "image", "mimeType": "image/jpeg", "data": "YWJj"},
            {"type": "resource", "resource": {"uri": "diagram", "blob": "YWJjZA==", "mimeType": "image/png"}},
            {"type": "text", "text": "last"},
        ]}
        message, has_image = prompt_message(params)
        self.assertTrue(has_image)
        self.assertEqual(message["content"], [
            {"type": "text", "text": "first"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,YWJj"}},
            {"type": "text", "text": "[Embedded image resource: diagram]"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,YWJjZA=="}},
            {"type": "text", "text": "last"},
        ])
        self.assertEqual(AcpServer.prompt_message(params), (message, has_image))

    def test_recovery_reprojects_saved_user_message_blocks(self) -> None:
        self.assertEqual(prompt_blocks_from_user_message({"content": "hello"}), [{"type": "text", "text": "hello"}])
        self.assertEqual(prompt_blocks_from_user_message({"content": None}), [])
        saved = {"role": "user", "content": [
            {"type": "text", "text": "A"},
            {"type": "image_url", "image_url": {"url": "data:image/webp;base64,YWJj"}},
            {"type": "image_url", "image_url": {"url": "https://external.invalid/image.png"}},
            {"type": "image_url", "image_url": "data:image/png;base64,ZA=="},
            None,
        ]}
        expected = [
            {"type": "text", "text": "A"},
            {"type": "image", "mimeType": "image/webp", "data": "YWJj"},
            {"type": "image", "mimeType": "image/png", "data": "ZA=="},
        ]
        self.assertEqual(prompt_blocks_from_user_message(saved), expected)
        self.assertEqual(AcpServer._prompt_blocks_from_user_message(saved), expected)
        self.assertEqual(prompt_message({"prompt": expected})[0], {
            "role": "user", "content": [
                {"type": "text", "text": "A"},
                {"type": "image_url", "image_url": {"url": "data:image/webp;base64,YWJj"}},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,ZA=="}},
            ]
        })

    def test_permutations_keep_compatibility_with_acp_server(self) -> None:
        blocks = [
            {"type": "text", "text": "x"},
            {"type": "resource_link", "uri": "local://ref", "name": "ref"},
            {"type": "image", "data": "Yg=="},
        ]
        for ordered in itertools.permutations(blocks):
            with self.subTest(order=[block["type"] for block in ordered]):
                params = {"prompt": list(ordered)}
                self.assertEqual(AcpServer.prompt_message(params), prompt_message(params))
                self.assertEqual(AcpServer.prompt_text(params), prompt_text(params))
