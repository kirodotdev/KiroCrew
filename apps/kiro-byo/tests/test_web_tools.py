from __future__ import annotations

import socket
import unittest
from unittest.mock import MagicMock, patch

from kiro_byo.web_tools import (
    SafeWebClient,
    WebToolError,
    _PinnedHTTPSConnection,
    _connect_socket,
)


_PUBLIC_V4 = "93.184.216.34"


def _public_dns(host: str, port: int, *args, **kwargs):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (_PUBLIC_V4, port),
        )
    ]


class SafeWebClientTests(unittest.TestCase):
    def test_private_local_and_credential_urls_are_blocked(self) -> None:
        client = SafeWebClient()
        for url in (
            "http://127.0.0.1/secret",
            "http://localhost/secret",
            "http://10.0.0.1/secret",
            "http://169.254.169.254/latest/meta-data/",
            "https://user:pass@example.com/secret",
        ):
            with self.subTest(url=url), self.assertRaises(WebToolError):
                client._assert_public_url(url)

    def test_mixed_public_private_dns_answer_pins_public_candidate(self) -> None:
        client = SafeWebClient()
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (_PUBLIC_V4, 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
        ]
        with patch("kiro_byo.web_tools.socket.getaddrinfo", return_value=answers):
            endpoint = client._resolve_public_url("https://example.com/docs")
        self.assertEqual(endpoint.sockaddr, (_PUBLIC_V4, 443))


    def test_dns_fake_ip_range_is_allowed_but_literal_fake_ip_is_blocked(self) -> None:
        client = SafeWebClient()
        fake_ip = "198.18.0.42"
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (fake_ip, 443)),
        ]
        with patch("kiro_byo.web_tools.socket.getaddrinfo", return_value=answers):
            endpoint = client._resolve_public_url("https://example.com/news")

        self.assertEqual(endpoint.host, "example.com")
        self.assertEqual(endpoint.sockaddr, (fake_ip, 443))
        with self.assertRaisesRegex(WebToolError, "local/private"):
            client._resolve_public_url(f"https://{fake_ip}/news")

    def test_public_answer_is_preferred_over_dns_fake_ip(self) -> None:
        client = SafeWebClient()
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("198.18.0.42", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (_PUBLIC_V4, 443)),
        ]
        with patch("kiro_byo.web_tools.socket.getaddrinfo", return_value=answers):
            endpoint = client._resolve_public_url("https://example.com/news")
        self.assertEqual(endpoint.sockaddr, (_PUBLIC_V4, 443))

    def test_fake_ip_plus_real_private_dns_answer_is_still_rejected(self) -> None:
        client = SafeWebClient()
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("198.18.0.42", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
        ]
        with patch("kiro_byo.web_tools.socket.getaddrinfo", return_value=answers):
            with self.assertRaisesRegex(WebToolError, "blocked destinations"):
                client._resolve_public_url("https://example.com/news")

    def test_resolution_returns_pinned_public_sockaddr(self) -> None:
        client = SafeWebClient()
        with patch("kiro_byo.web_tools.socket.getaddrinfo", side_effect=_public_dns) as resolver:
            endpoint = client._resolve_public_url("https://example.com/docs?q=1")

        resolver.assert_called_once()
        self.assertEqual(endpoint.host, "example.com")
        self.assertEqual(endpoint.port, 443)
        self.assertEqual(endpoint.sockaddr, (_PUBLIC_V4, 443))
        self.assertEqual(endpoint.family, socket.AF_INET)

    def test_resolve_then_connect_does_not_reresolve_hostname(self) -> None:
        """Regression test for DNS rebinding / check-to-use TOCTOU.

        If a second DNS lookup occurred during connect, the side effect below
        would return 127.0.0.1. The hardened path must resolve exactly once and
        connect directly to the first validated sockaddr.
        """
        client = SafeWebClient()
        rebinding_answers = [
            [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (_PUBLIC_V4, 443),
                )
            ],
            [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("127.0.0.1", 443),
                )
            ],
        ]
        fake_socket = MagicMock()
        with patch(
            "kiro_byo.web_tools.socket.getaddrinfo",
            side_effect=rebinding_answers,
        ) as resolver, patch(
            "kiro_byo.web_tools.socket.socket",
            return_value=fake_socket,
        ):
            endpoint = client._resolve_public_url("https://example.com/")
            _connect_socket(endpoint, timeout=3.0)

        self.assertEqual(resolver.call_count, 1)
        fake_socket.connect.assert_called_once_with((_PUBLIC_V4, 443))

    def test_https_uses_original_hostname_for_tls_sni(self) -> None:
        client = SafeWebClient()
        with patch("kiro_byo.web_tools.socket.getaddrinfo", side_effect=_public_dns):
            endpoint = client._resolve_public_url("https://example.com/")

        raw_socket = MagicMock()
        tls_socket = MagicMock()
        context = MagicMock()
        context.wrap_socket.return_value = tls_socket

        with patch("kiro_byo.web_tools._connect_socket", return_value=raw_socket):
            conn = _PinnedHTTPSConnection(endpoint, timeout=3.0, context=context)
            conn.connect()

        context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="example.com")
        self.assertIs(conn.sock, tls_socket)

    def test_redirect_to_private_ip_is_blocked_before_second_request(self) -> None:
        client = SafeWebClient()
        with patch("kiro_byo.web_tools.socket.getaddrinfo", side_effect=_public_dns), patch.object(
            client,
            "_request_once",
            return_value=(302, "text/html", b"", "http://127.0.0.1/secret"),
        ) as request_once:
            with self.assertRaisesRegex(WebToolError, "local/private"):
                client._request_bytes("https://example.com/start")

        self.assertEqual(request_once.call_count, 1)

    def test_fetch_turns_html_into_readable_text(self) -> None:
        client = SafeWebClient(max_bytes=10000)
        page = b"<html><head><style>x</style></head><body><h1>Hello</h1><p>world <b>from web</b></p><script>bad()</script></body></html>"
        with patch.object(
            client,
            "_request_bytes",
            return_value=("https://example.com/docs", "text/html; charset=utf-8", page),
        ):
            result = client.fetch("https://example.com/docs")
        self.assertIn("Hello", result)
        self.assertIn("world from web", result)
        self.assertNotIn("bad()", result)

    def test_search_parses_titles_urls_and_snippets(self) -> None:
        client = SafeWebClient(max_bytes=10000)
        page = b'''<html><body>
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Example A</a>
<a class="result__snippet">first snippet</a></div>
<div class="result"><a class="result__a" href="https://example.org/b">Example B</a>
<div class="result__snippet">second snippet</div></div>
</body></html>'''
        with patch.object(
            client,
            "_request_bytes",
            return_value=("https://html.duckduckgo.com/html/?q=test", "text/html; charset=utf-8", page),
        ):
            result = client.search("test", max_results=2)
        self.assertIn("1. Example A", result)
        self.assertIn("https://example.com/a", result)
        self.assertIn("first snippet", result)
        self.assertIn("2. Example B", result)

    def test_fetch_rejects_binary_content(self) -> None:
        client = SafeWebClient(max_bytes=10000)
        with patch.object(
            client,
            "_request_bytes",
            return_value=("https://example.com/a.png", "image/png", b"\x89PNG"),
        ):
            with self.assertRaisesRegex(WebToolError, "non-text"):
                client.fetch("https://example.com/a.png")


if __name__ == "__main__":
    unittest.main()
