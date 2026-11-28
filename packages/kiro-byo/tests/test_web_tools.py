from __future__ import annotations

import socket
import unittest
from unittest.mock import MagicMock, patch

from kiro_byo.web_tools import (
    SafeWebClient,
    WebToolError,
    _PinnedHTTPSConnection,
    _connect_socket,
    _parse_local_proxy,
    _socks5_connect,
    _ProxyHTTPSConnection,
    _ProxyHTTPConnection,
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

    def test_unicode_host_is_idna_for_dns_http_and_tls(self) -> None:
        client = SafeWebClient()
        with patch("kiro_byo.web_tools.socket.getaddrinfo", side_effect=_public_dns) as dns:
            endpoint = client._resolve_public_url("https://例え.テスト/日本語")
        self.assertEqual(endpoint.host, "xn--r8jz45g.xn--zckzah")
        self.assertEqual(dns.call_args.args[0], endpoint.host)

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

    def test_unicode_web_path_and_query_are_escaped_for_http_request_line(self) -> None:
        import http.client
        import urllib.parse
        client = SafeWebClient()
        target = client._request_target(urllib.parse.urlsplit("https://example.com/日本語?q=中文&x=%2F"))
        self.assertEqual(target, "/%E6%97%A5%E6%9C%AC%E8%AA%9E?q=%E4%B8%AD%E6%96%87&x=%2F")
        http.client.HTTPConnection._encode_request(None, f"GET {target} HTTP/1.1")

    def test_search_falls_back_to_bing_rss_after_primary_timeout(self) -> None:
        client = SafeWebClient()
        feed = """<?xml version="1.0"?><rss><channel><item>
        <title>日本語 title</title><link>https://example.org/article</link>
        <description>&lt;b&gt;中文&lt;/b&gt; snippet</description>
        </item></channel></rss>""".encode("utf-8")
        with patch.object(client, "_request_bytes", side_effect=[
            WebToolError("request timed out"),
            ("https://www.bing.com/search", "application/rss+xml", feed),
        ]) as requests:
            result = client.search("特撮 中文", max_results=2)
        self.assertEqual(requests.call_count, 2)
        self.assertIn("source: Bing RSS", result)
        self.assertIn("日本語 title", result)
        self.assertIn("中文 snippet", result)
        self.assertIn("format=rss", requests.call_args_list[1].args[0])
        self.assertLessEqual(sum(k.kwargs["timeout_seconds"] for k in requests.call_args_list), 15.01)

    def test_search_explains_both_failures_without_returning_fake_results(self) -> None:
        client = SafeWebClient()
        with patch.object(client, "_request_bytes", side_effect=[WebToolError("timeout"), WebToolError("HTTP 403")]):
            with self.assertRaisesRegex(WebToolError, "Bing RSS") as ctx:
                client.search("日本語")
        self.assertIn("DuckDuckGo", str(ctx.exception))

    def test_local_proxy_rejects_remote_and_credentials(self) -> None:
        for bad in (
            "http://192.168.1.1:7890", "http://proxy.example.com:7890",
            "http://127.0.0.1:7890/a", "http://u:p@127.0.0.1:7890",
            "ftp://127.0.0.1:7890", "http://127.0.0.1:0",
        ):
            with self.subTest(bad=bad), self.assertRaises(WebToolError):
                _parse_local_proxy(bad, source="test")
        self.assertEqual(_parse_local_proxy("127.0.0.1:7890", source="test").port, 7890)
        self.assertEqual(_parse_local_proxy("socks5://[::1]:10808", source="test").scheme, "socks5")

    def test_system_proxy_is_selected_for_validated_public_target(self) -> None:
        client = SafeWebClient(proxy_mode="auto", proxy_url="")
        with patch("kiro_byo.web_tools.socket.getaddrinfo", side_effect=_public_dns):
            endpoint = client._resolve_public_url("https://example.com/")
        with patch("kiro_byo.web_tools.urllib.request.proxy_bypass", return_value=False), patch(
            "kiro_byo.web_tools.urllib.request.getproxies",
            return_value={"https": "http://127.0.0.1:7890"},
        ):
            result = client._proxy_for(endpoint)
        self.assertEqual(result.host, "127.0.0.1")
        self.assertEqual(result.source, "system")

    def test_direct_mode_opt_out_of_proxy(self) -> None:
        client = SafeWebClient(proxy_mode="direct", proxy_url="http://127.0.0.1:7890")
        with patch("kiro_byo.web_tools.socket.getaddrinfo", side_effect=_public_dns):
            endpoint = client._resolve_public_url("https://example.com/")
        with patch("kiro_byo.web_tools.urllib.request.getproxies") as getter:
            self.assertIsNone(client._proxy_for(endpoint))
        getter.assert_not_called()

    def test_fake_ip_tun_bypasses_separate_proxy_to_keep_mapping(self) -> None:
        client = SafeWebClient(proxy_mode="auto", proxy_url="http://127.0.0.1:7890")
        with patch("kiro_byo.web_tools.socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("198.18.0.42", 443)),
        ]):
            endpoint = client._resolve_public_url("https://example.com/")
        self.assertIsNone(client._proxy_for(endpoint))

    def test_system_nonlocal_proxy_is_rejected_not_silently_direct(self) -> None:
        client = SafeWebClient(proxy_mode="auto", proxy_url="")
        with patch("kiro_byo.web_tools.socket.getaddrinfo", side_effect=_public_dns):
            endpoint = client._resolve_public_url("https://example.com/")
        with patch("kiro_byo.web_tools.urllib.request.proxy_bypass", return_value=False), patch(
            "kiro_byo.web_tools.urllib.request.getproxies",
            return_value={"https": "http://remote-proxy.test:3128"},
        ):
            with self.assertRaisesRegex(WebToolError, "127.0.0.1"):
                client._proxy_for(endpoint)

    def test_https_proxy_connects_validated_ip_but_uses_domain_for_tls(self) -> None:
        client = SafeWebClient(proxy_mode="auto", proxy_url="")
        with patch("kiro_byo.web_tools.socket.getaddrinfo", side_effect=_public_dns):
            endpoint = client._resolve_public_url("https://example.com/")
        proxy = _parse_local_proxy("http://127.0.0.1:7890", source="test")
        raw_socket = MagicMock()
        secure_socket = MagicMock()
        context = MagicMock()
        context.wrap_socket.return_value = secure_socket
        with patch("kiro_byo.web_tools.socket.socket", return_value=raw_socket), patch.object(
            _ProxyHTTPSConnection, "_tunnel",
        ) as tunnel:
            conn = _ProxyHTTPSConnection(endpoint, proxy, timeout=3)
            conn._context = context
            conn.connect()
        raw_socket.connect.assert_called_once_with(("127.0.0.1", 7890))
        self.assertEqual(conn._tunnel_host, _PUBLIC_V4)
        tunnel.assert_called_once()
        context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="example.com")
        self.assertIs(conn.sock, secure_socket)

    def test_socks5_uses_pinned_ip_not_hostname(self) -> None:
        client = SafeWebClient()
        with patch("kiro_byo.web_tools.socket.getaddrinfo", side_effect=_public_dns):
            endpoint = client._resolve_public_url("https://example.com/")
        raw_socket = MagicMock()
        # greeting, success with IPv4 bound address, bound address, port
        replies = iter([b"\x05\x00", b"\x05\x00\x00\x01", b"\x00\x00\x00\x00", b"\x00\x00"])
        raw_socket.recv.side_effect = lambda n: next(replies)
        _socks5_connect(raw_socket, endpoint)
        connect_request = raw_socket.sendall.call_args_list[1].args[0]
        self.assertIn(socket.inet_aton(_PUBLIC_V4), connect_request)
        self.assertNotIn(b"example.com", connect_request)

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
