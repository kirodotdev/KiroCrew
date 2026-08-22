"""Two backups taken in the same second must not become one object."""

from __future__ import annotations

import pytest

from kiro_crew import snapshot_remote as remote

NAME = "kirocrew-snapshot-20260101T000000Z.tar.gz"


class TestEachUploadGetsItsOwnKey:
    def test_the_same_bundle_name_yields_different_object_names(self) -> None:
        """The snapshot name is second-resolution, so it cannot be the key on its own."""
        names = {remote._unique_object_name(NAME) for _ in range(50)}

        assert len(names) == 50, "two uploads in one second would overwrite each other"

    def test_it_still_reads_as_the_same_archive(self) -> None:
        got = remote._unique_object_name(NAME)

        assert got.endswith(".tar.gz"), "the suffix broke the extension"
        assert got.startswith("kirocrew-snapshot-20260101T000000Z-"), got

    @pytest.mark.parametrize(
        "filename",
        ["bundle.tgz", "bundle.tar.gz", "bundle", "odd.name.tar.gz"],
    )
    def test_every_shape_keeps_its_extension_and_gains_a_suffix(self, filename: str) -> None:
        got = remote._unique_object_name(filename)

        assert got != filename
        if "." in filename:
            for suffix in (".tar.gz", ".tgz"):
                if filename.endswith(suffix):
                    assert got.endswith(suffix)
                    break

    def test_the_upload_path_uses_it(self) -> None:
        import inspect

        src = inspect.getsource(remote.upload)
        assert (
            "_unique_object_name(" in src
        ), "the uploader still keys the object by the bundle name alone"
