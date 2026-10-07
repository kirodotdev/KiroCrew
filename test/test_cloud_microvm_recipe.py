"""The recipe zip, and that it reuses the Fargate lane's recipe rather than copying it.

The clause these tests exist for is "reuse the packaging and recipe code; do not
write a second bundle format". So the first things asserted are not behaviours of
this module at all. They are that the Dockerfile in the zip IS the Fargate lane's
``Dockerfile.crew``, that the bundle layout checked is that Dockerfile's own
``COPY`` list, and that the bundle digest the image cache keys on IS
``packaging.build``'s function. Each of those, implemented a second time, would be
a second answer to a question one lane has already answered -- and the looser
answer is the one that ships the wrong content.
"""

from __future__ import annotations

import random
import re
import zipfile
from pathlib import Path

import pytest

from kiro_crew.cloud.microvm import recipe as recipe_module
from kiro_crew.cloud.microvm.recipe import (
    CREW_DOCKERFILE,
    DOCKERFILE_NAME,
    REQUIRED_BUNDLE_DIR,
    REQUIRED_BUNDLE_MEMBERS,
    AssembledRecipe,
    RecipeRefused,
    assemble,
    bundle_digest_of,
    check_layout,
)

BASE = "arn:aws:lambda:us-east-1:123456789012:microvm-image/al2023-base"
#: The base image the build RUNS on. Recorded in the recipe; it is not what
#: identifies the base content, because this lane concatenates the base's own
#: layers into the Dockerfile rather than starting from a built base image.
#: :func:`base_recipe_digest` is what says which base content.


@pytest.fixture(autouse=True)
def _clean_staging():
    recipe_module.forget_all()
    yield
    recipe_module.forget_all()


#: A fixed timestamp for fixture wheels. Zip stores seconds in units of two, so an
#: even one survives the round trip and can be compared against what was written.
_FIXTURE_STAMP = (2020, 1, 2, 3, 4, 6)


def _write_wheel(path: Path, members: dict[str, bytes]) -> Path:
    """A real wheel: a zip, with a ``RECORD`` listing what is in it.

    A wheel IS a zip and this lane reads it as one -- :func:`dashboard_members` asks
    what it carries before the recipe is assembled -- so a fixture that is not a
    readable archive cannot exercise the code under test.
    """
    record = "".join(f"{name},sha256=deadbeef,{len(body)}\n" for name, body in members.items())
    body_by_name = {**members, "kiro_crew-0.0.0.dist-info/RECORD": record.encode("utf-8")}
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in body_by_name.items():
            # An explicit timestamp, because `writestr` with a plain name stamps the
            # current second: two fixture wheels built either side of a second
            # boundary would then differ, and the byte-stability check below would
            # fail for a reason that has nothing to do with the code it tests.
            archive.writestr(zipfile.ZipInfo(name, date_time=_FIXTURE_STAMP), body)
    return path


@pytest.fixture()
def wheel(tmp_path) -> Path:
    """A stand-in for the Kiro Crew wheel the base recipe installs.

    A minimal but REAL wheel. It is one of the inputs the base recipe digest covers,
    so a zip assembled without one would be an image with no serving code; and the
    lane reads its member list to refuse one carrying the dashboard, so an
    unreadable stand-in would refuse every test instead of the case under test.
    """
    return _write_wheel(
        tmp_path / "kiro_crew-0.0.0-py3-none-any.whl",
        {"kiro_crew/__init__.py": b"# serving code\n"},
    )


@pytest.fixture()
def bundle(tmp_path) -> Path:
    """A bundle with exactly what ``Dockerfile.crew`` copies, and nothing else."""
    root = tmp_path / "bundle"
    (root / "skills" / "demo").mkdir(parents=True)
    (root / "manifest.json").write_text('{"crew_name": "demo", "digest": "sha256:x"}\n')
    (root / "agent.json").write_text('{"name": "demo"}\n')
    (root / "mcp.json").write_text("{}\n")
    (root / "skills" / "demo" / "SKILL.md").write_text("# demo\n")
    return root


class TestItReusesTheFargateRecipe:
    def test_the_shipped_dockerfile_is_the_fargate_crew_layer(self):
        """Not a MicroVM copy of it. The same file on disk, which is why a change to
        the crew layer cannot leave this lane building the old one."""
        assert CREW_DOCKERFILE.name == "Dockerfile.crew"
        assert CREW_DOCKERFILE.is_file(), f"{CREW_DOCKERFILE} is not shipped"
        assert "aws_control" in CREW_DOCKERFILE.parts

    def test_the_zip_carries_that_file_verbatim_apart_from_its_base_reference(
        self, bundle, wheel, tmp_path
    ):
        """Every line of Dockerfile.crew is in the zip's Dockerfile except the two
        that name a registry base this lane does not have."""
        out = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "recipe.zip",
        )
        with zipfile.ZipFile(out.path) as archive:
            built = archive.read(DOCKERFILE_NAME).decode("utf-8")
        for line in CREW_DOCKERFILE.read_text(encoding="utf-8").splitlines():
            if line in ("ARG BASE", "FROM ${BASE}") or "${BASE}" in line:
                continue
            assert line in built, f"Dockerfile.crew's {line!r} is not in the recipe"

    def test_the_required_members_are_that_dockerfiles_copy_list(self):
        """Read out of the Dockerfile rather than restated, so a COPY added there
        without a change here fails this test instead of failing a Lambda build."""
        text = CREW_DOCKERFILE.read_text(encoding="utf-8")
        copies = [line for line in text.splitlines() if line.startswith("COPY ")]
        assert copies, "Dockerfile.crew copies nothing; the layout contract moved"
        named = " ".join(copies)
        for member in REQUIRED_BUNDLE_MEMBERS:
            assert member in named, f"{member} is required here but Dockerfile.crew drops it"
        assert REQUIRED_BUNDLE_DIR in named

    def test_the_bundle_files_sit_at_the_zips_root(self, bundle, wheel, tmp_path):
        """No ``bundle/`` prefix: Dockerfile.crew's build context IS the bundle
        directory, and a prefix would mean editing its COPY lines."""
        out = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "recipe.zip",
        )
        with zipfile.ZipFile(out.path) as archive:
            names = set(archive.namelist())
        assert "manifest.json" in names
        assert "agent.json" in names
        assert "mcp.json" in names
        assert "skills/demo/SKILL.md" in names


class TestItReusesThePackagingDigest:
    def test_the_digest_is_packagings_own_function(self, bundle, wheel, monkeypatch):
        """Delegated, not reimplemented. Proven by patching the owner and seeing
        this module's answer change with it."""
        import kiro_crew.apps.builtins.aws_control.crew.packaging.build as pkg

        monkeypatch.setattr(pkg, "bundle_digest", lambda root, also_skip=frozenset(): "sentinel")
        assert bundle_digest_of(bundle) == "sentinel"

    def test_the_real_function_answers_for_a_real_bundle(self, bundle, wheel):
        digest = bundle_digest_of(bundle)
        assert isinstance(digest, str) and digest

    def test_the_same_bundle_gives_the_same_digest(self, bundle, wheel):
        assert bundle_digest_of(bundle) == bundle_digest_of(bundle)

    def test_a_changed_bundle_gives_a_different_digest(self, bundle, wheel):
        before = bundle_digest_of(bundle)
        (bundle / "skills" / "demo" / "EXTRA.md").write_text("# extra\n")
        assert bundle_digest_of(bundle) != before

    def test_an_absent_bundle_is_refused(self, tmp_path):
        with pytest.raises(RecipeRefused, match="no crew bundle"):
            bundle_digest_of(tmp_path / "nope")


class TestCheckLayout:
    @pytest.mark.parametrize("member", REQUIRED_BUNDLE_MEMBERS)
    def test_a_missing_file_is_refused_here_not_on_lambda(self, bundle, wheel, member):
        (bundle / member).unlink()
        with pytest.raises(RecipeRefused, match="missing"):
            check_layout(bundle)

    def test_a_missing_skills_directory_is_refused(self, bundle, wheel):
        (bundle / REQUIRED_BUNDLE_DIR / "demo" / "SKILL.md").unlink()
        (bundle / REQUIRED_BUNDLE_DIR / "demo").rmdir()
        (bundle / REQUIRED_BUNDLE_DIR).rmdir()
        with pytest.raises(RecipeRefused, match="missing"):
            check_layout(bundle)

    def test_an_empty_skills_directory_is_accepted(self, bundle, wheel):
        """May be empty but must exist, which is what Dockerfile.crew says."""
        (bundle / REQUIRED_BUNDLE_DIR / "demo" / "SKILL.md").unlink()
        (bundle / REQUIRED_BUNDLE_DIR / "demo").rmdir()
        check_layout(bundle)

    def test_a_bundle_carrying_its_own_dockerfile_is_refused(self, bundle, wheel):
        """It would collide with the one at the zip's root, and whichever was
        written last would decide what got built."""
        (bundle / DOCKERFILE_NAME).write_text("FROM scratch\n")
        with pytest.raises(RecipeRefused, match="carries its own"):
            check_layout(bundle)

    def test_an_absent_bundle_is_refused(self, tmp_path):
        with pytest.raises(RecipeRefused, match="no crew bundle"):
            check_layout(tmp_path / "nope")


class TestTheCrewNameIsReadableFromTheBundle:
    """The control plane needs the crew's name at LAUNCH, before the guest exists.

    Inside the VM the same manifest becomes ``SMC_CREW_NAME``, and the guest's
    front compares a turn's ``model`` against it. The launch tag is a different
    fact -- this launch's id -- so the host reads the name here and records it.
    """

    def test_the_manifests_crew_name_is_returned(self, bundle):
        from kiro_crew.cloud.microvm.recipe import bundle_crew_name

        assert bundle_crew_name(bundle) == "demo"

    @pytest.mark.parametrize(
        "content",
        ["{not json", "[]", '{"crew_name": ""}', "null"],
        ids=["unparseable", "not-an-object", "empty-name", "null"],
    )
    def test_every_unusable_manifest_reads_as_no_name(self, bundle, content):
        """Empty rather than a raise: the caller's answer to empty is to say the
        name is not recorded, which is better than a launch that fails over a
        field only the turn path reads."""
        from kiro_crew.cloud.microvm.recipe import bundle_crew_name

        (bundle / "manifest.json").write_text(content)
        assert bundle_crew_name(bundle) == ""

    def test_an_absent_bundle_reads_as_no_name(self, tmp_path):
        from kiro_crew.cloud.microvm.recipe import bundle_crew_name

        assert bundle_crew_name(tmp_path / "nope") == ""


class TestAssemble:
    def test_the_recipe_names_both_digests(self, bundle, wheel, tmp_path):
        out = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "recipe.zip",
        )
        assert out.recipe.base_digest == recipe_module.base_recipe_digest(wheel)
        assert out.recipe.bundle_digest == bundle_digest_of(bundle)

    def test_the_dockerfile_sits_at_the_zips_root(self, bundle, wheel, tmp_path):
        """Lambda builds what it finds at the root; under a directory it builds nothing."""
        out = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "recipe.zip",
        )
        with zipfile.ZipFile(out.path) as archive:
            assert DOCKERFILE_NAME in archive.namelist()

    def test_a_zero_skill_bundle_still_matches_the_digest_it_was_signed_with(
        self, bundle, wheel, tmp_path
    ):
        """The bytes installed must be exactly the bytes that were signed.

        ``packaging.build`` writes the bundle's digest into its manifest, and the
        guest recomputes it from what it is about to install before trusting it.
        Both hash every file except the top-level manifest, so ANY file this
        assembly adds under the bundle prefix changes the recompute, the
        comparison fails, and the supervisor refuses the bundle at boot -- after
        which every turn reports the crew as unreachable, which reads like a
        network fault rather than a signature one.

        This runs the guest's own ``_content_digest`` over the extracted bundle,
        not a local reimplementation, so the thing asserted is the comparison the
        guest actually makes.
        """
        import json
        import sys

        for leaf in (bundle / "skills" / "demo" / "SKILL.md",):
            leaf.unlink()
        (bundle / "skills" / "demo").rmdir()

        # Sign the zero-skill bundle the way the producer does, so the manifest
        # carries the digest of exactly this content.
        signed = bundle_digest_of(bundle)
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        manifest["digest"] = signed
        (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        signed = bundle_digest_of(bundle)

        out = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "recipe.zip",
        )

        # Extract the bundle members the way the image's COPY would see them.
        installed = tmp_path / "installed"
        installed.mkdir()
        with zipfile.ZipFile(out.path) as archive:
            for name in archive.namelist():
                if name.startswith("container/") or name.startswith("vendor/"):
                    continue
                if name in ("Dockerfile", recipe_module.SSM_AGENT_CONFIG.name):
                    continue
                archive.extract(name, installed)

        sys.path.insert(0, str(recipe_module.RUNTIME_DIR))
        try:
            from container.supervisor.bundle import _content_digest
        finally:
            sys.path.remove(str(recipe_module.RUNTIME_DIR))

        assert _content_digest(installed) == signed, (
            "the assembled bundle does not hash to the digest its manifest was "
            "signed with, so the guest will refuse it at boot"
        )
        # And the directory the image's COPY needs is there all the same.
        with zipfile.ZipFile(out.path) as archive:
            assert any(
                n.rstrip("/") == REQUIRED_BUNDLE_DIR for n in archive.namelist()
            ), "the build context carries no skills path at all"

    def test_a_bundle_with_no_skills_still_carries_the_skills_directory(
        self, bundle, wheel, tmp_path
    ):
        """A crew that ships no skills is a SUPPORTED bundle, and it has to build.

        ``packaging.build`` advertises exactly this shape -- a valid bundle with
        the crew's persona only -- but a zip carries no empty directories, so
        ``skills/`` vanished from the build context and the image's own
        ``COPY ... skills /app/crew-bundle/skills`` failed to checksum ``/skills``.
        Because the image name is the recipe digest, that failure was permanent
        for that content.
        """
        for leaf in (bundle / "skills" / "demo" / "SKILL.md",):
            leaf.unlink()
        (bundle / "skills" / "demo").rmdir()
        out = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "recipe.zip",
        )
        with zipfile.ZipFile(out.path) as archive:
            names = archive.namelist()
            entry = next(n for n in names if n.rstrip("/") == REQUIRED_BUNDLE_DIR)
            info = archive.getinfo(entry)
        # A DIRECTORY entry: zero length, a trailing slash, and the directory bit.
        # A file here would enter the bundle's signed set and break the digest the
        # guest checks before installing.
        assert entry.endswith("/")
        assert info.file_size == 0
        assert info.external_attr >> 16 & 0o40000, "the entry is not marked a directory"
        assert not [
            n for n in names if n.startswith(f"{REQUIRED_BUNDLE_DIR}/") and n != entry
        ], "something was written INSIDE the skills prefix, which the bundle digest signs"

    def test_a_bundle_with_skills_gets_no_placeholder(self, bundle, wheel, tmp_path):
        """The placeholder exists only to make an empty directory survive a zip.
        Adding it beside real skills would ship a file nothing explains."""
        out = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "recipe.zip",
        )
        with zipfile.ZipFile(out.path) as archive:
            assert f"{REQUIRED_BUNDLE_DIR}/.keep" not in archive.namelist()

    def test_a_recipe_with_no_wheel_is_refused(self, bundle, tmp_path):
        """The base recipe installs the wheel FROM the build context, so a zip
        without one builds an image with no serving code."""
        with pytest.raises(RecipeRefused, match="no Kiro Crew wheel"):
            assemble(
                bundle_dir=bundle,
                wheel=tmp_path / "nope.whl",
                base_image_arn=BASE,
                out_zip=tmp_path / "recipe.zip",
            )

    def test_a_bad_layout_is_refused_before_anything_is_written(self, bundle, wheel, tmp_path):
        (bundle / "mcp.json").unlink()
        out_zip = tmp_path / "recipe.zip"
        with pytest.raises(RecipeRefused, match="missing"):
            assemble(
                bundle_dir=bundle,
                wheel=wheel,
                base_image_arn=BASE,
                out_zip=out_zip,
            )
        assert not out_zip.exists()

    def test_an_oversized_zip_is_refused_and_removed(self, bundle, wheel, tmp_path, monkeypatch):
        """Refused before the upload rather than after paying for it."""
        monkeypatch.setattr(recipe_module, "MAX_RECIPE_BYTES", 10)
        out_zip = tmp_path / "recipe.zip"
        with pytest.raises(RecipeRefused, match="over the"):
            assemble(
                bundle_dir=bundle,
                wheel=wheel,
                base_image_arn=BASE,
                out_zip=out_zip,
            )
        assert not out_zip.exists()

    def test_the_zip_is_byte_reproducible(self, bundle, wheel, tmp_path):
        """A zip that differs run to run makes 'did the recipe change' unanswerable."""
        first = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "a.zip",
        )
        second = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "b.zip",
        )
        assert first.path.read_bytes() == second.path.read_bytes()

    def test_an_assembled_recipe_reports_its_size(self, bundle, wheel, tmp_path):
        out = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "recipe.zip",
        )
        assert isinstance(out, AssembledRecipe)
        assert out.size_bytes == out.path.stat().st_size > 0


class TestUploader:
    def test_it_keys_the_object_by_the_recipe_digest(self, bundle, wheel, tmp_path):
        """Named by what is in it, so a re-upload overwrites rather than accumulates."""
        out = recipe_module.remember(
            assemble(
                bundle_dir=bundle,
                wheel=wheel,
                base_image_arn=BASE,
                out_zip=tmp_path / "recipe.zip",
            )
        )
        seen: list[tuple[str, str, Path]] = []
        upload = recipe_module.uploader(
            bucket="kc-recipes", put_object=lambda b, k, p: seen.append((b, k, p))
        )
        uri = upload(out.recipe)
        assert uri == f"s3://kc-recipes/recipes/{out.recipe.digest()}.zip"
        assert seen == [("kc-recipes", f"recipes/{out.recipe.digest()}.zip", out.path)]

    def test_uploading_an_unassembled_recipe_is_refused(self):
        """A resolver that asked for an upload before assemble() ran is a caller bug."""
        from kiro_crew.cloud.microvm.image import Recipe

        upload = recipe_module.uploader(bucket="kc-recipes", put_object=lambda b, k, p: None)
        with pytest.raises(RecipeRefused, match="no assembled recipe"):
            upload(
                Recipe(
                    base_image_arn=BASE,
                    base_digest="sha256:" + "e" * 64,
                    bundle_digest="sha256:" + "f" * 64,
                )
            )

    def test_a_staged_zip_that_vanished_is_refused(self, bundle, wheel, tmp_path):
        out = recipe_module.remember(
            assemble(
                bundle_dir=bundle,
                wheel=wheel,
                base_image_arn=BASE,
                out_zip=tmp_path / "recipe.zip",
            )
        )
        out.path.unlink()
        upload = recipe_module.uploader(bucket="kc-recipes", put_object=lambda b, k, p: None)
        with pytest.raises(RecipeRefused, match="no assembled recipe"):
            upload(out.recipe)


class TestItFeedsTheImageCache:
    def test_a_changed_bundle_changes_the_image_name(self, bundle, wheel, tmp_path):
        """So a crew whose bundle changed gets a new image instead of the old one."""
        before = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "a.zip",
        ).recipe.image_name()
        (bundle / "skills" / "demo" / "EXTRA.md").write_text("# extra\n")
        after = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "b.zip",
        ).recipe.image_name()
        assert before != after

    def test_a_changed_base_recipe_changes_the_image_name(self, bundle, wheel, tmp_path):
        """The base's identity here is the digest of its INPUTS -- the Dockerfiles,
        the container package and the wheel -- because this lane has no separately
        built base image to take a digest of. So a changed wheel is a changed base."""
        before = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "a.zip",
        ).recipe.image_name()
        _write_wheel(wheel, {"kiro_crew/__init__.py": b"# different serving code\n"})
        after = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "b.zip",
        ).recipe.image_name()
        assert before != after


class TestTheZipCarriesWhatTheBuildNeeds:
    """Every member the concatenated Dockerfile reads, asserted by name.

    A missing one is a build that runs on Lambda's capacity and fails minutes
    later in a log the operator does not hold.
    """

    def test_the_container_package_travels(self, bundle, wheel, tmp_path):
        out = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "recipe.zip",
        )
        with zipfile.ZipFile(out.path) as archive:
            names = archive.namelist()
        assert any(n.startswith("container/") for n in names)
        assert "container/microvm/hooks.py" in names

    def test_the_wheel_travels_where_the_base_recipe_looks_for_it(self, bundle, wheel, tmp_path):
        out = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "recipe.zip",
        )
        with zipfile.ZipFile(out.path) as archive:
            assert f"vendor/{wheel.name}" in archive.namelist()

    def test_the_ssm_agent_config_travels(self, bundle, wheel, tmp_path):
        out = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "recipe.zip",
        )
        with zipfile.ZipFile(out.path) as archive:
            assert "microvm-ssm-agent.json" in archive.namelist()

    def test_no_pycache_travels(self, bundle, wheel, tmp_path):
        """Compiled bytecode is machine-specific, so shipping it would make the
        zip vary with whoever assembled it."""
        out = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "recipe.zip",
        )
        with zipfile.ZipFile(out.path) as archive:
            assert not [n for n in archive.namelist() if "__pycache__" in n]


class TestTheConcatenatedDockerfile:
    """Three recipes in one file, because this lane has no registry for a base."""

    def test_it_carries_all_three_layers(self, wheel):
        text = recipe_module.assemble_dockerfile("sha256:deadbeef")
        assert "crew layer (runtime/Dockerfile.crew)" in text
        assert "MicroVM layer (runtime/Dockerfile.microvm)" in text
        assert "container.microvm.hooks" in text

    def test_the_crew_layers_base_indirection_is_gone(self):
        """``ARG BASE`` / ``FROM ${BASE}`` name a registry reference this lane does
        not have, so they are replaced by the base's own layers."""
        text = recipe_module.assemble_dockerfile("sha256:deadbeef")
        assert not re.search(r"(?m)^ARG\s+BASE\s*$", text)
        assert not re.search(r"(?m)^FROM\s+\$\{BASE\}\s*$", text)

    def test_the_base_name_label_names_the_recipe_digest(self):
        """An undeclared build arg expands to an empty string, and an empty
        ``base.name`` is the defect Dockerfile.crew's own comment records."""
        text = recipe_module.assemble_dockerfile("sha256:deadbeef")
        assert "${BASE}" not in text
        assert "sha256:deadbeef" in text

    def test_it_refuses_a_crew_layer_it_does_not_recognise(self, tmp_path, monkeypatch):
        """A silently mis-assembled recipe is a build that succeeds and produces
        the wrong image, so an unrecognised shape fails here instead."""
        fake = tmp_path / "Dockerfile.crew"
        fake.write_text("FROM scratch\nCOPY manifest.json /\n")
        monkeypatch.setattr(recipe_module, "CREW_DOCKERFILE", fake)
        with pytest.raises(RecipeRefused, match="no longer starts"):
            recipe_module.assemble_dockerfile("sha256:deadbeef")


class TestARedirectingEntryIsRefused:
    """A link in the bundle would travel the bytes it points at into the zip.

    ``zipfile.write`` FOLLOWS a link, so a bundle carrying
    ``agent.json -> ~/.aws/credentials`` would upload those bytes to S3 and bake
    them into an image. ``packaging.build``'s digest refuses to sign a redirect
    for the same reason, so refusing here is also what keeps the two in step: a
    redirect the digest will not cover must not be one this zip carries.
    """

    def test_a_symlinked_bundle_file_is_refused(self, bundle, wheel, tmp_path):
        secret = tmp_path / "credentials"
        secret.write_text("[default]\naws_access_key_id = AKIAEXAMPLE\n")
        (bundle / "agent.json").unlink()
        (bundle / "agent.json").symlink_to(secret)
        with pytest.raises(RecipeRefused, match="is a link or junction"):
            assemble(
                bundle_dir=bundle,
                wheel=wheel,
                base_image_arn=BASE,
                out_zip=tmp_path / "recipe.zip",
            )

    def test_a_symlink_deeper_in_the_bundle_is_refused(self, bundle, wheel, tmp_path):
        secret = tmp_path / "id_rsa"
        secret.write_text("PRIVATE KEY")
        (bundle / "skills" / "demo" / "stolen").symlink_to(secret)
        with pytest.raises(RecipeRefused, match="is a link or junction"):
            assemble(
                bundle_dir=bundle,
                wheel=wheel,
                base_image_arn=BASE,
                out_zip=tmp_path / "recipe.zip",
            )

    def test_nothing_is_written_when_a_link_is_found(self, bundle, wheel, tmp_path):
        (bundle / "linked").symlink_to(tmp_path / "anything")
        out_zip = tmp_path / "recipe.zip"
        with pytest.raises(RecipeRefused):
            assemble(
                bundle_dir=bundle,
                wheel=wheel,
                base_image_arn=BASE,
                out_zip=out_zip,
            )
        assert not out_zip.exists()

    def test_an_ordinary_bundle_still_assembles(self, bundle, wheel, tmp_path):
        """The refusal is of REDIRECTS, not of nested directories."""
        (bundle / "skills" / "demo" / "nested").mkdir()
        (bundle / "skills" / "demo" / "nested" / "more.md").write_text("# more\n")
        out = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "recipe.zip",
        )
        with zipfile.ZipFile(out.path) as archive:
            assert "skills/demo/nested/more.md" in archive.namelist()


class TestTheImageBakesTheStrictPosture:
    """Every crew route on this lane authenticates, and the IMAGE is what says so.

    A MicroVM carries its own internet-reachable HTTPS endpoint that no network
    connector closes, so the container is the only boundary. A value supplied at
    launch is one a caller can omit, and a posture that depends on every launch
    remembering to ask for it is not a posture. These tests read the ASSEMBLED
    recipe -- what Lambda actually builds -- rather than the layer file alone,
    because a line in a Dockerfile the concatenation drops is a line that ships
    nowhere.
    """

    def test_the_assembled_dockerfile_sets_the_flag(self, bundle, wheel, tmp_path):
        out = assemble(
            bundle_dir=bundle,
            wheel=wheel,
            base_image_arn=BASE,
            out_zip=tmp_path / "recipe.zip",
        )
        with zipfile.ZipFile(out.path) as archive:
            built = archive.read(DOCKERFILE_NAME).decode("utf-8")
        assert "SMC_REQUIRE_AUTH_ALL_ROUTES=1" in built

    def test_the_flag_is_an_ENV_not_a_build_arg(self):
        """``ARG`` is consumed at build time and is gone from the running VM;
        ``ENV`` is part of the image, so a VM started from it has the flag
        whatever the launch said."""
        text = recipe_module.MICROVM_DOCKERFILE.read_text(encoding="utf-8")
        env_block = text.split("ENV ", 1)[1].split("\n\n", 1)[0]
        assert "SMC_REQUIRE_AUTH_ALL_ROUTES=1" in env_block
        assert "ARG SMC_REQUIRE_AUTH_ALL_ROUTES" not in text

    def test_the_guest_reads_that_exact_name(self):
        """The image and the container must agree on the spelling, or the flag is
        set and nothing reads it."""
        config = (recipe_module.CONTAINER_DIR / "common" / "config.py").read_text(encoding="utf-8")
        assert '"SMC_REQUIRE_AUTH_ALL_ROUTES"' in config

    def test_the_fargate_layers_do_not_set_it(self):
        """Fargate's default is unchanged: that lane's flag stays absent, which is
        what its private subnet and zero-ingress security group already assert."""
        for recipe in (recipe_module.BASE_DOCKERFILE, recipe_module.CREW_DOCKERFILE):
            assert "SMC_REQUIRE_AUTH_ALL_ROUTES" not in recipe.read_text(encoding="utf-8")


class TestTheRunAsUserIsOneValue:
    """Registration and image must name the same POSIX user.

    ``cloud/ssm.py`` wraps every in-guest command as ``sudo -u <run_as> -i``, so a
    user the guest does not have makes the command exit with ``sudo: unknown
    user``. The first such command is the dashboard's token mint, and a failed
    mint takes the tunnel with it -- leaving a crew that is online, registered and
    unreachable, with nothing in the control plane saying why.

    Read from the files rather than restated, so a rename in any one of the three
    places fails here instead of on a live VM.
    """

    def test_the_image_runs_the_crew_as_that_user(self):
        """The base recipe's own ``USER``, which the supervisor and its children
        inherit."""
        text = recipe_module.BASE_DOCKERFILE.read_text(encoding="utf-8")
        users = [
            line.split(None, 1)[1].strip() for line in text.splitlines() if line.startswith("USER ")
        ]
        assert users, "the base recipe declares no USER"
        assert users[-1] == recipe_module.GUEST_RUN_AS

    def test_the_guest_wraps_its_own_commands_in_that_user(self):
        """``hooks.py`` spawns the supervisor as this user, so the processes that
        exist in the VM are its processes."""
        hooks = (recipe_module.CONTAINER_DIR / "microvm" / "hooks.py").read_text(encoding="utf-8")
        assert f'CREW_USER = "{recipe_module.GUEST_RUN_AS}"' in hooks

    def test_the_registration_names_that_user(self):
        """And not the registry's EC2 default, which is a user no MicroVM has."""
        engine = (Path(recipe_module.__file__).resolve().parent / "engine.py").read_text(
            encoding="utf-8"
        )
        assert "ssm_run_as=recipe_mod.GUEST_RUN_AS" in engine

    def test_it_is_not_the_registrys_ec2_default(self):
        from kiro_crew.instances.registry import _DEFAULT_SSM_RUN_AS

        assert recipe_module.GUEST_RUN_AS != _DEFAULT_SSM_RUN_AS, (
            "this lane's guest has no ec2-user, so taking the registry default is "
            "what makes every in-guest command fail"
        )

    def test_the_value_is_a_usable_unix_name(self):
        """The registry validates it, so a value this lane picked must pass."""
        from kiro_crew.instances.registry import _SSM_RUN_AS_RE

        assert _SSM_RUN_AS_RE.match(recipe_module.GUEST_RUN_AS)


class TestTheFrontPortIsOneValue:
    """The image decides where the front listens; the registration must not guess.

    A forward pointed at the registry's dashboard default comes UP and reaches a
    port nobody listens on, so the failure reads as a crew that will not answer
    rather than as a misdirected forward.
    """

    def test_the_front_port_matches_the_image(self):
        env_line = recipe_module.MICROVM_DOCKERFILE.read_text(encoding="utf-8")
        assert f"SMC_FRONT_PORT={recipe_module.GUEST_FRONT_PORT}" in env_line

    def test_it_is_not_the_hook_port(self):
        """The platform's hook listener owns that port, and the two cannot share
        one."""
        text = recipe_module.MICROVM_DOCKERFILE.read_text(encoding="utf-8")
        assert f"SMC_HOOK_PORT={recipe_module.GUEST_FRONT_PORT}" not in text

    def test_the_registration_targets_it(self):
        engine = (Path(recipe_module.__file__).resolve().parent / "engine.py").read_text(
            encoding="utf-8"
        )
        assert "remote_port=recipe_mod.GUEST_FRONT_PORT" in engine


class TestTheStagedWheelCarriesNoDashboard:
    """The dashboard SPA must not reach the recipe, and the lane must say so if it does.

    Not a size optimisation. Measured on one build, the assets are 53.26 MB of an
    89.93 MB wheel against a 64 MiB ceiling, so a checkout that has ever built the
    dashboard -- which ``kirocrew pod provision`` does -- cannot build an image at
    all. The crew here serves one turn route and a liveness answer and starts no page
    server, so none of it is reachable in the guest.

    The ceiling is monkeypatched small in the end-to-end case below. The property is
    "removing these assets is what brings the zip under the ceiling", and it holds at
    any scale; asserting it at the real one would mean building a real 90 MB wheel in
    a unit test.
    """

    def _wheel_with_dashboard(self, tmp_path: Path, asset_bytes: int) -> Path:
        return _write_wheel(
            tmp_path / "kiro_crew-0.0.0-py3-none-any.whl",
            {
                "kiro_crew/__init__.py": b"# serving code\n",
                "kiro_crew/static/dist/index.html": b"<html></html>",
                "kiro_crew/static/dist/vendor/big.js": b"x" * asset_bytes,
            },
        )

    def test_the_members_are_found_by_their_own_prefix(self, tmp_path):
        staged = self._wheel_with_dashboard(tmp_path, 32)
        assert recipe_module.dashboard_members(staged) == [
            "kiro_crew/static/dist/index.html",
            "kiro_crew/static/dist/vendor/big.js",
        ]

    def test_a_wheel_without_them_reports_none(self, wheel):
        """Non-vacuity: the finder must distinguish, or stripping proves nothing."""
        assert recipe_module.dashboard_members(wheel) == []

    def test_stripping_removes_exactly_the_dashboard(self, tmp_path):
        staged = self._wheel_with_dashboard(tmp_path, 4096)
        freed = recipe_module.strip_dashboard(staged)

        assert freed > 0, "nothing was removed, so the staged wheel is unchanged"
        assert recipe_module.dashboard_members(staged) == []
        with zipfile.ZipFile(staged) as archive:
            assert archive.read("kiro_crew/__init__.py") == b"# serving code\n"
            assert "kiro_crew-0.0.0.dist-info/RECORD" in archive.namelist()

    def test_the_record_drops_the_rows_for_what_it_removed(self, tmp_path):
        """A ``RECORD`` keeping rows for absent files describes a distribution that
        does not match itself, which is what ``pip`` reads when it uninstalls."""
        staged = self._wheel_with_dashboard(tmp_path, 32)
        recipe_module.strip_dashboard(staged)
        with zipfile.ZipFile(staged) as archive:
            record = archive.read("kiro_crew-0.0.0.dist-info/RECORD").decode("utf-8")

        assert "kiro_crew/static/" not in record
        assert "kiro_crew/__init__.py" in record, "stripping emptied the RECORD instead"

    def test_stripping_is_byte_stable(self, tmp_path):
        """``base_recipe_digest`` hashes this file, so a rewrite that varied run to
        run would miss the image cache on every build and pay for a rebuild."""
        left, right = tmp_path / "a", tmp_path / "b"
        left.mkdir()
        right.mkdir()
        first = self._wheel_with_dashboard(left, 128)
        second = self._wheel_with_dashboard(right, 128)
        recipe_module.strip_dashboard(first)
        recipe_module.strip_dashboard(second)

        assert first.read_bytes() == second.read_bytes()

        # And the reason they match: each kept member carries its OWN timestamp
        # across. Two strips of identical input would also agree if the rewrite
        # re-dated everything to now, so equality alone does not pin preservation.
        with zipfile.ZipFile(first) as archive:
            assert archive.getinfo("kiro_crew/__init__.py").date_time == _FIXTURE_STAMP

    def test_stripping_a_clean_wheel_changes_nothing(self, wheel):
        before = wheel.read_bytes()
        assert recipe_module.strip_dashboard(wheel) == 0
        assert wheel.read_bytes() == before

    def test_assemble_refuses_a_wheel_that_still_carries_them(self, bundle, tmp_path):
        """And the refusal names the cause. The size check alone would report a byte
        count, which is what sent this defect to a live take to be found."""
        staged = self._wheel_with_dashboard(tmp_path, 64)
        with pytest.raises(recipe_module.RecipeRefused) as refusal:
            assemble(
                bundle_dir=bundle,
                wheel=staged,
                base_image_arn=BASE,
                out_zip=tmp_path / "recipe.zip",
            )
        message = str(refusal.value)
        assert "kiro_crew/static/" in message
        assert "build_microvm_image_zip.py" in message

    def test_assemble_refuses_a_wheel_that_is_not_a_zip(self, bundle, tmp_path):
        staged = tmp_path / "kiro_crew-0.0.0-py3-none-any.whl"
        staged.write_bytes(b"PK\x03\x04 not really a wheel")
        with pytest.raises(recipe_module.RecipeRefused, match="not a readable zip"):
            assemble(
                bundle_dir=bundle,
                wheel=staged,
                base_image_arn=BASE,
                out_zip=tmp_path / "recipe.zip",
            )

    def test_stripping_is_what_brings_the_zip_under_the_ceiling(
        self, bundle, tmp_path, monkeypatch
    ):
        """The end-to-end property, at a scale a unit test can hold.

        The same wheel is refused for its size with the dashboard in it and assembles
        without. Run with the real prefix and the real assembler; only the ceiling is
        scaled, so the arithmetic under test is the shipped arithmetic.
        """
        # Above the recipe's own baseline -- the Dockerfiles, the container package
        # and the bundle are a couple of hundred kilobytes before any wheel -- and
        # below that baseline plus the assets added next.
        monkeypatch.setattr(recipe_module, "MAX_RECIPE_BYTES", 1024 * 1024)
        staged = self._wheel_with_dashboard(tmp_path, 32)

        # Incompressible and STORED, so the ceiling is measured against real bytes
        # rather than against how well a run of one character deflates. Seeded, so
        # the same bytes are drawn on every run and a failure here can be replayed.
        noise = random.Random(20261008).randbytes(2 * 1024 * 1024)
        with zipfile.ZipFile(staged, "a", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("kiro_crew/static/dist/vendor/noise.js", noise)

        # The prefix refusal stood down for this half, so the SIZE check is what
        # answers and the assertion is about bytes rather than about member names.
        with monkeypatch.context() as unchecked:
            unchecked.setattr(recipe_module, "dashboard_members", lambda _w: [])
            with pytest.raises(recipe_module.RecipeRefused, match="over the"):
                assemble(
                    bundle_dir=bundle,
                    wheel=staged,
                    base_image_arn=BASE,
                    out_zip=tmp_path / "over.zip",
                )

        recipe_module.strip_dashboard(staged)
        out = assemble(
            bundle_dir=bundle,
            wheel=staged,
            base_image_arn=BASE,
            out_zip=tmp_path / "under.zip",
        )
        assert out.size_bytes < recipe_module.MAX_RECIPE_BYTES
