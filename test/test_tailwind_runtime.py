"""The Tailwind runtime carried inside a frame document instead of linked from it.

A sandboxed frame's own request for ``/vendor/tailwindcss-browser.js`` goes out
with no cookie, so a dashboard behind a proxy that authenticates by cookie (a
single-sign-on tunnel) has every such request refused and every widget renders
unstyled. The gateway therefore serves frame documents with the runtime already
inside them. What that rests on is asserted here:

* the tag ``buildSrcdoc`` emits is the one replaced, and only in the head
* a document the inliner does not recognise is served exactly as minted
* a bundle that cannot sit safely inside ``<script>`` is never inlined
* a rebuilt bundle is picked up without a restart
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from conftest import assert_rejected_without_backtracking
from kiro_crew import publish_sync
from kiro_crew import tailwind_runtime as tr

_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_JS = "/*tw-runtime*/globalThis.__tw=1;"
_INLINE = f"<script>{_RUNTIME_JS}</script>".encode()


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "tailwindcss-browser.js"
    path.write_text(_RUNTIME_JS, encoding="utf-8")
    monkeypatch.setattr(tr, "TAILWIND_RUNTIME_FILE", path)
    monkeypatch.setattr(tr, "_cached", None)
    return path


_NONCE = "0123456789abcdef0123456789abcdef"


def _emitted_tag(src: str, nonce: str | None = None) -> str:
    """The runtime tag exactly as ``buildSrcdoc`` (widgetSrcdoc.ts) emits it:
    with ``nonce`` when the page's own scripts are blocked, else with ``onerror``.

    Rendered from the TypeScript source rather than copied here, so a change to
    the tag's shape fails this file instead of silently linking the runtime
    again behind a cookie-authenticating proxy.
    """
    ts = (_ROOT / "website" / "src" / "lib" / "widgetSrcdoc.ts").read_text(encoding="utf-8")
    templates = re.search(
        r"\(_match, src\) => nonce\s*\?\s*`(<script [^`]*</script>)`\s*:\s*`(<script [^`]*</script>)`",
        ts,
    )
    event = re.search(r"const TW_ERROR_EVENT = '([^']+)'", ts)
    handler = re.search(r"const TW_ERROR_INLINE_HANDLER =\s*`([^`]+)`", ts)
    assert templates and event and handler, "widgetSrcdoc.ts does not emit the tag rendered here"
    if nonce is not None:
        return templates.group(1).replace("${src}", src).replace("${nonce}", nonce)
    onerror = handler.group(1).replace("${TW_ERROR_EVENT}", event.group(1))
    return templates.group(2).replace("${src}", src).replace("${TW_ERROR_INLINE_HANDLER}", onerror)


def _doc(head_extra: str = "", body: str = '<p class="p-4">x</p>') -> bytes:
    return (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="script-src \'unsafe-inline\'">'
        f"{head_extra}<style>body{{margin:0}}</style></head>"
        f'<body class="dark">{body}</body></html>'
    ).encode()


@pytest.mark.parametrize(
    "src",
    [
        "https://someone.w.tunnels.example/vendor/tailwindcss-browser.js",
        "http://127.0.0.1:5476/vendor/tailwindcss-browser.js",
        "http://127.0.0.1:5476/kirocrew/vendor/tailwindcss-browser.js",
    ],
)
def test_the_tag_buildsrcdoc_emits_is_replaced_by_the_runtime(runtime: Path, src: str) -> None:
    tag = _emitted_tag(src)
    doc = _doc(tag)

    out = tr.inline_into_document(doc)

    assert _INLINE in out
    assert b"<script src=" not in out
    # Nothing but the tag changed.
    assert out.replace(_INLINE, b"") == doc.replace(tag.encode(), b"")


def test_the_inline_copy_keeps_the_tags_nonce(runtime: Path) -> None:
    # A page whose own scripts are blocked admits scripts by nonce only, so an
    # inline copy without the tag's nonce would be refused and the page unstyled.
    tag = _emitted_tag("http://127.0.0.1:5476/vendor/tailwindcss-browser.js", nonce=_NONCE)
    doc = _doc(tag)

    out = tr.inline_into_document(doc)

    inline = f'<script nonce="{_NONCE}">{_RUNTIME_JS}</script>'.encode()
    assert inline in out
    assert out.replace(inline, b"") == doc.replace(tag.encode(), b"")


@pytest.mark.parametrize("nonce", ["a b", "", "a;b", "<x>"])
def test_a_tag_with_an_odd_nonce_is_served_as_minted(runtime: Path, nonce: str) -> None:
    tag = (
        '<script src="http://127.0.0.1:5476/vendor/tailwindcss-browser.js" '
        f'crossorigin="anonymous" nonce="{nonce}"></script>'
    )
    doc = _doc(tag)
    assert tr.inline_into_document(doc) == doc


def test_a_head_of_unterminated_script_openers_is_scanned_in_linear_time(runtime: Path) -> None:
    # The head is whatever the minting caller sent. A run of `<script ` openers
    # with no `>` made each candidate rescan the rest of the head (quadratic).
    def reject(doc: bytes) -> None:
        assert tr.inline_into_document(doc) == doc

    assert_rejected_without_backtracking(reject, lambda n: _doc("<script " * n))


def test_repeated_src_attributes_are_scanned_in_linear_time(runtime: Path) -> None:
    def reject(doc: bytes) -> None:
        assert tr.inline_into_document(doc) == doc

    assert_rejected_without_backtracking(
        reject,
        lambda n: _doc(
            "<script" + ' src="http://127.0.0.1:5476/vendor/tailwindcss-browser.js"' * n + ">"
        ),
    )


def test_a_document_without_the_tag_is_served_as_minted(runtime: Path) -> None:
    doc = _doc('<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>')
    assert tr.inline_into_document(doc) == doc


def test_a_document_without_a_head_is_served_as_minted(runtime: Path) -> None:
    doc = b"<p>no head</p>" + _emitted_tag("/vendor/tailwindcss-browser.js").encode()
    assert tr.inline_into_document(doc) == doc


def test_a_lookalike_in_the_body_is_left_alone(runtime: Path) -> None:
    # The body holds the model's HTML. Only the head carries the tag buildSrcdoc
    # wrote, so nothing the model writes decides what gets replaced.
    doc = _doc(body=_emitted_tag("http://127.0.0.1:5476/vendor/tailwindcss-browser.js"))
    assert tr.inline_into_document(doc) == doc


def test_only_the_head_tag_is_replaced(runtime: Path) -> None:
    head_tag = _emitted_tag("http://127.0.0.1:5476/vendor/tailwindcss-browser.js")
    body_tag = '<script src="/vendor/tailwindcss-browser.js"></script>'
    out = tr.inline_into_document(_doc(head_tag, body=body_tag))

    assert out.count(_INLINE) == 1
    assert out.index(_INLINE) < out.index(b"</head>")
    assert body_tag.encode() in out


def test_a_missing_bundle_leaves_the_link_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unbuilt checkout: the link still loads wherever no cookie is needed.
    monkeypatch.setattr(tr, "TAILWIND_RUNTIME_FILE", tmp_path / "absent.js")
    monkeypatch.setattr(tr, "_cached", None)
    doc = _doc(_emitted_tag("http://127.0.0.1:5476/vendor/tailwindcss-browser.js"))
    assert tr.inline_into_document(doc) == doc


def test_an_empty_bundle_is_not_inlined(runtime: Path) -> None:
    runtime.write_text("", encoding="utf-8")
    doc = _doc(_emitted_tag("http://127.0.0.1:5476/vendor/tailwindcss-browser.js"))
    assert tr.inline_into_document(doc) == doc


def test_an_unreadable_bundle_is_not_inlined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A directory at the asset path stats fine and raises on read.
    monkeypatch.setattr(tr, "TAILWIND_RUNTIME_FILE", tmp_path)
    monkeypatch.setattr(tr, "_cached", None)
    assert tr.runtime_script_tag() is None


def test_a_bundle_that_opens_an_html_comment_is_not_inlined(runtime: Path) -> None:
    # `<!--` then `<script` puts the tokenizer in its double-escaped state, where
    # the closing `</script>` does not end the element and the rest of the
    # document becomes script. Linking is the safe outcome.
    runtime.write_text('var s="<!--<script>";', encoding="utf-8")
    doc = _doc(_emitted_tag("http://127.0.0.1:5476/vendor/tailwindcss-browser.js"))
    assert tr.inline_into_document(doc) == doc
    assert tr.script_text('var s="<!--";') is None


@pytest.mark.parametrize("close_tag", ["</script>", "</SCRIPT>", "</Script >", "</sCrIpT/"])
def test_script_terminators_in_the_bundle_are_neutralised(runtime: Path, close_tag: str) -> None:
    runtime.write_text(f'var s="{close_tag}";', encoding="utf-8")
    out = tr.inline_into_document(
        _doc(_emitted_tag("http://127.0.0.1:5476/vendor/tailwindcss-browser.js"))
    )
    assert f'"<\\/{close_tag[2:]}";'.encode() in out
    # One script element: the runtime's own, not a truncated one plus leaked text.
    assert out.lower().count(b"</script") == 1


def test_a_rebuilt_bundle_is_picked_up_without_a_restart(runtime: Path) -> None:
    doc = _doc(_emitted_tag("http://127.0.0.1:5476/vendor/tailwindcss-browser.js"))
    assert _INLINE in tr.inline_into_document(doc)

    runtime.write_text("/*rebuilt*/", encoding="utf-8")
    stat = runtime.stat()
    os.utime(runtime, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    out = tr.inline_into_document(doc)
    assert b"<script>/*rebuilt*/</script>" in out
    assert _INLINE not in out


def test_publish_and_the_frame_channel_read_the_same_file() -> None:
    assert publish_sync._TAILWIND_RUNTIME_FILE == tr.TAILWIND_RUNTIME_FILE
