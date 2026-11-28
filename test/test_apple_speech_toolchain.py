"""The speech helper's compiler comes from the developer directory its SDK comes from."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew import apple_speech


@pytest.mark.parametrize("active", ["xcode", "clt", "unset"])
def test_resolver_takes_the_compiler_xcode_select_points_at(tmp_path, active):
    """With both toolchains installed the compiler must match the SDK.

    `_sdk_path` asks `xcrun`, which follows `xcode-select`. Command Line Tools
    older than the selected Xcode cannot read that Xcode's SDK, so taking the
    first fixed path regardless builds nothing.
    """
    clt = tmp_path / "CommandLineTools" / "usr" / "bin" / "swiftc"
    xcode_dir = tmp_path / "Xcode.app" / "Contents" / "Developer"
    xcode = xcode_dir / "Toolchains" / "XcodeDefault.xctoolchain" / "usr" / "bin" / "swiftc"
    for swiftc in (clt, xcode):
        swiftc.parent.mkdir(parents=True)
        swiftc.write_text("", "utf-8")
    link = tmp_path / "xcode_select_link"
    if active == "xcode":
        link.symlink_to(xcode_dir)
    elif active == "clt":
        link.symlink_to(tmp_path / "CommandLineTools")
    expected = xcode if active == "xcode" else clt
    with (
        patch.object(apple_speech, "_SWIFTC_FIXED_PATHS", (str(clt), str(xcode))),
        patch.object(apple_speech, "_XCODE_SELECT_LINK", str(link), create=True),
        patch.object(apple_speech, "_is_trusted_toolchain", lambda p: True),
    ):
        assert apple_speech._swiftc_fast() == str(expected)
