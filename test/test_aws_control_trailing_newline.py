r"""AWS Control: a drive key segment or install id that ends in a newline is refused.

Python's ``$`` also matches just before a trailing ``"\n"``, so a ``$``-anchored
check would accept ``"a.txt\n"`` and ``"<32 hex>\n"``; the key segment and install
id patterns end at ``\Z``. A request key reaches an S3 object name, and a listed
folder name reaches the backup owner check, so each is tested at the helper or
route a caller uses.
No test creates a file or folder whose name holds a newline.
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest
from aws_control_routes_helpers import (
    ACCOUNT,
    _consent_ok,
    _drive_found,
    _enabled_owner_env,
    _payload,
    _registered,
    _request,
)

from kiro_crew.apps.builtins.aws_control.backend import backup
from kiro_crew.apps.builtins.aws_control.backend import routes as routes_mod
from kiro_crew.apps.builtins.aws_control.backend import storage
from kiro_crew.apps.builtins.aws_control.backend.backup_parts import identity

_INSTALL = "0123456789abcdef0123456789abcdef"
_OTHER = "fedcba9876543210fedcba9876543210"


class TestKeyValidation:
    @pytest.mark.parametrize(
        "key",
        ["a.txt\n", "photos/a.txt\n", "photos\n/a.txt", "photos 2026/img (1).png\n"],
    )
    def test_a_key_with_a_segment_ending_in_a_newline_is_refused(self, key: str) -> None:
        assert storage.validate_key(key) is not None

    def test_the_same_keys_without_the_newline_are_accepted(self) -> None:
        for key in ("a.txt", "photos/a.txt", "photos 2026/img (1).png"):
            assert storage.validate_key(key) is None, key


class TestUploadRoute:
    def test_upload_refuses_a_key_ending_in_a_newline_before_any_write(self) -> None:
        handlers = _registered()
        p1, p2, p3 = _enabled_owner_env()
        req = _request(
            "POST",
            f"/drive/{ACCOUNT}/upload?section=drive&key=a.txt%0A",
            match_info={"account": ACCOUNT},
        )
        assert req.rel_url.query["key"] == "a.txt\n"
        with (
            p1,
            p2,
            p3,
            _consent_ok(),
            _drive_found(),
            mock.patch.object(routes_mod.storage_mod, "put_file") as put,
        ):
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/upload")](req)  # type: ignore[operator]
            )
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_key"
        put.assert_not_called()


class TestInstallIdOwnership:
    def test_a_folder_named_like_an_install_id_plus_a_newline_is_not_an_owner(self) -> None:
        origin, owner = backup.classify_key(f"snapshots/{_OTHER}\n/a.tar.gz", _INSTALL)
        assert (origin, owner) == (backup.ORIGIN_LEGACY, "")

    def test_a_real_install_id_folder_still_names_its_owner(self) -> None:
        origin, owner = backup.classify_key(f"snapshots/{_OTHER}/a.tar.gz", _INSTALL)
        assert (origin, owner) == (backup.ORIGIN_OTHER, _OTHER)

    def test_a_stored_install_id_ending_in_a_newline_is_not_an_identity(self) -> None:
        assert identity._stored_identity({identity.INSTALL_KEY: {"id": _INSTALL + "\n"}}) is None
        assert identity._stored_identity({identity.INSTALL_KEY: {"id": _INSTALL}})["id"] == (
            _INSTALL
        )
