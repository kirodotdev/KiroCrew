"""Unit tests for the cloud config store (cloud/config.py) — no secrets stored."""

from __future__ import annotations

import json

import pytest

from kiro_crew.cloud import config as cloud_config
from kiro_crew.cloud.config import (
    _MAX_FILE_BYTES,
    DEFAULT_REGION,
    CloudConfig,
    FargateConfig,
)

#: The model-credential secret as a conforming ``(name, ARN)`` pair: the name is
#: ``kirocrew/crew/<crew>/<ENV>`` and the ARN is that name plus one six-character
#: service suffix. The account id is fictional.
CREDENTIAL_SECRET = [
    "kirocrew/crew/demo/KIRO_API_KEY",
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:kirocrew/crew/demo/KIRO_API_KEY-abcdef",
]

#: A complete Fargate block. Every case below is this, minus or plus one thing, so
#: a case cannot pass by being malformed in a second way the assertion never named.
COMPLETE_FARGATE = {
    "cluster": "kirocrew-crew-prod",
    "subnets": ["subnet-a", "subnet-b"],
    "security_groups": ["sg-1"],
    "image": "public.ecr.aws/example/kirocrew-crew-base@sha256:" + "a" * 64,
    "secrets": [CREDENTIAL_SECRET],
    "cpu_architecture": "X86_64",
}


class TestFargateConfig:
    """A block is COMPLETE or ABSENT. There is no third state, by design.

    A half-written block that produced a usable object would leave the lane
    registered and refusing every launch made through it -- spending an operator's
    attention at launch time on a mistake that was visible when they saved the
    file.
    """

    def test_the_credential_recipient_names_the_image_and_the_credential_arn(self):
        """What the operator confirms at launch: the container that receives the model
        credential, and the ARN of the secret that delivers it there.

        Both, because both decide who receives it. The image reference is digest-pinned, so
        it names the registry, the repository and the content -- but the digest rule
        constrains the FORM of the reference and never who owns the registry, which is why a
        person has to read it rather than a checker merely validate it.
        """
        config = FargateConfig.from_mapping(COMPLETE_FARGATE)

        recipient = config.credential_recipient()

        assert COMPLETE_FARGATE["image"] in recipient
        assert CREDENTIAL_SECRET[1] in recipient
        # The NAME is not enough on its own: two secrets can be named alike across accounts,
        # and the ARN is what the execution role actually fetches.
        assert recipient.count("arn:aws:secretsmanager:") == 1, recipient

    def test_changing_the_image_changes_what_must_be_confirmed(self):
        """The property the whole design rests on: a rewritten block cannot produce the value
        the operator already confirmed, so the launch refuses instead of substituting."""
        theirs = FargateConfig.from_mapping(COMPLETE_FARGATE).credential_recipient()
        substituted = {
            **COMPLETE_FARGATE,
            "image": "public.ecr.aws/attacker/x@sha256:" + "b" * 64,
        }

        assert FargateConfig.from_mapping(substituted).credential_recipient() != theirs

    def test_an_incomplete_block_has_no_recipient_to_confirm(self):
        """Same answer the lane itself gives: there is no launch, so there is nothing to
        confirm. Returning a partial string would be a value an operator could confirm for a
        lane that does not exist."""
        assert FargateConfig().credential_recipient() == ""

    def test_the_recipient_is_rendered_once_not_twice(self):
        """ONE renderer, shared with the engine.

        The confirmation is a comparison between what an operator was shown and what a launch
        resolves. Two spellings of the same pair make that a comparison of the renderings, and
        this repository has already paid three times for a rule kept in two places.
        """
        import ast
        import inspect
        import textwrap

        from kiro_crew.cloud.fargate.taskdef import credential_recipient as shared

        tree = ast.parse(textwrap.dedent(inspect.getsource(FargateConfig.credential_recipient)))
        returns = [n for n in ast.walk(tree) if isinstance(n, ast.Return) and n.value is not None]
        rendered = [r for r in returns if isinstance(r.value, ast.Call)]
        assert rendered, "the recipient is built here instead of being delegated"
        for r in rendered:
            assert ast.unparse(r.value.func).endswith("credential_recipient"), ast.unparse(r)
        assert callable(shared)

    def test_a_complete_block_is_read(self):
        config = FargateConfig.from_mapping(COMPLETE_FARGATE)
        assert config is not None
        assert config.cluster == "kirocrew-crew-prod"
        assert config.subnets == ("subnet-a", "subnet-b")
        assert config.security_groups == ("sg-1",)
        assert config.secrets == (
            (COMPLETE_FARGATE["secrets"][0][0], COMPLETE_FARGATE["secrets"][0][1]),
        )
        assert config.is_complete()

    @pytest.mark.parametrize(
        ("label", "block"),
        [
            ("movable tag image", {**COMPLETE_FARGATE, "image": "public.ecr.aws/x/base:latest"}),
            ("no image", {**COMPLETE_FARGATE, "image": ""}),
            ("no cluster", {**COMPLETE_FARGATE, "cluster": ""}),
            ("no subnet", {**COMPLETE_FARGATE, "subnets": []}),
            ("no security group", {**COMPLETE_FARGATE, "security_groups": []}),
            ("unknown architecture", {**COMPLETE_FARGATE, "cpu_architecture": "RISCV"}),
            ("subnets not a list", {**COMPLETE_FARGATE, "subnets": "subnet-a"}),
            ("secrets not a list", {**COMPLETE_FARGATE, "secrets": "nope"}),
            ("secret entry is not a pair", {**COMPLETE_FARGATE, "secrets": [["only-one"]]}),
            ("secret arn is empty", {**COMPLETE_FARGATE, "secrets": [["KIRO_API_KEY", ""]]}),
            ("no secrets at all", {**COMPLETE_FARGATE, "secrets": []}),
            (
                "secrets but none named for the model credential",
                {
                    **COMPLETE_FARGATE,
                    "secrets": [["kirocrew/crew/demo/OTHER_KEY", CREDENTIAL_SECRET[1]]],
                },
            ),
            ("public ip is the string false", {**COMPLETE_FARGATE, "assign_public_ip": "false"}),
            ("public ip is the string zero", {**COMPLETE_FARGATE, "assign_public_ip": "0"}),
            ("public ip is the string true", {**COMPLETE_FARGATE, "assign_public_ip": "true"}),
            ("public ip is a number", {**COMPLETE_FARGATE, "assign_public_ip": 1}),
            ("public ip is null", {**COMPLETE_FARGATE, "assign_public_ip": None}),
            (
                "secrets list past the item bound",
                {
                    **COMPLETE_FARGATE,
                    "secrets": [CREDENTIAL_SECRET] * (cloud_config._MAX_LIST_ITEMS + 1),
                },
            ),
            (
                "subnets list past the item bound",
                {**COMPLETE_FARGATE, "subnets": ["subnet-a"] * (cloud_config._MAX_LIST_ITEMS + 1)},
            ),
            (
                "image string past the size bound",
                {**COMPLETE_FARGATE, "image": "x" * (cloud_config._MAX_STRING_LEN + 1)},
            ),
            (
                "a subnet string past the size bound",
                {**COMPLETE_FARGATE, "subnets": ["s" * (cloud_config._MAX_STRING_LEN + 1)]},
            ),
            (
                "a secret name past the size bound",
                {
                    **COMPLETE_FARGATE,
                    "secrets": [
                        [
                            "x" * (cloud_config._MAX_STRING_LEN + 1) + "/KIRO_API_KEY",
                            CREDENTIAL_SECRET[1],
                        ]
                    ],
                },
            ),
            ("not an object", "nope"),
            ("absent", None),
        ],
    )
    def test_an_unusable_block_reads_as_absent(self, label: str, block: object):
        assert FargateConfig.from_mapping(block) is None, label

    def test_a_secretless_block_is_incomplete_because_the_engine_would_refuse_it(self):
        """The engine refuses a task definition delivering no model credential.

        So a block with every placement field and an empty ``secrets`` list is the
        offered-and-refusing state exactly: the lane would register and reject every
        launch. Judged here, it is absent instead.
        """
        assert FargateConfig.from_mapping({**COMPLETE_FARGATE, "secrets": []}) is None

    def test_a_reference_the_engine_would_refuse_does_not_register(self):
        """The gate asks the ENGINE, so its answer and the engine's cannot differ.

        This replaces an earlier assertion that a conforming NAME registered whatever
        its ARN said. That was deliberate at the time -- the ARN pairing was left to the
        engine to avoid a second copy of its rule -- but it meant a mismatched pair
        registered the lane and was then refused at launch, which is the state this
        module exists to prevent. Delegating to ``identity.secret_env_name`` moved the
        boundary rather than duplicating it: there is still exactly one copy of the
        rule, and it now runs here too.
        """
        block = {**COMPLETE_FARGATE, "secrets": [[CREDENTIAL_SECRET[0], "arn:not-a-real-arn"]]}
        assert FargateConfig.from_mapping(block) is None

    def test_a_conforming_reference_still_registers(self):
        """The positive side, so the test above cannot pass by refusing everything."""
        assert FargateConfig.from_mapping(COMPLETE_FARGATE) is not None

    @pytest.mark.parametrize("bad", [False, True, 0, 1, 1.5, None, [], {}, [1], {"a": 1}])
    def test_no_string_field_coerces_a_non_string(self, bad: object):
        """Every string field, derived from the dataclass, not a list someone maintains.

        `str()` made any JSON scalar truthy: `false` became `"False"`, which is
        non-empty, so `is_complete()` passed and the lane registered against a cluster
        that does not exist. Parametrized over the FIELDS as well as the values, so a
        string field added later is covered without anyone remembering.
        """
        from kiro_crew.cloud.config import _STRING_FIELD_DEFAULTS

        assert _STRING_FIELD_DEFAULTS, "the field derivation must not be empty"
        for field_name in _STRING_FIELD_DEFAULTS:
            block = {**COMPLETE_FARGATE, field_name: bad}
            assert FargateConfig.from_mapping(block) is None, f"{field_name}={bad!r}"

    def test_the_string_field_list_matches_the_dataclass(self):
        """Pins the derivation itself, so a field cannot silently drop out of it."""
        from dataclasses import fields

        from kiro_crew.cloud.config import _STRING_FIELD_DEFAULTS

        expected = {f.name for f in fields(FargateConfig) if isinstance(f.default, str)}
        assert set(_STRING_FIELD_DEFAULTS) == expected


class TestAConcurrentEditIsNeverLost:
    """The property, stated so a narrower race cannot pass it.

    A caller reads the config, works, and writes the whole record back. Anything that
    landed in between is in the file but not in the snapshot, so writing the snapshot
    erases it with nothing said. The property is not "the window is small": it is that
    the write either INCLUDES the other edit or REFUSES. Both outcomes keep the edit;
    only a silent overwrite loses it.

    Note on scope: the read-only seal on `cloud.json` does NOT cover this. The seal stops
    a sandboxed agent from writing the file at all, and the writers racing here are both
    trusted paths. Claiming the seal answers this would be the same mistake as the macOS
    branch -- naming a protection that is not doing the work.
    """

    FARGATE = dict(COMPLETE_FARGATE)

    @staticmethod
    def _seed(p) -> None:
        import json as _json

        p.write_text(_json.dumps({"profile": "old", "region": "us-east-1"}), encoding="utf-8")

    def test_the_tag_pattern_accepts_its_own_bound(self):
        """One bound, two users. A second literal would be free to drift."""
        from kiro_crew.cloud.config import _TAG_MAX_LEN, _TAG_RE

        assert _TAG_RE.match("a" * _TAG_MAX_LEN), "the pattern must accept its own bound"
        assert not _TAG_RE.match("a" * (_TAG_MAX_LEN + 1)), "and reject one past it"

    def test_a_file_past_the_ceiling_is_refused_without_being_read_whole(
        self, tmp_path, monkeypatch
    ):
        """The ceiling must bound the READ, not only the verdict.

        A read that ignores the ceiling both defeats the memory bound and pulls in a file
        too large to write back, so the size of what comes in is the property under test.
        """
        from kiro_crew.cloud.config import _MAX_FILE_BYTES, CloudConfig

        p = tmp_path / "cloud.json"
        p.write_text(
            '{"profile": "p", "pad": "' + "x" * (_MAX_FILE_BYTES + 5000) + '"}',
            encoding="utf-8",
        )
        on_disk = p.stat().st_size
        assert on_disk > _MAX_FILE_BYTES

        # The verdict alone cannot witness the bound: with an UNBOUNDED read the length
        # check inside load() still refuses, so an assertion on the defaults alone holds
        # whether the read is bounded or not, and the test's name would claim a property
        # its body never observes. The assertion is therefore on how many bytes come IN,
        # which is the thing the ceiling exists to bound.
        import builtins

        pulled: list[int] = []
        real_open = builtins.open

        class _CountingReads:
            """Delegates everything, recording only how much each read returned."""

            def __init__(self, fh):
                self._fh = fh

            def read(self, *a, **k):
                data = self._fh.read(*a, **k)
                pulled.append(len(data))
                return data

            def __enter__(self):
                self._fh.__enter__()
                return self

            def __exit__(self, *exc):
                return self._fh.__exit__(*exc)

            def __getattr__(self, name):
                return getattr(self._fh, name)

        def counting_open(file, *a, **k):
            fh = real_open(file, *a, **k)
            return _CountingReads(fh) if str(file) == str(p) else fh

        monkeypatch.setattr(builtins, "open", counting_open)
        loaded = CloudConfig.load(p)
        monkeypatch.undo()

        assert pulled, "the loader never read the file, so nothing here tested the bound"
        assert max(pulled) <= _MAX_FILE_BYTES + 1, (
            f"load() pulled {max(pulled)} bytes in for a {on_disk}-byte file: the ceiling "
            "must bound the READ, or an oversized file exhausts memory before the refusal"
        )
        assert loaded.profile == "", "an over-size file must read as defaults"

    def test_two_configs_with_the_same_settings_are_equal(self, tmp_path):
        """The fingerprint must stay out of equality, or callers comparing configs break."""
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        self._seed(p)
        other = tmp_path / "elsewhere.json"
        other.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        assert CloudConfig.load(p) == CloudConfig.load(other), "read-source must not affect =="

    def test_the_config_module_has_no_preservation_mechanism_left(self):
        """The sidecar is GONE, not guarded.

        Its two review findings were both defects OF it -- a copy the write gate did not
        cover, and a directory leaf no alias check validated -- so the code they are about is
        what had to go. A mechanism left in place behind a flag would keep both.
        """
        import kiro_crew.cloud.config as config_mod

        leftovers = [n for n in vars(config_mod) if "preserv" in n.lower()]
        assert leftovers == [], leftovers

    def test_the_precondition_caller_cannot_resolve_an_empty_tag(self, monkeypatch):
        """Merging from displaced bytes stays out of reach of a caller with a precondition.

        `_on_disk_tag` answers the default for a file that cannot be parsed, so a caller
        naming the default tag would be the one way a precondition could be satisfied by a
        tag nobody read. The only production caller of `expect_last_tag` resolves its tag
        through a path that exits rather than yielding an empty one, which is what closes
        that case, so it is pinned here rather than left to reading.
        """
        import argparse as _argparse

        import pytest as _pytest

        from kiro_crew import cli_cloud as _cli_cloud
        from kiro_crew.cloud.config import CloudConfig

        monkeypatch.setattr(CloudConfig, "load", classmethod(lambda cls, *a, **k: cls()))
        assert CloudConfig().last_tag == "", "the premise of this test is the default tag"

        with _pytest.raises(SystemExit):
            _cli_cloud._resolve_tag(_argparse.Namespace(tag=""))

        assert (
            _cli_cloud._resolve_tag(_argparse.Namespace(tag="kc-given")) == "kc-given"
        ), "an explicitly named tag must still be honoured"

    @pytest.mark.parametrize(
        "kind",
        ["too_deep", "huge_integer", "not_utf8", "not_json", "wrong_shape"],
    )
    def test_a_file_that_cannot_become_a_document_reads_as_defaults(self, tmp_path, kind):
        """One answer for every way a hand-edited file fails to parse.

        These reach `json.loads` through different failure types: a decode error, a
        `JSONDecodeError`, the interpreter's integer-string limit (a plain `ValueError` on
        syntax that is perfectly valid JSON), and a `RecursionError` on depth the parser
        accepts less of than it can build. No caller can act differently on any of them, so
        naming them one at a time is what lets one be missed; this list is the answer.

        Parametrized on a short KEY, not on the payload: pytest exports the node id as an
        environment variable, and a 60,000-bracket document in the id blows past the Windows
        32767-character cap, which the repo has a gate for.
        """
        from kiro_crew.cloud.config import CloudConfig

        p = tmp_path / "cloud.json"
        if kind == "not_utf8":
            p.write_bytes(b'{"profile": "\xff\xfe"}')
        else:
            raw = {
                "too_deep": '{"fargate": ' + "[" * 60000 + "]" * 60000 + "}",
                "huge_integer": '{"fargate": ' + "9" * 5000 + "}",
                "not_json": "{not json",
                "wrong_shape": "[1, 2, 3]",
            }[kind]
            p.write_text(raw, encoding="utf-8")

        loaded = CloudConfig.load(p)  # must not raise
        assert loaded.profile == "", f"{kind} did not read as defaults"
        assert loaded.fargate is None, f"{kind} retained a block"

    @pytest.mark.parametrize("contents", ["absent", "empty", "complete"])
    def test_a_delegated_workspace_over_the_config_is_refused_whatever_it_holds(
        self, tmp_path, contents
    ):
        """The seal cannot reach a delegated spawn, so the SPAWN is refused instead.

        Parametrized over contents on purpose. Conditioning this on whether a Fargate block
        exists was tried and is wrong: an agent does not need to swap a field it can create,
        so with no block it writes a complete one and the owner's next launch runs its image.
        All three cases must refuse identically, which is what makes the rule content-blind.
        """
        from kiro_crew import sandbox
        from kiro_crew.cloud import config as cloud_config

        home = tmp_path / "crew"
        home.mkdir()
        if contents == "empty":
            (home / "cloud.json").write_text("{}", encoding="utf-8")
        elif contents == "complete":
            (home / "cloud.json").write_text(
                json.dumps({"profile": "p", "region": "us-east-1", "fargate": COMPLETE_FARGATE}),
                encoding="utf-8",
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sandbox, "config_dir", lambda: home)
            mp.setattr(cloud_config, "config_dir", lambda: home)
            mp.setattr(sandbox.sys, "platform", "win32")  # a delegated platform
            reason = sandbox.delegated_workspace_exposes_sealed_target(str(tmp_path))

        assert reason is not None, f"a delegated workspace containing the config ({contents})"
        assert "sealed cloud configuration" in reason, reason
        assert "model credential" in reason, "the reason must name the actual consequence"

    def test_every_strict_leaf_of_either_shape_is_covered_by_the_delegated_guard(self):
        """The omission was shape-shaped, so the pin is on the DERIVATION, not on a member.

        The guard built its targets from the FILE list alone, which covers one shape and leaves
        the other uncovered however many entries are added. Deriving from one mapping keyed by
        both lists is what makes the next sealed leaf covered by construction; this asserts the
        mapping and the two lists cannot drift, in either direction.
        """
        import inspect

        from kiro_crew import sandbox

        both = set(sandbox._CREW_NOFOLLOW_READONLY_FILE_LEAVES) | set(
            sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES
        )
        assert set(sandbox._DELEGATED_OVERLAP_LEAF_REASONS) == both

        # And the guard reads that mapping, rather than one of the two lists it can outlive.
        src = inspect.getsource(sandbox.delegated_workspace_exposes_sealed_target)
        assert "for leaf in _DELEGATED_OVERLAP_LEAF_REASONS" in src, src[:0] or (
            "the target list must be derived from the mapping, or one leaf shape goes uncovered"
        )

    def test_a_delegated_workspace_elsewhere_is_left_alone(self, tmp_path):
        """Only an overlap is refused, so an ordinary project workspace still spawns."""
        from kiro_crew import sandbox

        home = tmp_path / "crew"
        home.mkdir()
        (home / "cloud.json").write_text("{}", encoding="utf-8")
        elsewhere = tmp_path / "some-project"
        elsewhere.mkdir()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sandbox, "config_dir", lambda: home)
            mp.setattr(sandbox, "_resolved_kiro_agents_targets", lambda: [])
            mp.setattr(sandbox.sys, "platform", "win32")
            assert sandbox.delegated_workspace_exposes_sealed_target(str(elsewhere)) is None

    # (kind, bytes) -- every way a PRESENT file can fail to be a record.
    UNUSABLE = [
        ("truncated", b'{"profile": "prod", "region": "eu-west-1"'),
        ("not an object", b"[1, 2, 3]"),
        ("not utf-8", b'\xcb\xff{"profile": "prod"}'),
        ("oversized", b'{"pad": "' + b"x" * (_MAX_FILE_BYTES + 64) + b'"}'),
    ]

    @pytest.mark.parametrize("kind,blob", UNUSABLE, ids=[k for k, _ in UNUSABLE])
    def test_load_still_tolerates_every_one_of_them(self, tmp_path, kind, blob):
        """Two policies over ONE parser: the reader tolerates what the writer refuses."""
        p = tmp_path / "cloud.json"
        p.write_bytes(blob)
        assert CloudConfig.load(p) == CloudConfig(), kind

    def test_the_set_of_cloud_config_writers_is_closed(self):
        """Show the SET, so a new writer cannot be added without this failing.

        Patching the caller a reviewer happened to name leaves the next one for the next
        reviewer. What closes the class is enumerating every write of this document and
        requiring each to be the field-scoped one.
        """
        import ast as _ast
        import pathlib as _pathlib

        src_root = _pathlib.Path(__file__).resolve().parents[1] / "src/kiro_crew"
        # `load` is a read and is always fine. Anything ELSE that is not `apply_update`
        # fails, including a write method added later under a name nobody here foresaw --
        # that is what makes this a closed set rather than a list of the two callers a
        # reviewer happened to name.
        allowed = {"apply_update", "load"}
        offenders: list[str] = []
        for f in sorted(src_root.rglob("*.py")):
            if f.name == "config.py" and f.parent.name == "cloud":
                continue  # the implementation itself
            try:
                tree = _ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for n in _ast.walk(tree):
                if not (isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)):
                    continue
                recv = n.func.value
                is_cloud_cfg = (isinstance(recv, _ast.Name) and recv.id == "CloudConfig") or (
                    isinstance(recv, _ast.Attribute) and recv.attr == "CloudConfig"
                )
                if is_cloud_cfg and n.func.attr not in allowed:
                    offenders.append(f"{f.relative_to(src_root)}:{n.lineno} .{n.func.attr}")
        assert not offenders, (
            "these write CloudConfig by a route other than apply_update: "
            f"{offenders}. Every writer runs after its remote work, so each must name the "
            "fields it owns and must not be refusable."
        )

    def test_no_cloud_caller_reads_mutates_and_saves(self):
        """No cloud caller holds a snapshot, mutates it and writes it back.

        A caller that does erases whatever landed meanwhile, and it runs AFTER the remote
        change -- the resume write and the destroy's tag clear are both past the point of no
        return -- so a failure there cannot be retried away.

        `CloudConfig` has no writer at all, so a `cfg.save()` would not even resolve; this is
        the static ratchet for that, and it also covers the launch record, which is where a
        whole-record write now legitimately happens. `LaunchState` is frozen, so a mutation on
        one cannot silently reach disk either -- but a caller could still build a stale record
        by hand and record it, and the field-scoped `LaunchState.record(**fields)` is what to
        use instead.

        AST, not a source grep: `wizard.py` carries the words `cfg.save()` inside an
        explaining comment, and a grep would count that comment as the defect it warns about.
        """
        import ast as _ast
        import pathlib as _pathlib

        for rel in ("src/kiro_crew/cloud/wizard.py", "src/kiro_crew/cli_cloud.py"):
            path = _pathlib.Path(__file__).resolve().parents[1] / rel
            tree = _ast.parse(path.read_text(encoding="utf-8"))
            offenders = [
                n.lineno
                for n in _ast.walk(tree)
                if isinstance(n, _ast.Call)
                and isinstance(n.func, _ast.Attribute)
                and n.func.attr == "save"
                and isinstance(n.func.value, _ast.Name)
                and n.func.value.id in {"cfg", "config", "cloud_config", "state", "launch_state"}
            ]
            assert not offenders, (
                f"{rel} saves a whole snapshot at line(s) {offenders}; write the fields this "
                "caller owns with LaunchState.record(profile=..., region=..., last_tag=...)"
            )

    def test_a_good_credential_beside_a_bad_secret_does_not_register(self):
        """Every reference is validated, not just the first that matches.

        An early return accepted a valid credential ref sitting beside a malformed or
        cross-crew one, and the engine then refused the whole document at launch --
        registering a lane that rejects every launch through it.
        """
        same_crew = [
            "kirocrew/crew/demo/OTHER_KEY",
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:"
            "kirocrew/crew/demo/OTHER_KEY-AbCdEf",
        ]
        cross_crew = [
            "kirocrew/crew/other/OTHER_KEY",
            "arn:aws:secretsmanager:us-east-1:999988887777:secret:"
            "kirocrew/crew/other/OTHER_KEY-AbCdEf",
        ]
        good = {**COMPLETE_FARGATE, "secrets": [list(CREDENTIAL_SECRET), same_crew]}
        assert FargateConfig.from_mapping(good) is not None, "a same-crew sibling is fine"

        for label, bad in (("cross-crew", cross_crew), ("malformed", ["junk", "arn:bogus"])):
            block = {**COMPLETE_FARGATE, "secrets": [list(CREDENTIAL_SECRET), bad]}
            assert FargateConfig.from_mapping(block) is None, label

    @pytest.mark.parametrize("field", ["subnets", "security_groups"])
    @pytest.mark.parametrize("bad", [5, "", None, {"a": 1}])
    def test_one_bad_list_member_voids_the_block(self, field: str, bad: object):
        """All-or-nothing, because filtering silently changed the placement.

        Dropping the bad member launched the task in whichever subnets survived -- a
        placement the operator never wrote. One bad member voids the block, exactly as
        one bad secret entry does.
        """
        block = {**COMPLETE_FARGATE, field: ["subnet-ok", bad]}
        assert FargateConfig.from_mapping(block) is None, f"{field}={bad!r}"

    @pytest.mark.parametrize(("value", "expected"), [(True, True), (False, False)])
    def test_a_boolean_public_ip_flag_is_read_as_written(self, value: bool, expected: bool):
        config = FargateConfig.from_mapping({**COMPLETE_FARGATE, "assign_public_ip": value})
        assert config is not None
        assert config.assign_public_ip is expected

    def test_an_absent_public_ip_flag_defaults_to_false(self):
        assert "assign_public_ip" not in COMPLETE_FARGATE
        config = FargateConfig.from_mapping(COMPLETE_FARGATE)
        assert config is not None
        assert config.assign_public_ip is False

    def test_a_movable_tag_is_refused_here_rather_than_at_launch(self):
        """``taskdef.py`` requires ``<repo>@sha256:<64 hex>``.

        Caught at the file boundary, an operator learns it where they typed it. Left
        to the launch, the same mistake surfaces as a refusal from a lane they were
        offered, with nothing pointing back at ``cloud.json``.
        """
        assert FargateConfig.from_mapping({**COMPLETE_FARGATE, "image": "repo:v1"}) is None

    def test_one_bad_secret_entry_drops_the_whole_block(self):
        """Not just that entry.

        Launching with one fewer secret than the operator wrote starts a task that
        then fails on a missing variable -- which is harder to trace than a lane
        that was never offered.
        """
        two = {**COMPLETE_FARGATE, "secrets": [COMPLETE_FARGATE["secrets"][0], ["broken"]]}
        assert FargateConfig.from_mapping(two) is None

    def test_a_corrupt_block_leaves_the_rest_of_the_config_readable(self, tmp_path):
        """One bad block must not cost the profile and region too."""
        p = tmp_path / "cloud.json"
        p.write_text(
            json.dumps({"profile": "dev", "region": "us-west-2", "fargate": {"cluster": "c"}}),
            encoding="utf-8",
        )
        loaded = CloudConfig.load(p)
        assert loaded.fargate_config() is None
        assert loaded.profile == "dev"
        assert loaded.region == "us-west-2"

    def test_no_secret_value_field_exists_to_write_one_into(self):
        """The file's contract is identifiers only, and the shape enforces it.

        A secret is named by its canonical name and its ARN; the value is fetched by
        the task's execution role before the container starts. A field for a value
        would be the first place someone put one.
        """
        fields = {f.name for f in FargateConfig.__dataclass_fields__.values()}
        for forbidden in ("secret_values", "value", "values", "password", "token"):
            assert forbidden not in fields


class TestCloudConfig:
    def test_defaults(self, tmp_path):
        cfg = CloudConfig.load(tmp_path / "cloud.json")
        assert cfg.profile == ""
        assert cfg.region == DEFAULT_REGION
        assert cfg.last_tag == ""

    def test_over_long_last_tag_sanitized_to_empty(self, tmp_path):
        # A 52-63 char last_tag must be sanitized to "" on load — NOT carried
        # into the resume path where validate_tag (cap 51) would raise. The
        # sanitizer's job is "no last launch", not a crash. Keep _TAG_RE in
        # lockstep with ec2._TAG_RE.
        from kiro_crew.cloud import ec2

        assert ec2._TAG_RE.pattern == r"^[a-zA-Z0-9-]{1,51}$"  # the cap we mirror
        p = tmp_path / "cloud.json"
        p.write_text('{"profile": "dev", "region": "us-east-1", "last_tag": "%s"}' % ("a" * 60))
        cfg = CloudConfig.load(p)
        assert cfg.last_tag == ""  # too long -> dropped, no ValidationError later
        # A malformed-charset tag is likewise dropped.
        p.write_text('{"last_tag": "bad tag!"}')
        assert CloudConfig.load(p).last_tag == ""
        # A valid 51-char tag is kept.
        p.write_text('{"last_tag": "%s"}' % ("k" * 51))
        assert CloudConfig.load(p).last_tag == "k" * 51

    def test_corrupt_file_falls_back_to_defaults(self, tmp_path):
        p = tmp_path / "cloud.json"
        p.write_text("not json{{{")
        cfg = CloudConfig.load(p)
        assert cfg.region == DEFAULT_REGION

    def test_non_object_json_falls_back_to_defaults(self, tmp_path):
        # Valid JSON that isn't an object ("hello", [1,2], 42, null) must not
        # raise AttributeError out of load() — that would give a raw traceback on
        # every `kirocrew cloud` command (handle_cloud only catches AWS/validation
        # errors). Honor the tolerate-a-corrupt-file promise.
        for body in ('"hello"', "[1, 2, 3]", "42", "null", "true"):
            p = tmp_path / "cloud.json"
            p.write_text(body)
            cfg = CloudConfig.load(p)
            assert cfg.region == DEFAULT_REGION
            assert cfg.profile == ""
            assert cfg.last_tag == ""

    def test_missing_region_coerced_to_default(self, tmp_path):
        p = tmp_path / "cloud.json"
        p.write_text('{"profile": "dev", "region": ""}')
        cfg = CloudConfig.load(p)
        assert cfg.region == DEFAULT_REGION


class TestCloudConfigIsSealedAgainstAgentWrites:
    """``cloud.json`` must not be writable through an agent's file-edit tool.

    The Fargate block made this file an input to a security decision:
    ``fargate.image`` chooses the container a launch runs, and the task's execution
    role delivers the model credential into that container. An agent that could
    rewrite the field could name a digest-pinned image of its own -- the digest rule
    constrains the reference's FORM, not who owns the registry -- while leaving the
    owner's placement and secrets intact, so the owner's next launch would hand the
    credential to an image they never chose.
    """

    #: Spelled out rather than looped from the production tuple: a test derived from
    #: the same list the code reads would keep passing after someone emptied it.
    PATHS = ("~/.kiro/crew/cloud.json", "~/.kirocrew/cloud.json")

    @pytest.mark.parametrize("path", PATHS)
    def test_an_agent_may_not_write_it(self, path: str):
        from kiro_crew.security.paths import is_sensitive_write_path

        assert is_sensitive_write_path(path) is True

    @pytest.mark.parametrize("path", PATHS)
    def test_it_stays_readable(self, path: str):
        """Write-protected, not sensitive: the selector reads it on every request.

        Blocking reads would take the Set-up tab down instead of protecting it.
        """
        from kiro_crew.security.paths import is_sensitive_path

        assert is_sensitive_path(path) is False

    def test_the_predicate_can_still_say_no(self):
        """Positive control, so the two assertions above cannot pass vacuously.

        A sibling path under the same crew home that nothing protects must come back
        writable; without this, a predicate that answered True for everything would
        satisfy this class.
        """
        from kiro_crew.security.paths import is_sensitive_write_path

        assert is_sensitive_write_path("~/.kiro/crew/not-a-protected-leaf.json") is False


class TestTheSealedCloudConfigNameCannotBeAnAlias:
    """A bind mount seals a link's REFERENT, so the sealed leaf must not be a link.

    Otherwise the lexical name stays replaceable in a writable parent: a sandboxed
    process unlinks it, drops its own file there, and the seal is intact around a name
    that now chooses which image a Fargate launch runs.
    """

    def test_cloud_json_is_on_the_nofollow_file_list(self):
        from kiro_crew import sandbox

        assert "cloud.json" in sandbox._CREW_NOFOLLOW_READONLY_FILE_LEAVES

    def test_every_nofollow_file_leaf_is_also_precreated(self):
        """Mirrors the assert the directory list carries.

        A nofollow leaf that is not materialised has no seal to protect on a default
        install, so the strict check would guard a name nothing binds.
        """
        from kiro_crew import sandbox

        assert set(sandbox._CREW_NOFOLLOW_READONLY_FILE_LEAVES) <= set(
            sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES
        )

    def test_the_list_is_not_everything(self):
        """Positive control: the strict path is opt-in, not applied to every leaf."""
        from kiro_crew import sandbox

        assert set(sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES) - set(
            sandbox._CREW_NOFOLLOW_READONLY_FILE_LEAVES
        )


def _strict_check(target: str) -> None:
    """The strict refusal with fixed wording, so the tests below measure SHAPE only.

    ``harm`` and ``remedy`` are required of every caller because the two strict leaves are
    refused for different reasons -- an aliased ``cloud.json`` picks the image a launch runs,
    an aliased launch record picks the stack a destroy deletes -- and neither may inherit the
    other's message. Which words those are is not what this class is about, so they are
    supplied once here rather than repeated in every case.
    """
    from kiro_crew import sandbox

    sandbox._require_real_file_nofollow(
        target,
        harm="pick what the launch does",
        remedy="Make the path a lone regular file.",
    )


class TestTheStrictCloudConfigSealRefusesEveryUncoveredName:
    """A read-only bind seals a MOUNT, not an inode, so two names escape it.

    A symlink leaves the lexical name replaceable; a second hardlink puts an alias
    outside the mount that reaches the same inode. For an ordinary ceiling the codebase
    only WARNS about both, because refusing would break a dotfile manager or a snapshot
    tool. For ``cloud.json`` a write through either name picks the container image a
    Fargate launch runs, and the execution role delivers the model credential into it,
    so the strict leaf refuses where the rest warn.
    """

    #: A block present means an alias could choose the image a launch runs, which is
    #: the only reason this leaf refuses where every other one warns.
    RISKY = '{"profile": "p", "fargate": {"cluster": "c"}}'
    #: No block, so an alias selects nothing: treated like every other ceiling.
    HARMLESS = '{"profile": "p", "region": "us-east-1"}'

    @staticmethod
    def _strict_target(tmp_path):
        from kiro_crew import sandbox

        return tmp_path / sandbox._CREW_NOFOLLOW_READONLY_FILE_LEAVES[0]

    def test_a_lone_regular_file_is_accepted(self, tmp_path):
        p = self._strict_target(tmp_path)
        p.write_text(self.RISKY, encoding="utf-8")
        _strict_check(str(p))

    def test_an_absent_file_is_accepted(self, tmp_path):
        """Absent is the publish path's job, not this check's."""
        _strict_check(str(self._strict_target(tmp_path)))

    def test_a_symlink_is_refused(self, tmp_path):
        from kiro_crew import sandbox

        real = tmp_path / "elsewhere.json"
        real.write_text(self.RISKY, encoding="utf-8")
        p = self._strict_target(tmp_path)
        p.symlink_to(real)
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="SYMLINK"):
            _strict_check(str(p))

    def test_a_hardlinked_file_is_refused(self, tmp_path):
        """The shape the earlier symlink-only check missed entirely."""
        from kiro_crew import sandbox

        p = self._strict_target(tmp_path)
        p.write_text(self.RISKY, encoding="utf-8")
        (tmp_path / "alias.json").hardlink_to(p)
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="hardlink"):
            _strict_check(str(p))

    def test_a_directory_at_the_name_is_refused(self, tmp_path):
        from kiro_crew import sandbox

        p = self._strict_target(tmp_path)
        p.mkdir()
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="not a regular file"):
            _strict_check(str(p))

    def test_an_unreadable_alias_is_treated_as_risky(self, tmp_path):
        """Contents are never read now, so even an unreadable alias refuses."""
        from kiro_crew import sandbox

        p = self._strict_target(tmp_path)
        p.write_text(self.RISKY, encoding="utf-8")
        (tmp_path / "alias.json").hardlink_to(p)
        p.chmod(0o000)
        try:
            with pytest.raises(sandbox.SandboxCeilingUnsealable):
                _strict_check(str(p))
        finally:
            p.chmod(0o600)

    #: Contents that a content-based rule would have judged differently. The refusal must
    #: not vary across them: the agent supplies the contents, so any rule reading them can
    #: be satisfied by writing something else. The escaped spellings are the specific
    #: bypass a byte-level probe had; the no-block and empty rows are the ones an earlier
    #: exemption let through, which is how an agent could write a whole block via the alias.
    ENCODINGS = [
        ("plain block", '{"fargate": {"cluster": "c"}}'),
        ("escaped key", '{"\\u0066argate": {"cluster": "c"}}'),
        ("fully escaped key", '{"\\u0066\\u0061\\u0072\\u0067\\u0061\\u0074\\u0065": {}}'),
        ("no block at all", '{"profile": "p", "region": "us-east-1"}'),
        ("empty object", "{}"),
        ("unparseable", "{not json"),
        ("not an object", '"hello"'),
        ("empty file", ""),
    ]

    @pytest.mark.parametrize(("label", "body"), ENCODINGS)
    def test_a_symlink_is_refused_whatever_the_contents(self, tmp_path, label, body):
        from kiro_crew import sandbox

        real = tmp_path / "elsewhere.json"
        real.write_text(body, encoding="utf-8")
        p = self._strict_target(tmp_path)
        p.symlink_to(real)
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="SYMLINK"):
            _strict_check(str(p))

    @pytest.mark.parametrize(("label", "body"), ENCODINGS)
    def test_a_hardlink_is_refused_whatever_the_contents(self, tmp_path, label, body):
        from kiro_crew import sandbox

        p = self._strict_target(tmp_path)
        p.write_text(body, encoding="utf-8")
        (tmp_path / "alias.json").hardlink_to(p)
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="hardlink"):
            _strict_check(str(p))

    def test_the_rule_reads_no_contents_at_all(self, tmp_path):
        """The property that makes the bypass class empty rather than narrower.

        Any rule deciding on contents can be satisfied by writing different contents,
        and the agent is who writes them. Asserted on the source so a future contents
        check cannot creep back in without failing here.
        """
        import inspect

        from kiro_crew import sandbox

        src = inspect.getsource(sandbox._require_real_file_nofollow)
        for reader in ("open(", "read(", "json.loads", "carries_risk"):
            assert reader not in src, f"the strict refusal must not read contents: {reader}"


class TestTheCredentialGateAgreesWithTheEngine:
    """No spelling may pass this gate and then be refused at launch.

    The gate exists so an incomplete block leaves the lane UNREGISTERED instead of
    registered-and-refusing. A gate that accepts a name the engine rejects recreates
    that exact state from inside the gate, which is what a tail-only check did: both a
    bare ``KIRO_API_KEY`` and a wrong-prefix ``junk/KIRO_API_KEY`` passed here and were
    refused by ``identity.secret_env_name``.
    """

    @staticmethod
    def _with_credential_named(name: str) -> dict:
        return {
            **COMPLETE_FARGATE,
            "secrets": [
                [name, f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{name}-AbCdEf"]
            ],
        }

    @pytest.mark.parametrize(
        "name",
        [
            "KIRO_API_KEY",
            "junk/KIRO_API_KEY",
            "kirocrew/crew/KIRO_API_KEY",
            "kirocrew/crew//KIRO_API_KEY",
            "kirocrew/crew/a/b/KIRO_API_KEY",
            "kirocrew/crew/demo/OTHER_KEY",
        ],
    )
    def test_a_name_the_engine_would_refuse_does_not_register_the_lane(self, name: str):
        assert FargateConfig.from_mapping(self._with_credential_named(name)) is None, name

    def test_a_conforming_name_is_accepted(self):
        block = self._with_credential_named("kirocrew/crew/demo/KIRO_API_KEY")
        assert FargateConfig.from_mapping(block) is not None

    def test_every_name_this_gate_accepts_the_engine_also_accepts(self):
        """The property itself, over a MATRIX, checked against the engine.

        Written as a product rather than a hand-picked list because a hand-picked list
        is what let this defect recur three times: each round fixed the spelling that
        had been reported and left the next one. Any name this module admits must
        survive ``secret_env_name``, which is what the launch path calls, so the two
        halves cannot drift apart without a red here.
        """
        import itertools

        from kiro_crew.cloud.fargate.identity import SecretRef, secret_env_name

        prefixes = ["kirocrew/crew/", "kirocrew/crews/", "junk/", ""]
        crews = [
            "demo",
            "DEMO",
            "De-mo",
            "-demo",
            "demo-",
            "d" * 32,
            "d" * 33,
            "dem_o",
            "d\u00e9",
            "a",
            "a-b",
            "a--b",
            "1",
            "",
            "ab/cd",
        ]
        keys = ["KIRO_API_KEY", "kiro_api_key", "OTHER"]

        accepted = 0
        for prefix, crew, key in itertools.product(prefixes, crews, keys):
            name = f"{prefix}{crew}/{key}"
            if FargateConfig.from_mapping(self._with_credential_named(name)) is None:
                continue
            accepted += 1
            arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{name}-AbCdEf"
            secret_env_name(SecretRef(name=name, arn=arn))
        assert accepted, "the matrix must contain at least one accepted name"

    @pytest.mark.parametrize(
        "crew",
        ["DEMO", "De-mo", "-demo", "demo-", "dem_o", "d\u00e9", "d" * 33, ""],
    )
    def test_a_crew_segment_the_engine_would_refuse_does_not_register(self, crew: str):
        """Each of these passed the earlier truthiness check and died in provisioning."""
        name = f"kirocrew/crew/{crew}/KIRO_API_KEY"
        assert FargateConfig.from_mapping(self._with_credential_named(name)) is None, crew

    @pytest.mark.parametrize("crew", ["demo", "a", "1", "a-b", "a--b", "d" * 32])
    def test_a_conforming_crew_segment_registers(self, crew: str):
        name = f"kirocrew/crew/{crew}/KIRO_API_KEY"
        assert FargateConfig.from_mapping(self._with_credential_named(name)) is not None, crew
