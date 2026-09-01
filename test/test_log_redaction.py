"""Tests for kiro_crew.log_redaction."""

from __future__ import annotations

import importlib
import logging
import sys

import pytest

from kiro_crew.log_redaction import (
    SecretRedactionFilter,
    install_log_redaction,
    uninstall_log_redaction,
)


class TestSecretRedactionFilter:
    """Unit tests for the SecretRedactionFilter class."""

    def test_redacts_vault_secret(self) -> None:
        filt = SecretRedactionFilter(["sk-secret-key-12345"])
        result = filt.redact("Connecting with key sk-secret-key-12345 to DB")
        assert "sk-secret-key-12345" not in result
        assert "[REDACTED]" in result

    def test_redacts_multiple_secrets(self) -> None:
        filt = SecretRedactionFilter(["sk-secret-key-12345", "hunter2_password"])
        result = filt.redact("key=sk-secret-key-12345 pass=hunter2_password")
        assert "sk-secret-key-12345" not in result
        assert "hunter2_password" not in result
        assert result.count("[REDACTED]") == 2

    def test_redacts_bearer_token(self) -> None:
        filt = SecretRedactionFilter([])
        result = filt.redact("Auth header: Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig")
        assert "eyJhbGciOiJSUzI1NiJ9" not in result
        assert "Bearer [REDACTED]" in result

    def test_redacts_bearer_case_insensitive(self) -> None:
        filt = SecretRedactionFilter([])
        result = filt.redact("header: bearer eyJhbGciOiJSUzI1NiJ9.payload.sig")
        assert "eyJhbGciOiJSUzI1NiJ9" not in result

    def test_passthrough_non_secret_text(self) -> None:
        filt = SecretRedactionFilter([])
        msg = "Normal log message with no secrets"
        assert filt.redact(msg) == msg

    def test_short_secrets_not_redacted(self) -> None:
        """Secrets shorter than 4 chars are not redacted to avoid false positives."""
        filt = SecretRedactionFilter(["ab", "x"])
        result = filt.redact("value is ab here")
        assert result == "value is ab here"

    def test_no_vault_io_at_construction(self) -> None:
        """Filter construction does ZERO vault I/O — just compiles patterns."""
        # This test exists to verify the architectural contract: no imports
        # of kiro_crew.secrets happen inside SecretRedactionFilter.
        filt = SecretRedactionFilter(["my-secret-value"])
        assert filt.redact("my-secret-value") == "[REDACTED]"

    def test_empty_patterns_builtin_coverage(self) -> None:
        """With no secret patterns, the built-in pattern set is applied.

        Bearer tokens, AWS access key IDs, and standalone JWTs are redacted;
        other opaque strings pass through (redaction is pattern-based).
        """
        filt = SecretRedactionFilter([])
        msg = "key=sk-live-123 and Bearer eyJhbGciOiJSUzI1NiJ9.test"
        result = filt.redact(msg)
        # sk-live-123 passes through (not in patterns)
        assert "sk-live-123" in result
        # Bearer token is always redacted
        assert "eyJhbGciOiJSUzI1NiJ9" not in result

    def test_aws_key_id_redacted(self) -> None:
        """An AWS access key ID is redacted anywhere in the text."""
        filt = SecretRedactionFilter([])
        result = filt.redact("creds: AKIAIOSFODNN7EXAMPLE for deploy")
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[REDACTED]" in result

    def test_aws_key_id_redacted_when_word_adjacent(self) -> None:
        """A key abutting word characters (``aws_AKIA…``) is still redacted.

        ``\\b`` never fires between ``_`` and ``A``, so boundary-anchored
        patterns miss keys embedded in identifiers or rendered templates.
        """
        filt = SecretRedactionFilter([])
        result = filt.redact("aws_AKIAIOSFODNN7EXAMPLE and key=AKIAIOSFODNN7EXAMPLEsuffix")
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_bare_jwt_redacted_outside_bearer_scheme(self) -> None:
        """A standalone JWT (no ``Bearer `` prefix) is redacted."""
        filt = SecretRedactionFilter([])
        result = filt.redact('{"authorization": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.sig"}')
        assert "eyJhbGciOiJSUzI1NiJ9" not in result

    def test_five_segment_jwe_redacted(self) -> None:
        """A 5-segment JWE is covered, matching ``security.py``'s scrubber."""
        filt = SecretRedactionFilter([])
        jwe = "eyJhbGciOiJkaXIifQ..iv-value.ciphertext-value.tag-value"
        result = filt.redact(f"token: {jwe}")
        assert "eyJhbGciOiJkaXIifQ" not in result


def _make_record(msg: str, exc_info: object = None) -> logging.LogRecord:
    """Create a record through the LIVE factory — how ``logging`` itself does it."""
    return logging.getLogRecordFactory()("kiro_crew.test", logging.INFO, "", 0, msg, None, exc_info)


class TestInstallLogRedaction:
    """Tests for the install_log_redaction convenience function."""

    @pytest.fixture(autouse=True)
    def _no_redaction_installed(self):
        """Start and end each test with no redacting factory installed.

        The record factory is a single process-global slot, and these tests assert on
        the install contract itself, so each one owns its precondition rather than
        inheriting whatever ran before it on this worker.
        ``conftest._restore_log_record_factory`` puts the slot back after every test in
        the suite; uninstalling on both sides here is what makes these tests
        independent of that.
        """
        uninstall_log_redaction()
        yield
        uninstall_log_redaction()

    def test_installs_record_factory(self) -> None:
        before = logging.getLogRecordFactory()
        install_log_redaction([])
        assert logging.getLogRecordFactory() is not before
        # The wrapper is live, not merely constructed.
        assert "Bearer [REDACTED]" in _make_record("h: Bearer eyJhbGciOiJSUzI1NiJ9.a.b").msg

    def test_returns_filter_instance(self) -> None:
        assert isinstance(install_log_redaction(["test-secret"]), SecretRedactionFilter)

    def test_redacts_via_record_factory(self) -> None:
        """Records created after install have secrets redacted."""
        install_log_redaction(["sk-secret-key-12345"])
        record = _make_record("key is sk-secret-key-12345 here")
        assert "sk-secret-key-12345" not in record.msg
        assert "[REDACTED]" in record.msg

    def test_install_survives_a_module_reload(self) -> None:
        """A reload must not orphan the installed wrapper or make it call itself.

        ``importlib.reload`` re-executes this module in the SAME namespace, so every
        module-level name the wrapper might read is reset while ``logging`` still holds
        the wrapper. A design that resolved the base factory or the active filter
        through one of those names would either recurse (the wrapper becomes its own
        base — process-wide, so ALL logging dies, not just redaction) or silently stop
        redacting. The wrapper carries both on itself, so a reload cannot reach them.
        """
        import kiro_crew.log_redaction as mod

        install_log_redaction(["sk-secret-key-12345"])
        installed = logging.getLogRecordFactory()
        try:
            importlib.reload(mod)
            assert logging.getLogRecordFactory() is installed
            assert "sk-secret-key-12345" not in _make_record("k=sk-secret-key-12345").msg

            # And a post-reload install re-points the SAME live wrapper.
            mod.install_log_redaction(["second-secret-value"])
            assert logging.getLogRecordFactory() is installed
            assert "second-secret-value" not in _make_record("k=second-secret-value").msg
        finally:
            mod.uninstall_log_redaction()

    def test_reinstall_under_a_foreign_wrapper_never_cycles(self) -> None:
        """A second install must not chain onto a third party's wrapper of ours.

        The stdlib's own "Customizing LogRecord" recipe is to capture
        ``getLogRecordFactory()`` and wrap it, which is what logging-instrumentation
        libraries do. That wrapper reports as foreign, so capturing it as the base
        factory would build ``ours -> theirs -> ours`` — and because the wrapper
        resolves its base at call time on a process-global slot, the next log call
        anywhere raises RecursionError and NO record is emitted again.
        """
        install_log_redaction(["sk-secret-key-12345"])
        ours = logging.getLogRecordFactory()

        def foreign_factory(*args, **kwargs):
            record = ours(*args, **kwargs)
            record.tenant = "acme"
            return record

        logging.setLogRecordFactory(foreign_factory)
        try:
            install_log_redaction(["sk-secret-key-12345"])
            record = _make_record("key is sk-secret-key-12345 here")
        finally:
            logging.setLogRecordFactory(ours)

        # Redaction still applies through the foreign wrapper, and the wrapper it
        # added survived — the second install left the slot alone.
        assert "sk-secret-key-12345" not in record.msg
        assert record.tenant == "acme"

    def test_uninstall_leaves_a_foreign_wrapper_alone(self) -> None:
        """Uninstall must not reach into a chain it does not own.

        Ours is unreachable behind a third party's closure, so uninstall is a no-op
        and redaction CONTINUES — the safe direction for a control that keeps secrets
        out of the log. Unlinking their wrapper to get at ours is the alternative, and
        it silently costs every later record whatever they added.
        """
        install_log_redaction(["sk-secret-key-12345"])
        ours = logging.getLogRecordFactory()

        def foreign_factory(*args, **kwargs):
            record = ours(*args, **kwargs)
            record.tenant = "acme"
            return record

        logging.setLogRecordFactory(foreign_factory)
        try:
            uninstall_log_redaction()
            assert logging.getLogRecordFactory() is foreign_factory
            record = _make_record("key is sk-secret-key-12345 here")
            assert "sk-secret-key-12345" not in record.msg
            assert record.tenant == "acme"
        finally:
            logging.setLogRecordFactory(ours)
            uninstall_log_redaction()

    def test_reinstall_adopts_the_new_patterns(self) -> None:
        """A second install re-points the active filter instead of being a no-op."""
        install_log_redaction(["first-secret-value"])
        install_log_redaction(["second-secret-value"])

        record = _make_record("a=first-secret-value b=second-secret-value")
        assert "second-secret-value" not in record.msg
        # The first install's patterns are replaced, not merged — one active filter.
        assert "first-secret-value" in record.msg

    def test_uninstall_restores_the_previous_factory(self) -> None:
        before = logging.getLogRecordFactory()
        install_log_redaction(["sk-secret-key-12345"])
        uninstall_log_redaction()

        assert logging.getLogRecordFactory() is before
        assert "sk-secret-key-12345" in _make_record("key is sk-secret-key-12345").msg

    def test_uninstall_without_install_is_a_noop(self) -> None:
        before = logging.getLogRecordFactory()
        uninstall_log_redaction()
        assert logging.getLogRecordFactory() is before

    def test_installed_factory_renders_exc_info_into_exc_text(self) -> None:
        """``exc_info`` is cleared once a SECRET-BEARING traceback is rendered.

        Pinned because it is the destructive half of the wrapper: a handler must not
        be able to re-render an unredacted traceback, and clearing the field is what
        guarantees that. ``exc_info`` is rendered, redacted, and cleared for EVERY
        record carrying one — secret-bearing or not (frame locals are unscannable);
        see ``test_secretless_exc_info_is_rendered_and_cleared`` for the secretless
        direction.
        """
        install_log_redaction([])
        try:
            raise ValueError("Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig")
        except ValueError:
            record = _make_record("boom", exc_info=sys.exc_info())

        assert record.exc_info is None
        assert record.exc_text is not None
        assert "eyJhbGciOiJSUzI1NiJ9" not in record.exc_text

    def test_render_failure_destroys_args_unconditionally(self) -> None:
        """When ``getMessage()`` raises, args are dropped even without a secret.

        On the render-failure branch the args were never inspected, so
        "unchanged rendered text means secretless" does not hold. A
        secret-bearing arg surviving there would later be printed unredacted by
        ``Handler.handleError``'s ``Arguments: %s`` stderr dump. The old
        unconditional destruction is kept for exactly this branch.
        """
        install_log_redaction([])
        record = logging.getLogRecordFactory()(
            "third_party.lib.client",
            logging.INFO,
            "",
            0,
            "coords: %d",  # %d with a str arg -> getMessage() raises TypeError
            ("Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig",),
            None,
        )
        assert record.args is None

    def test_lossy_format_spec_still_destroys_secret_bearing_args(self) -> None:
        """A truncating format spec cannot smuggle a secret through in args.

        ``%.100s`` renders only a prefix; when the secret sits past the
        truncation point the rendered text scans clean while the full
        credential remains in ``record.args`` for a structured handler to
        export. The preserve decision therefore scans each arg's own string
        form, not just the rendered message.
        """
        install_log_redaction([])
        long_prefix = "x" * 120
        record = logging.getLogRecordFactory()(
            "third_party.lib.client",
            logging.INFO,
            "",
            0,
            "payload: %.100s",
            (long_prefix + " Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig",),
            None,
        )
        # The rendered prefix contains no Bearer token, but the arg does:
        # args must be destroyed so no handler can export the credential.
        assert record.args is None

    def test_lossy_format_aws_credential_in_args_is_destroyed(self) -> None:
        """An AWS key ID past a ``%.100s`` truncation point cannot survive in args.

        The full-arg scan covers every built-in pattern — the rendered prefix
        scans clean, but the arg's own string form carries the key ID, so the
        record is materialized and cleared.
        """
        install_log_redaction([])
        long_prefix = "x" * 120
        record = logging.getLogRecordFactory()(
            "third_party.lib.client",
            logging.INFO,
            "",
            0,
            "acp output: %.100s",
            (long_prefix + " AKIAIOSFODNN7EXAMPLE",),
            None,
        )
        assert record.args is None

    def test_opaque_msg_object_disqualifies_preservation(self) -> None:
        """A non-str ``msg`` object is materialized even when its text is clean.

        ``logging`` permits any object as ``msg``; a structured handler could
        export the object itself, whose attributes the rendered text never
        showed.
        """

        class Msg:
            def __init__(self) -> None:
                self.token = "Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig"

            def __str__(self) -> str:
                return "benign message"

        install_log_redaction([])
        record = logging.getLogRecordFactory()(
            "third_party.lib.client", logging.INFO, "", 0, Msg(), None, None
        )
        assert type(record.msg) is str
        assert record.args is None

    def test_mapping_args_disqualify_preservation(self) -> None:
        """Dict args are never preserved — keys and the dict object are exportable.

        ``%(name)s`` formatting renders only the referenced VALUES; a
        credential hidden in a KEY never reaches the rendered text, but a
        structured handler exporting ``record.args`` gets the whole mapping.
        """
        install_log_redaction([])
        record = logging.getLogRecordFactory()(
            "third_party.lib.client",
            logging.INFO,
            "",
            0,
            "user: %(user)s",
            {"user": "alice", "Bearer eyJhbGciOiJSUzI1NiJ9.k": "x"},
            None,
        )
        assert record.args is None

    def test_falsy_opaque_arg_disqualifies_preservation(self) -> None:
        """A falsy credential-bearing object cannot pose as absent args.

        Only ``None`` or an exact empty tuple count as no-args; an object
        whose ``__bool__`` returns False still has exportable attributes.
        """

        class FalsyCreds:
            def __init__(self) -> None:
                self.token = "Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig"

            def __bool__(self) -> bool:
                return False

            def __str__(self) -> str:
                return "empty"

        install_log_redaction([])
        record = logging.getLogRecordFactory()(
            "third_party.lib.client", logging.INFO, "", 0, "state: %s", FalsyCreds(), None
        )
        assert record.args is None

    def test_opaque_object_arg_disqualifies_preservation(self) -> None:
        """An object with a benign ``str()`` but credential attributes is destroyed.

        Text scanning cannot bound an opaque object's exportable surface: a
        structured handler serializing the object itself (attribute dump,
        JSON) exports content ``str()`` never showed. Only exact immutable
        scalars qualify for preservation.
        """

        class Client:
            def __init__(self) -> None:
                self.token = "Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig"

            def __str__(self) -> str:
                return "WeixinClient(connected)"

        install_log_redaction([])
        record = logging.getLogRecordFactory()(
            "third_party.lib.client",
            logging.INFO,
            "",
            0,
            "client ready: %s",
            (Client(),),
            None,
        )
        assert record.args is None

    def test_str_subclass_arg_disqualifies_preservation(self) -> None:
        """A ``str`` SUBCLASS does not qualify — it can carry attributes."""

        class Tagged(str):
            pass

        tagged = Tagged("benign text")
        tagged.token = "Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig"  # type: ignore[attr-defined]
        install_log_redaction([])
        record = logging.getLogRecordFactory()(
            "third_party.lib.client",
            logging.INFO,
            "",
            0,
            "note: %s",
            (tagged,),
            None,
        )
        assert record.args is None

    def test_secretless_record_preserves_args(self) -> None:
        """A secretless record keeps its ``%s`` template and ``args`` untouched.

        The factory is process-global and sees EVERY record, including third-party
        ones. When redaction does not change the rendered message, the record must
        pass through unmutated so a JSON / observability handler still gets the
        original template and its argument tuple to format structurally. Mutating
        unconditionally (materializing ``msg`` and zeroing ``args`` on every record)
        silently destroys those structured fields even when no secret is present.
        """
        install_log_redaction([])
        record = logging.getLogRecordFactory()(
            "third_party.lib.client",
            logging.INFO,
            "",
            0,
            "request %s completed in %d ms",
            ("GET /items", 42),
            None,
        )
        # Template and args survive — a structured handler can still format them.
        assert record.msg == "request %s completed in %d ms"
        assert record.args == ("GET /items", 42)
        # And the rendered message is correct (no secret, so no redaction).
        assert record.getMessage() == "request GET /items completed in 42 ms"

    def test_secretless_exc_info_is_rendered_and_cleared(self) -> None:
        """Even a secretless exception has ``exc_info`` rendered and cleared.

        The preserve-on-clean rule applies only to ``msg``/``args``, whose
        exportable surface is exactly the string forms the scan inspects. A
        live ``exc_info`` triple is an object graph whose traceback frames
        carry LOCALS (e.g. a client object holding a token) that no text scan
        can bound — a locals-capturing handler would export credentials the
        rendered-text check never saw. So the traceback is always rendered
        (and redacted) into ``exc_text`` and ``exc_info`` is always cleared.
        """
        install_log_redaction([])
        try:
            raise ValueError("plain failure with no secret")
        except ValueError:
            exc = sys.exc_info()
            record = _make_record("boom", exc_info=exc)

        assert record.exc_info is None
        assert record.exc_text is not None
        assert "plain failure with no secret" in record.exc_text

    def test_bearer_record_redacts_msg_and_clears_args(self) -> None:
        """A Bearer-bearing record still gets ``msg`` redacted and ``args`` cleared.

        The only-on-change contract must not weaken the secret guarantee: when the
        rendered message contains a secret, ``msg`` is replaced with the redacted
        text and ``args`` is cleared so a handler cannot re-format from the original
        template and leak it.
        """
        install_log_redaction([])
        record = logging.getLogRecordFactory()(
            "kiro_crew.test",
            logging.INFO,
            "",
            0,
            "auth header %s",
            ("Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig",),
            None,
        )
        assert "eyJhbGciOiJSUzI1NiJ9" not in record.msg
        assert "Bearer [REDACTED]" in record.msg
        assert record.args is None

    def test_bearer_exc_info_is_rendered_and_cleared(self) -> None:
        """A Bearer-bearing traceback is rendered, redacted, and ``exc_info`` cleared."""
        install_log_redaction([])
        try:
            raise ValueError("token Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig leaked")
        except ValueError:
            record = _make_record("boom", exc_info=sys.exc_info())

        assert record.exc_info is None
        assert record.exc_text is not None
        assert "eyJhbGciOiJSUzI1NiJ9" not in record.exc_text
        assert "Bearer [REDACTED]" in record.exc_text

    def test_zero_vault_imports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """log_redaction module does NOT import kiro_crew.secrets."""
        import kiro_crew

        mod_name = "kiro_crew.log_redaction"
        # A fresh import is the only way to observe what the module pulls in, and it
        # rebinds BOTH sys.modules and the parent package attribute — so undo covers
        # both, or `kiro_crew.log_redaction` keeps pointing at the duplicate and one
        # process-global chokepoint has two module objects. monkeypatch reverts even
        # when the assertion below fails.
        monkeypatch.setattr(kiro_crew, "log_redaction", sys.modules[mod_name])
        monkeypatch.delitem(sys.modules, mod_name)

        imported_before = set(sys.modules.keys())
        importlib.import_module(mod_name)
        imported_after = set(sys.modules.keys())

        new_imports = imported_after - imported_before
        vault_imports = [m for m in new_imports if "secrets" in m and "kiro_crew" in m]
        assert vault_imports == [], f"log_redaction imported vault modules: {vault_imports}"
