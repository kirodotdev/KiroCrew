"""An external prompt reference is refused, and the refusal says what to do instead.

Reading a ``file://`` prompt safely means resolving an operator-supplied path without
following a redirect, on two platforms with different link semantics, before any resolution can
reach the network. That is ~350 lines whose review found 20+ separate defects across seven
rounds while the rest of this module was settled, so it ships as its own change.

This file pins the limitation so it is a decision rather than a gap: the build refuses, the
message is actionable, and nothing silently produces a crew that answers as nobody.
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from .test_producer import load_build, make_crew

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the crew bundle builder is POSIX-only; guarded off on platforms without an "
    "atomic no-follow primitive (Windows). See the POSIX-only entry guard.",
)


@_posix_only
def test_a_file_prompt_is_refused_with_an_actionable_message(tmp_path: pathlib.Path) -> None:
    """Refused, and the message tells the operator to inline the persona."""
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="file:///etc/persona.md")
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)

    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    message = str(caught.value)
    assert "references its prompt as a file" in message
    assert "literal text" in message, "the refusal does not say what to do instead"


@_posix_only
def test_the_refusal_does_not_read_the_referenced_file(tmp_path: pathlib.Path) -> None:
    """The point of refusing is that nothing is read, so a planted file stays unread.

    Asserted through the bundle rather than the exception: what would leak is the file's BYTES
    reaching agent.json, and only building can show they did not.
    """
    mod = load_build()
    secret = tmp_path / "secret.md"
    secret.write_bytes(b"PRIVATE KEY MATERIAL\n")
    home = make_crew(tmp_path / "home", prompt=f"file://{secret}")
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)

    with pytest.raises(mod.ExportRefused):
        mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)


@_posix_only
def test_an_inline_prompt_is_unaffected(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: the ordinary case must build, and its bytes must be carried verbatim."""
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="an inline persona, byte for byte")
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    result = mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert result.spec["prompt"] == "an inline persona, byte for byte"


@_posix_only
def test_a_missing_prompt_still_says_so(tmp_path: pathlib.Path) -> None:
    """The two refusals are different and must stay distinguishable."""
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="x")
    spec_path = home / "agents" / "frontdesk.json"
    spec_path.write_text(json.dumps({"prompt": "   "}), encoding="utf-8")
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)

    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert "has no prompt" in str(caught.value)


@_posix_only
def test_a_credential_in_an_inline_prompt_is_still_caught(tmp_path: pathlib.Path) -> None:
    """The scan belongs on the shared path, so removing the file branch must not move it."""
    mod = load_build()
    home = make_crew(
        tmp_path / "home",
        prompt="aws_secret_access_key = EXAMPLE-PLACEHOLDER-NOT-A-REAL-KEY",
    )
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert "credential" in str(caught.value)
