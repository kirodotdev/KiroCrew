"""AWS Control storage engine — the security-sensitive S3 layer.

This file pins the BEHAVIOUR CONTRACTS the docstring in ``storage.py`` calls
load-bearing: bucket creation and hardening ORDER, key-validation rejections,
presign expiry clamping, section prefixing, and every branch that REFUSES.
It intentionally leans on the same conventions as ``test_aws_control_app.py``:
patch ``storage._checked`` / ``storage.engine.run_aws``, assert on the argv
handed to the AWS CLI, and build tag-discovery JSON payloads by hand. Comments
explain WHY a case matters, not what the code does.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew.apps.builtins.aws_control.backend import storage
from kiro_crew.deploy import engine
from kiro_crew.deploy.engine import AWSError

# ---------------------------------------------------------------------------
# Section prefixing — a raw prefix must never cross the HTTP boundary, so the
# section->prefix mapping is the only place a caller-supplied section becomes a
# key prefix. section_key() is what every object-I/O path funnels through.
# ---------------------------------------------------------------------------


class TestSectionKey:
    def test_section_key_prepends_the_sections_prefix(self):
        # The API concept is the SECTION name; the raw prefix is internal.
        assert storage.section_key("library", "a.txt") == "artifacts/a.txt"
        assert storage.section_key("drive", "a.txt") == "drive/a.txt"
        assert storage.section_key("backup", "x/y.tar.gz") == "backup/x/y.tar.gz"

    def test_section_key_rejects_an_unknown_section(self):
        # An unmapped section is a KeyError, not a silently-empty prefix that
        # would land objects at the bucket root outside any section.
        with pytest.raises(KeyError):
            storage.section_key("nope", "a.txt")


class TestNewBucketName:
    def test_generated_name_matches_the_discovery_scheme(self):
        # Discovery requires a FULL prefix+12-hex match, so the name minter and
        # the discovery regex must agree or a freshly-created drive is invisible.
        name = storage.new_bucket_name()
        assert name.startswith(storage.BUCKET_PREFIX)
        assert storage._BUCKET_NAME_RE.fullmatch(name), name


# ---------------------------------------------------------------------------
# Discovery — the trust decision. These pin the branches find_drive() takes
# on empty results, malformed JSON, and a single clean hit.
# ---------------------------------------------------------------------------


class TestFindDrive:
    def test_no_matches_returns_none(self):
        empty = json.dumps({"ResourceTagMappingList": []})
        with mock.patch.object(storage, "_checked", return_value=empty):
            assert storage.find_drive("p", "us-east-1", account="111122223333") is None

    def test_single_match_is_returned(self):
        name = "kirocrew-drive-0123456789ab"
        payload = json.dumps({"ResourceTagMappingList": [{"ResourceARN": f"arn:aws:s3:::{name}"}]})
        with (
            mock.patch.object(storage, "_checked", return_value=payload),
            # head-bucket confirming the owner: discovery now asks S3 whose bucket
            # this is before handing the name back.
            mock.patch.object(storage.engine, "run_aws", return_value=(0, "", "")),
        ):
            assert storage.find_drive("p", "us-east-1", account="111122223333") == name

    def test_a_bucket_owned_by_another_account_is_refused(self):
        # The tags say WHICH bucket; only S3 says WHOSE it is. A profile repointed
        # from A to B discovers B's tagged bucket, and without this a request for
        # /drive/A would read and write B's drive with no consent from B's owner.
        name = "kirocrew-drive-0123456789ab"
        payload = json.dumps({"ResourceTagMappingList": [{"ResourceARN": f"arn:aws:s3:::{name}"}]})
        with (
            mock.patch.object(storage, "_checked", return_value=payload),
            mock.patch.object(
                storage.engine, "run_aws", return_value=(1, "", "An error occurred (403)")
            ),
        ):
            with pytest.raises(storage.AWSError) as exc:
                storage.find_drive("p", "us-east-1", account="111122223333")
        assert "111122223333" in str(exc.value)

    def test_the_owner_probe_carries_the_verified_account(self):
        name = "kirocrew-drive-0123456789ab"
        payload = json.dumps({"ResourceTagMappingList": [{"ResourceARN": f"arn:aws:s3:::{name}"}]})
        with (
            mock.patch.object(storage, "_checked", return_value=payload),
            mock.patch.object(storage.engine, "run_aws", return_value=(0, "", "")) as probe,
        ):
            storage.find_drive("p", "us-east-1", account="111122223333")
        argv = probe.call_args.args[0]
        assert argv[:2] == ["s3api", "head-bucket"]
        assert argv[argv.index("--expected-bucket-owner") + 1] == "111122223333"

    def test_the_refusal_redacts_stderr_before_truncating_it(self):
        # This defect has now been written twice: a raw `[:200]` slice cuts the
        # text FIRST, and a credential straddling the cut becomes a fragment that
        # matches no redactor pattern downstream -- so it travels into the
        # response and the audit log looking harmless. _trimmed_stderr redacts
        # first, which is the only order that works.
        name = "kirocrew-drive-0123456789ab"
        payload = json.dumps({"ResourceTagMappingList": [{"ResourceARN": f"arn:aws:s3:::{name}"}]})
        secret = "AKIAIOSFODNN7EXAMPLE"
        noise = "x" * 190
        with (
            mock.patch.object(storage, "_checked", return_value=payload),
            mock.patch.object(
                storage.engine, "run_aws", return_value=(1, "", f"{noise}{secret} denied")
            ),
        ):
            with pytest.raises(storage.AWSError) as exc:
                storage.find_drive("p", "us-east-1", account="111122223333")
        message = str(exc.value)
        # Neither the whole key nor the leading fragment a naive cut would leave.
        assert secret not in message
        assert secret[:12] not in message

    def test_malformed_json_reads_as_no_drive(self):
        # tag:GetResources returning garbage must degrade to "no drive", never
        # raise: a discovery crash on the read path would block the console.
        with mock.patch.object(storage, "_checked", return_value="{not json"):
            assert storage.find_drive("p", "us-east-1", account="111122223333") is None

    def test_empty_region_falls_back_to_the_engine_default(self):
        # The region flag is always sent; an empty region must resolve to the
        # engine default rather than an empty argv value.
        seen: dict[str, str] = {}

        def checked(args, profile, *, action, timeout=30):
            seen["region"] = args[args.index("--region") + 1]
            return json.dumps({"ResourceTagMappingList": []})

        with mock.patch.object(storage, "_checked", side_effect=checked):
            storage.find_drive("p", "", account="111122223333")
        assert seen["region"] == engine.DEFAULT_REGION

    def test_both_discovery_tags_are_anded_in_the_filter(self):
        # Discovery is a trust decision: BOTH kirocrew:managed=true AND
        # kirocrew:drive=default must be required, or a bucket carrying only one
        # tag could be adopted as the mutation target.
        seen: dict[str, list] = {}

        def checked(args, profile, *, action, timeout=30):
            seen["args"] = args
            return json.dumps({"ResourceTagMappingList": []})

        with mock.patch.object(storage, "_checked", side_effect=checked):
            storage.find_drive("p", "us-east-1", account="111122223333")
        joined = " ".join(seen["args"])
        assert f"Key={storage.TAG_DRIVE},Values={storage.DRIVE_ID}" in joined
        assert f"Key={engine.TAG_MANAGED},Values=true" in joined


# ---------------------------------------------------------------------------
# Creation — the hardening ORDER is the whole point. A discovered drive
# promises versioning + BPA + SSE, and the discovery TAGS are what make it
# discoverable, so everything must hold BEFORE the tags land.
# ---------------------------------------------------------------------------


class TestCreateDrive:
    def _run_create(self, region: str, owner_rc: int = 0, owner_err: str = ""):
        """Drive create_drive with instrumented _checked/_harden_bucket/run_aws
        and return the ordered list of (kind, args) the engine saw."""
        calls: list[tuple[str, object]] = []

        def checked(args, profile, *, action, timeout=30):
            calls.append(("checked", args))
            return ""

        def harden(bucket, profile, tagset):
            calls.append(("harden", (bucket, tagset)))

        def run_aws(args, profile, timeout=30):
            calls.append(("run_aws", args))
            return owner_rc, "", owner_err

        with (
            mock.patch.object(storage, "_checked", side_effect=checked),
            mock.patch.object(storage, "_harden_bucket", side_effect=harden),
            mock.patch.object(storage.engine, "run_aws", side_effect=run_aws),
        ):
            bucket = storage.create_drive("p", region, "123456789012")
        return bucket, calls

    def test_versioning_is_enabled_before_hardening_tags_land(self):
        # Order contract: create-bucket, then the ownership assertion, then
        # put-bucket-versioning, then _harden_bucket (which writes the discovery
        # tags LAST). A crash after tags but before versioning would leave a
        # discoverable drive that silently loses overwrite history — this pins
        # that it cannot happen.
        bucket, calls = self._run_create("us-west-2")
        kinds = [c[0] for c in calls]
        assert kinds == ["checked", "run_aws", "checked", "harden"]

        create_args = calls[0][1]
        assert create_args[:2] == ["s3api", "create-bucket"]
        assert create_args[create_args.index("--bucket") + 1] == bucket

        versioning_args = calls[2][1]
        assert versioning_args[:2] == ["s3api", "put-bucket-versioning"]
        assert "Status=Enabled" in versioning_args

        # The tags handed to hardening carry BOTH discovery tags — this is what
        # a later find_drive() will require.
        _bucket, tagset = calls[3][1]
        assert f"Key={engine.TAG_MANAGED},Value=true" in tagset
        assert f"Key={storage.TAG_DRIVE},Value={storage.DRIVE_ID}" in tagset

    def test_ownership_is_asserted_before_the_bucket_becomes_a_drive(self):
        # create-bucket runs in a fresh CLI process that resolves the profile
        # itself, so a matching triple in the caller cannot promise which account
        # the bucket landed in. The only way to know is to ask about the bucket.
        _bucket, calls = self._run_create("us-west-2")
        head = next(a for k, a in calls if k == "run_aws")
        assert head[:2] == ["s3api", "head-bucket"]
        assert head[head.index("--expected-bucket-owner") + 1] == "123456789012"
        # It must come before the tags that make the bucket discoverable.
        kinds = [c[0] for c in calls]
        assert kinds.index("run_aws") < kinds.index("harden")

    def test_a_bucket_in_an_unconfirmed_account_never_becomes_a_drive(self):
        # 403 from head-bucket means the bucket is not owned by the verified
        # account. Nothing may be tagged (tags are what discovery finds) and the
        # call must fail rather than hand back a drive.
        with pytest.raises(storage.AWSError) as exc:
            self._run_create("us-west-2", owner_rc=1, owner_err="An error occurred (403)")
        assert "123456789012" in str(exc.value)
        # The bucket name is surfaced so the owner can remove the orphan.
        assert "kirocrew-drive-" in str(exc.value)

    def test_an_ambiguous_ownership_answer_is_treated_as_a_mismatch(self):
        # A throttle leaves us unable to say which account this is; tagging it
        # anyway would turn "unknown" into "this is your drive".
        with pytest.raises(storage.AWSError):
            self._run_create("us-west-2", owner_rc=1, owner_err="Throttling: rate exceeded")

    def test_no_delete_is_issued_against_an_unidentified_account(self):
        # Deliberately non-destructive: a delete here would be a blind call into
        # an account we just failed to identify, and it is not needed — an
        # untagged bucket is not a drive and never receives an object.
        calls: list[tuple[str, object]] = []

        def checked(args, profile, *, action, timeout=30):
            calls.append(("checked", args))
            return ""

        with (
            mock.patch.object(storage, "_checked", side_effect=checked),
            mock.patch.object(storage, "_harden_bucket"),
            mock.patch.object(storage.engine, "run_aws", return_value=(1, "", "403")),
            pytest.raises(storage.AWSError),
        ):
            storage.create_drive("p", "us-west-2", "123456789012")
        assert not any("delete-bucket" in str(a) for _k, a in calls)

    def test_us_east_1_omits_the_location_constraint(self):
        # us-east-1 is the API's implicit home region; sending a
        # LocationConstraint for it is an error S3 rejects.
        _bucket, calls = self._run_create("us-east-1")
        create_args = calls[0][1]
        assert "--create-bucket-configuration" not in create_args

    def test_non_home_region_sends_a_location_constraint(self):
        _bucket, calls = self._run_create("eu-central-1")
        create_args = calls[0][1]
        assert "--create-bucket-configuration" in create_args
        assert "LocationConstraint=eu-central-1" in create_args


# ---------------------------------------------------------------------------
# Listing — one delimited page. The load-bearing behaviour is (a) the section
# prefix is STRIPPED off returned keys, (b) the folder placeholder is dropped,
# and (c) every name is run through the credential/exfiltration redactors
# because keys can be authored outside this app.
# ---------------------------------------------------------------------------


class TestListSection:
    def _list(self, payload: str, **kw):
        with mock.patch.object(storage, "_checked", return_value=payload) as checked:
            result = storage.list_section(
                "p", "us-east-1", "b", "drive", **kw, account="111122223333"
            )
        return result, checked

    def test_keys_and_folders_are_section_relative(self):
        # Callers speak in section-relative keys; the "drive/" prefix must be
        # stripped so it never leaks back across the API boundary.
        payload = json.dumps(
            {
                "Contents": [
                    {"Key": "drive/", "Size": 0},  # the folder placeholder
                    {"Key": "drive/a.txt", "Size": 12, "LastModified": "2026-01-01"},
                ],
                "CommonPrefixes": [{"Prefix": "drive/photos/"}],
                "NextToken": "tok",
            }
        )
        result, _ = self._list(payload)
        assert result["files"] == [{"key": "a.txt", "size": 12, "modified": "2026-01-01"}]
        assert result["folders"] == ["photos"]
        assert result["nextToken"] == "tok"

    def test_folder_placeholder_object_is_dropped(self):
        # An object whose key IS the prefix is the zero-byte folder marker, not
        # a file the user uploaded — it must not show up as a file row.
        payload = json.dumps({"Contents": [{"Key": "drive/", "Size": 0}]})
        result, _ = self._list(payload)
        assert result["files"] == []

    def test_subpath_is_appended_to_the_section_prefix(self):
        # Navigating into a folder narrows the LIST prefix; the argv must carry
        # "drive/photos/" so S3 only returns that folder's page.
        seen: dict[str, str] = {}

        def checked(args, profile, *, action, timeout=30):
            seen["prefix"] = args[args.index("--prefix") + 1]
            return "{}"

        with mock.patch.object(storage, "_checked", side_effect=checked):
            storage.list_section(
                "p", "us-east-1", "b", "drive", subpath="photos", account="111122223333"
            )
        assert seen["prefix"] == "drive/photos/"

    def test_starting_token_is_forwarded_only_when_present(self):
        # Pagination is opt-in: an empty token must not append an empty
        # --starting-token that the CLI would reject.
        with mock.patch.object(storage, "_checked", return_value="{}") as checked:
            storage.list_section("p", "us-east-1", "b", "drive", account="111122223333")
            no_token = checked.call_args.args[0]
            storage.list_section(
                "p", "us-east-1", "b", "drive", token="abc", account="111122223333"
            )
            with_token = checked.call_args.args[0]
        assert "--starting-token" not in no_token
        assert with_token[with_token.index("--starting-token") + 1] == "abc"

    def test_names_are_run_through_the_egress_redactors(self):
        # Keys can be authored by console uploads or other tools, so a name
        # embedding a credential must be redacted before it reaches the
        # dashboard — same double-pass discipline as every egress surface.
        payload = json.dumps(
            {
                "Contents": [
                    {
                        "Key": "drive/aws_secret_access_key=AKIAIOSFODNN7EXAMPLEKEY.txt",
                        "Size": 1,
                    }
                ],
                "CommonPrefixes": [],
            }
        )
        result, _ = self._list(payload)
        assert "AKIAIOSFODNN7EXAMPLEKEY" not in result["files"][0]["key"]

    def test_empty_body_reads_as_an_empty_page(self):
        # _checked returning "" (or None) must parse as an empty listing, not
        # crash the section view.
        result, _ = self._list("")
        assert result == {"files": [], "folders": [], "nextToken": ""}


# ---------------------------------------------------------------------------
# Object I/O — thin wrappers over the CLI. The contract worth pinning is the
# exact argv: the S3 URI is built from section_key(), and timeouts propagate.
# ---------------------------------------------------------------------------


class TestListLibraryFolders:
    """The IDENTITY read behind the Library reconcile.

    Distinct from list_section on three points a reconcile depends on, each
    pinned here: unredacted names, a complete answer rather than a page, and a
    RAISE rather than an empty list when the response cannot be read.
    """

    def test_returns_raw_folder_names_and_anchors_the_library_prefix(self):
        # CommonPrefixes carry the full key prefix; the caller compares these
        # against ledger keys, so the prefix and the trailing slash both have to
        # come off. The prefix is anchored INSIDE (there is no section argument),
        # so a caller cannot point this read at another section.
        payload = json.dumps(["artifacts/alpha/", "artifacts/beta-two/"])
        with mock.patch.object(storage, "_checked", return_value=payload) as checked:
            folders = storage.list_library_folders(
                "prof", "us-west-2", "bkt", account="111122223333"
            )
        assert folders == ["alpha", "beta-two"]
        argv = checked.call_args.args[0]
        # Delimiter + prefix make it a one-level listing, and the query pulls
        # CommonPrefixes so the CLI's auto-pagination merges every page into it.
        assert "--delimiter" in argv and argv[argv.index("--delimiter") + 1] == "/"
        assert argv[argv.index("--prefix") + 1] == "artifacts/"
        assert argv[argv.index("--query") + 1] == "CommonPrefixes[].Prefix"
        # Owner-pinned like every other call: a bucket name that changed hands
        # must not answer for this account.
        assert argv[argv.index("--expected-bucket-owner") + 1] == "111122223333"
        # NO --max-items: passing one turns the CLI to client-side pagination and
        # this answer would become a first page the caller would read as the
        # whole prefix.
        assert "--max-items" not in argv

    def test_an_empty_library_is_an_empty_list(self):
        # No CommonPrefixes at all: --query renders null, which must read as "no
        # folders" rather than raising.
        with mock.patch.object(storage, "_checked", return_value="null"):
            assert (
                storage.list_library_folders("prof", "us-west-2", "bkt", account="111122223333")
                == []
            )

    def test_foreign_and_bare_prefix_rows_are_dropped(self):
        # A row outside the prefix (another section leaking into the response)
        # and the prefix placeholder itself carry no folder name; neither may
        # become an empty-string "folder" the caller then reasons about.
        payload = json.dumps(["drive/other/", "artifacts/", "artifacts//", "artifacts/real/"])
        with mock.patch.object(storage, "_checked", return_value=payload):
            assert storage.list_library_folders(
                "prof", "us-west-2", "bkt", account="111122223333"
            ) == ["real"]

    def test_unreadable_response_raises_instead_of_reading_as_empty(self):
        # THE case this function exists for. An empty answer means "nothing in
        # the cloud", and the reconcile acts on that by dropping every record it
        # holds -- so a garbled response must fail loudly, the opposite of
        # usage()'s deliberate degrade-to-empty.
        with mock.patch.object(storage, "_checked", return_value="{not json"):
            with pytest.raises(AWSError, match="could not be read as JSON"):
                storage.list_library_folders("prof", "us-west-2", "bkt", account="111122223333")


class TestListObjectKeys:
    def test_lists_the_whole_drive_owner_pinned_and_unpaged(self):
        # No --prefix and no --delimiter: a share row can name any shareable
        # section, so one listing answers for all of them. --query pulls Keys,
        # which the CLI's auto-pagination merges across every page.
        payload = json.dumps(["drive/a.txt", "artifacts/slug/v1.html", "backup/x.tgz"])
        with mock.patch.object(storage, "_checked", return_value=payload) as checked:
            keys = storage.list_object_keys("prof", "us-west-2", "bkt", account="111122223333")
        assert keys == {"drive/a.txt", "artifacts/slug/v1.html", "backup/x.tgz"}
        argv = checked.call_args.args[0]
        assert argv[:2] == ["s3api", "list-objects-v2"]
        assert argv[argv.index("--query") + 1] == "Contents[].Key"
        assert "--delimiter" not in argv and "--prefix" not in argv
        # Owner-pinned like every other call: a bucket name that changed hands
        # must not answer for this account.
        assert argv[argv.index("--expected-bucket-owner") + 1] == "111122223333"
        # NO --max-items: passing one turns the CLI to client-side pagination and
        # the answer would become a first page read as the whole drive -- which
        # this caller concludes ABSENCE from.
        assert "--max-items" not in argv
        assert checked.call_args.kwargs["action"] == "s3:ListBucket"

    def test_an_empty_drive_is_an_empty_set(self):
        # No Contents at all: --query renders null, which must read as "no
        # objects" rather than raising.
        with mock.patch.object(storage, "_checked", return_value="null"):
            assert (
                storage.list_object_keys("prof", "us-west-2", "bkt", account="111122223333")
                == set()
            )

    def test_non_string_rows_are_dropped(self):
        # A row that is not a key cannot be membership-tested against one, and
        # must not enter the set as something a caller then compares.
        payload = json.dumps(["drive/a.txt", None, 7, {"Key": "drive/b.txt"}])
        with mock.patch.object(storage, "_checked", return_value=payload):
            assert storage.list_object_keys("prof", "us-west-2", "bkt", account="111122223333") == {
                "drive/a.txt"
            }

    def test_unreadable_response_raises_instead_of_reading_as_empty(self):
        # THE case this function exists for. An empty answer means "the drive
        # holds nothing", and the caller acts on that by marking every share it
        # holds as pointing at a deleted object -- so a garbled response must
        # fail loudly, the opposite of usage()'s deliberate degrade-to-empty.
        with mock.patch.object(storage, "_checked", return_value="{not json"):
            with pytest.raises(AWSError, match="could not be read as JSON"):
                storage.list_object_keys("prof", "us-west-2", "bkt", account="111122223333")


class TestObjectIO:
    def test_put_file_is_owner_pinned_and_section_scoped(self, tmp_path):
        # s3api put-object, NOT `s3 cp`: no `aws s3` command accepts
        # --expected-bucket-owner, and without it the transfer trusts only the
        # bucket NAME -- which is globally unique, so a freed name re-created in
        # another account (with a policy that allows the write) would receive the
        # owner's file.
        local = tmp_path / "a.txt"
        local.write_bytes(b"x")
        with mock.patch.object(storage, "_checked") as checked:
            storage.put_file(
                "p", "us-east-1", "b", "drive", "a.txt", str(local), account="111122223333"
            )
        args, kwargs = checked.call_args.args, checked.call_args.kwargs
        argv = args[0]
        assert argv[:2] == ["s3api", "put-object"]
        assert argv[argv.index("--bucket") + 1] == "b"
        assert argv[argv.index("--key") + 1] == "drive/a.txt"
        assert argv[argv.index("--body") + 1] == str(local)
        assert argv[argv.index("--expected-bucket-owner") + 1] == "111122223333"
        assert kwargs["action"] == "s3:PutObject"

    def test_put_file_forwards_a_custom_timeout(self, tmp_path):
        # Uploads can be large; the caller's timeout must reach the subprocess
        # chokepoint rather than a hardcoded default.
        local = tmp_path / "a.txt"
        local.write_bytes(b"x")
        with mock.patch.object(storage, "_checked") as checked:
            storage.put_file(
                "p", "r", "b", "drive", "a.txt", str(local), timeout=999, account="111122223333"
            )
        assert checked.call_args.kwargs["timeout"] == 999

    def test_put_file_refuses_a_body_too_large_to_pin(self, tmp_path, monkeypatch):
        # put-object is ONE request, so an oversized body cannot be sent this way.
        # The alternative would be `s3 cp`'s multipart, which cannot carry the
        # owner check -- so this refuses instead of transferring unpinned.
        local = tmp_path / "big.tar.gz"
        local.write_bytes(b"x")
        monkeypatch.setattr(storage.os.path, "getsize", lambda p: 6 * 1024 * 1024 * 1024)
        with mock.patch.object(storage, "_checked") as checked:
            with pytest.raises(storage.AWSError) as exc:
                storage.put_file(
                    "p", "r", "b", "backup", "k.tar.gz", str(local), account="111122223333"
                )
        assert "owner-pinned" in str(exc.value)
        checked.assert_not_called()

    def test_get_file_is_owner_pinned_and_section_scoped(self, tmp_path):
        with mock.patch.object(storage, "_checked") as checked:
            storage.get_file(
                "p", "us-east-1", "b", "library", "a.txt", "/tmp/out", account="111122223333"
            )
        argv = checked.call_args.args[0]
        assert argv[:2] == ["s3api", "get-object"]
        assert argv[argv.index("--bucket") + 1] == "b"
        assert argv[argv.index("--key") + 1] == "artifacts/a.txt"
        assert argv[argv.index("--expected-bucket-owner") + 1] == "111122223333"
        # The outfile is positional and must stay last, after every option.
        assert argv[-1] == "/tmp/out"
        assert checked.call_args.kwargs["action"] == "s3:GetObject"

    def test_delete_key_writes_a_delete_object_call(self):
        # On the versioned bucket this is a delete MARKER, so the argv must be a
        # plain delete-object (recoverable), not a version purge.
        with mock.patch.object(storage, "_checked") as checked:
            storage.delete_key("p", "us-east-1", "b", "drive", "a.txt", account="111122223333")
        argv = checked.call_args.args[0]
        assert argv[:2] == ["s3api", "delete-object"]
        assert argv[argv.index("--key") + 1] == "drive/a.txt"
        assert checked.call_args.kwargs["action"] == "s3:DeleteObject"


class TestCopyObject:
    def test_copy_object_is_owner_pinned_on_both_ends(self):
        # copy-object reads AND writes, so the name-reuse attack put_file's
        # docstring describes applies to both sides: the destination pin alone
        # would still let a re-created source bucket serve a stranger's bytes.
        with mock.patch.object(storage, "_checked") as checked:
            storage.copy_object(
                "p", "us-east-1", "b", "drive", "a.txt", "sub/b.txt", account="111122223333"
            )
        argv = checked.call_args.args[0]
        assert argv[:2] == ["s3api", "copy-object"]
        assert argv[argv.index("--bucket") + 1] == "b"
        assert argv[argv.index("--key") + 1] == "drive/sub/b.txt"
        assert argv[argv.index("--copy-source") + 1] == "b/drive/a.txt"
        assert argv[argv.index("--expected-bucket-owner") + 1] == "111122223333"
        assert argv[argv.index("--expected-source-bucket-owner") + 1] == "111122223333"
        assert checked.call_args.kwargs["action"] == "s3:PutObject"

    def test_copy_source_is_url_encoded_but_keeps_separators(self):
        # The copy source travels in an HTTP header, so a space in a valid key
        # must be percent-encoded — while '/' stays literal or the bucket/key
        # split inside the header breaks.
        with mock.patch.object(storage, "_checked") as checked:
            storage.copy_object(
                "p", "us-east-1", "b", "drive", "my file.txt", "b.txt", account="111122223333"
            )
        argv = checked.call_args.args[0]
        assert argv[argv.index("--copy-source") + 1] == "b/drive/my%20file.txt"


# ---------------------------------------------------------------------------
# Folder create — a folder exists ONLY as a zero-byte, '/'-terminated
# placeholder, and its key must be exactly the shape list_section() filters out
# of `files` (obj["Key"] == prefix). If the two drift, a created folder shows up
# as a phantom file.
# ---------------------------------------------------------------------------


class TestFolderPlaceholderKey:
    def test_placeholder_is_section_key_plus_trailing_slash(self):
        # The listing computes its page prefix as SECTION_PREFIXES[section] +
        # f"{subpath}/" and drops the object whose key EQUALS it. The placeholder
        # must be that exact string.
        assert storage.folder_placeholder_key("drive", "photos") == "drive/photos/"
        assert storage.folder_placeholder_key("library", "a/b") == "artifacts/a/b/"

    def test_placeholder_matches_what_the_listing_filters(self):
        # Round-trip the contract: create a folder "photos", then list its parent
        # and confirm the placeholder is filtered from files and never leaks in.
        # This is the anti-drift assertion — it fails if either side changes.
        section, path = "drive", "photos"
        placeholder = storage.folder_placeholder_key(section, path)
        # list_section under the parent (root) sees the placeholder as a
        # CommonPrefix folder, and the object whose Key == the child prefix is
        # dropped. Here we assert the created key equals the prefix the listing
        # of the FOLDER ITSELF would compute and drop.
        listing_prefix = storage.SECTION_PREFIXES[section] + f"{path}/"
        assert placeholder == listing_prefix


class TestCreateFolder:
    def test_create_folder_puts_the_zero_byte_placeholder(self):
        # A folder is a zero-byte object: put-object with NO --body, owner-pinned
        # like every write, keyed at section/path/ (trailing slash appended here,
        # never taken from the caller).
        with mock.patch.object(storage, "_checked") as checked:
            storage.create_folder("p", "us-east-1", "b", "drive", "photos", account="111122223333")
        argv = checked.call_args.args[0]
        assert argv[:2] == ["s3api", "put-object"]
        assert argv[argv.index("--bucket") + 1] == "b"
        assert argv[argv.index("--key") + 1] == "drive/photos/"
        assert argv[argv.index("--expected-bucket-owner") + 1] == "111122223333"
        # Zero-byte: no body flag at all.
        assert "--body" not in argv
        assert checked.call_args.kwargs["action"] == "s3:PutObject"


# ---------------------------------------------------------------------------
# Folder delete (delete_prefix) — the blast-radius surface. The contract:
# (a) anchor on section/path/ WITH the trailing slash so a name-prefixed sibling
# is not swept; (b) page the batch API, honouring the 1000-key cap rather than
# assuming one call clears the folder; (c) every call owner-pinned; (d) return
# the true count removed.
# ---------------------------------------------------------------------------


class TestDeletePrefix:
    def _run(self, pages: list[dict], delete_out: object = ""):
        """Drive delete_prefix with list-objects-v2 returning `pages` in order,
        capturing every argv the engine saw.

        `delete_out` is what each delete-objects call returns: a string, or a
        callable given the parsed payload so a test can fail specific keys the
        way S3 does - per-key, inside a 200 response.
        """
        calls: list[list] = []
        page_iter = iter(pages)

        def checked(args, profile, *, action, timeout=30):
            calls.append(args)
            if args[1] == "list-objects-v2":
                # The walk RE-LISTS after every round (no resume token), so it
                # asks once more than there are populated pages and stops on the
                # empty one. Supplying that terminator here keeps each test's
                # `pages` about the content it cares about.
                return json.dumps(next(page_iter, {"Contents": []}))
            if callable(delete_out):
                return delete_out(json.loads(args[args.index("--delete") + 1]))
            return delete_out

        with mock.patch.object(storage, "_checked", side_effect=checked):
            removed = storage.delete_prefix(
                "p", "us-east-1", "b", "drive", "photos", account="111122223333"
            )
        return removed, calls

    def test_per_key_failure_is_raised_not_counted_as_removed(self):
        # DeleteObjects reports per-key failures INSIDE a 200 response, so the
        # CLI exits 0 and _checked (which only raises on rc != 0) sees success.
        # Counting the batch as removed would tell the caller a folder is gone
        # while objects it could not touch are still there.
        def one_denied(_payload):
            return json.dumps(
                {"Errors": [{"Key": "drive/photos/b", "Code": "AccessDenied", "Message": "no"}]}
            )

        with pytest.raises(storage.AWSError) as excinfo:
            self._run(
                [{"Contents": [{"Key": "drive/photos/a"}, {"Key": "drive/photos/b"}]}],
                delete_out=one_denied,
            )
        assert "AccessDenied" in str(excinfo.value)

    def test_an_empty_errors_list_is_still_success(self):
        removed, _calls = self._run(
            [{"Contents": [{"Key": "drive/photos/a"}]}],
            delete_out=json.dumps({"Errors": []}),
        )
        assert removed == 1

    def test_an_empty_delete_response_is_success(self):
        # Quiet=True on a fully successful call returns an EMPTY body; that is
        # the common path and must not be read as a failure. (A non-empty body
        # that will not parse is a different case - see the test below.)
        removed, _calls = self._run([{"Contents": [{"Key": "drive/photos/a"}]}], delete_out="")
        assert removed == 1

    def test_delete_batches_stay_within_argv_length_limits(self):
        # The payload travels as ONE argv element. A page of 1000 long keys
        # serializes past the per-argument ceiling (~128 KiB on Linux) and past
        # Windows' whole-command-line limit, so the page has to be split by
        # SERIALIZED SIZE, not just by S3's key count.
        long_keys = [f"drive/photos/{i:04d}-{'k' * 200}" for i in range(1000)]
        removed, calls = self._run([{"Contents": [{"Key": k} for k in long_keys]}])

        deletes = [a for a in calls if a[1] == "delete-objects"]
        assert len(deletes) > 1, "a 1000-key page of long keys must be split"
        seen: list[str] = []
        for args in deletes:
            payload = args[args.index("--delete") + 1]
            assert len(payload.encode()) <= storage._DELETE_PAYLOAD_MAX_BYTES
            seen += [o["Key"] for o in json.loads(payload)["Objects"]]
        # Every key exactly once: a split must not drop or duplicate work.
        assert seen == long_keys
        assert removed == len(long_keys)

    def test_delete_forces_json_output_so_the_error_check_can_parse(self):
        # `_raise_on_delete_errors` reads the response as JSON. A user's
        # ~/.aws/config may set `output = text` (or yaml), which would make the
        # body unparseable and turn the per-key error check into a no-op - the
        # guard would be silently config-dependent. The listing call already
        # pins its own --output json; this one must too.
        _removed, calls = self._run([{"Contents": [{"Key": "drive/photos/a"}]}])
        delete = next(a for a in calls if a[1] == "delete-objects")
        assert delete[delete.index("--output") + 1] == "json"

    def test_an_unparseable_non_empty_delete_body_is_a_failure(self):
        # With --output json pinned, a non-empty body that is not JSON is not
        # something to shrug at on a destructive path: report failure rather
        # than claim the objects are gone. (Empty stays success - that is what
        # Quiet returns when everything worked.)
        with pytest.raises(storage.AWSError):
            self._run(
                [{"Contents": [{"Key": "drive/photos/a"}]}],
                delete_out="DELETE_MARKER\tdrive/photos/a\ttrue",
            )

    def test_the_payload_budget_survives_worst_case_windows_escaping(self):
        # The budget is a DERIVED number, so pin the derivation rather than the
        # literal: subprocess builds the Windows command line with list2cmdline,
        # which escapes every " as \", and an S3 key may legitimately contain
        # quotes. A batch that doubles must still fit CreateProcess's limit.
        worst_case = storage._DELETE_PAYLOAD_MAX_BYTES * 2
        fixed_argv = len(
            "s3api delete-objects --bucket bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb "
            "--delete --expected-bucket-owner 111122223333 "
        )
        assert worst_case + fixed_argv < storage._WINDOWS_CMDLINE_MAX

    def test_quote_dense_keys_still_produce_spawnable_batches(self):
        # A key made of quotes is the pathological case the budget exists for:
        # JSON-escaping doubles it once, Windows argv escaping can double it
        # again. Every batch must survive both and still cover each key once.
        quoted = [f"drive/photos/{i}-{chr(34) * 40}" for i in range(200)]
        removed, calls = self._run([{"Contents": [{"Key": k} for k in quoted]}])
        seen: list[str] = []
        for args in (a for a in calls if a[1] == "delete-objects"):
            payload = args[args.index("--delete") + 1]
            assert len(payload.encode()) <= storage._DELETE_PAYLOAD_MAX_BYTES
            # Model list2cmdline's quote escaping: the doubled form must fit.
            assert len(payload.replace('"', '\\"').encode()) < storage._WINDOWS_CMDLINE_MAX
            seen += [o["Key"] for o in json.loads(payload)["Objects"]]
        assert seen == quoted
        assert removed == len(quoted)

    def test_deletes_every_object_and_returns_the_count(self):
        removed, calls = self._run(
            [{"Contents": [{"Key": "drive/photos/a"}, {"Key": "drive/photos/b"}]}]
        )
        assert removed == 2
        delete = next(a for a in calls if a[1] == "delete-objects")
        assert delete[:2] == ["s3api", "delete-objects"]
        payload = json.loads(delete[delete.index("--delete") + 1])
        assert payload["Objects"] == [{"Key": "drive/photos/a"}, {"Key": "drive/photos/b"}]
        assert delete[delete.index("--expected-bucket-owner") + 1] == "111122223333"

    def test_list_is_anchored_on_the_trailing_slash_prefix(self):
        # Anchoring on "drive/photos/" (not "drive/photos") is what keeps a
        # sibling "drive/photos-backup/" out of the delete. Pin the LIST prefix.
        _removed, calls = self._run([{"Contents": [{"Key": "drive/photos/a"}]}])
        list_args = next(a for a in calls if a[1] == "list-objects-v2")
        assert list_args[list_args.index("--prefix") + 1] == "drive/photos/"
        assert list_args[list_args.index("--expected-bucket-owner") + 1] == "111122223333"

    def test_walks_a_multi_page_folder_by_RE_LISTING_not_by_resume_token(self):
        # A folder larger than one batch must still be fully deleted, and the walk
        # deliberately does NOT resume with --starting-token.
        #
        # `--max-items` is CLIENT-side pagination: when the CLI truncates inside a
        # server page it emits a COMPOSITE token carrying an intra-page offset
        # (boto_truncate_amount). S3 is free to return a short page, so the CLI
        # can fetch another to reach the requested count and truncate mid-page.
        # Resuming with that token re-lists and skips N items - but those N were
        # just DELETED, so the skip lands on surviving keys, which are then never
        # removed while the call reports completion.
        #
        # Re-listing from the prefix each round has no such offset to get wrong:
        # what was deleted is simply gone, so the next listing begins at the next
        # survivor. It is also memory-bounded, unlike collecting every key first.
        removed, calls = self._run(
            [
                {"Contents": [{"Key": "drive/photos/a"}], "NextToken": "T1"},
                {"Contents": [{"Key": "drive/photos/b"}]},
                {"Contents": []},
            ]
        )
        assert removed == 2
        lists = [a for a in calls if a[1] == "list-objects-v2"]
        deletes = [a for a in calls if a[1] == "delete-objects"]
        assert len(deletes) == 2
        # Every listing starts from the prefix: no resume token is ever sent,
        # even though the first page offered one.
        for args in lists:
            assert "--starting-token" not in args

    def test_stops_when_a_listing_stops_shrinking(self):
        # Termination safety: the walk relies on each round removing what it
        # listed. If a listing keeps returning the same keys (a delete that
        # reported success without removing anything), spinning forever would be
        # worse than failing, so the walk refuses to repeat a round that made no
        # progress.
        same = {"Contents": [{"Key": "drive/photos/a"}]}
        calls: list[list] = []

        def checked(args, profile, *, action, timeout=30):
            calls.append(args)
            if args[1] == "list-objects-v2":
                return json.dumps(same)
            return ""

        with mock.patch.object(storage, "_checked", side_effect=checked):
            with pytest.raises(storage.AWSError):
                storage.delete_prefix(
                    "p", "us-east-1", "b", "drive", "photos", account="111122223333"
                )
        # It gave up rather than looping: a bounded number of rounds.
        assert len([a for a in calls if a[1] == "list-objects-v2"]) < 10

    def test_batch_size_is_capped_at_the_api_limit(self):
        # The listing window (--max-items) must be the API's own per-request cap,
        # so a single delete-objects never exceeds what S3 accepts.
        _removed, calls = self._run([{"Contents": [{"Key": "drive/photos/a"}]}])
        list_args = next(a for a in calls if a[1] == "list-objects-v2")
        assert list_args[list_args.index("--max-items") + 1] == str(storage._DELETE_BATCH_MAX)
        assert storage._DELETE_BATCH_MAX == 1000

    def test_an_empty_folder_issues_no_delete_call(self):
        # A prefix with no objects (already empty, or only the placeholder just
        # removed) must not send an empty delete-objects, which the CLI rejects.
        removed, calls = self._run([{"Contents": []}])
        assert removed == 0
        assert not any(a[1] == "delete-objects" for a in calls)

    def test_malformed_list_body_stops_cleanly(self):
        # A garbled listing page must not crash mid-delete; it reads as an empty
        # page and the walk terminates.
        with mock.patch.object(storage, "_checked", return_value="{not json"):
            removed = storage.delete_prefix(
                "p", "us-east-1", "b", "drive", "photos", account="111122223333"
            )
        assert removed == 0


class TestObjectExists:
    def test_head_object_success_means_exists(self):
        # object_exists uses run_aws directly (not _checked) so a 404 head is a
        # normal False, never an exception the caller must catch.
        with mock.patch.object(engine, "run_aws", return_value=(0, "", "")) as run:
            assert (
                storage.object_exists("p", "r", "b", "drive", "a.txt", account="111122223333")
                is True
            )
        argv = run.call_args.args[0]
        assert argv[:2] == ["s3api", "head-object"]
        assert argv[argv.index("--key") + 1] == "drive/a.txt"

    def test_nonzero_return_means_missing(self):
        # A missing object heads with rc!=0 naming 404; presign relies on this
        # so a typo'd key can't mint a working-looking URL that 404s for the
        # recipient.
        with mock.patch.object(engine, "run_aws", return_value=(255, "", "Not Found")):
            assert (
                storage.object_exists("p", "r", "b", "drive", "gone.txt", account="111122223333")
                is False
            )

    def test_404_stderr_means_missing(self):
        err = "An error occurred (404) when calling the HeadObject operation: Not Found"
        with mock.patch.object(engine, "run_aws", return_value=(254, "", err)):
            assert (
                storage.object_exists("p", "r", "b", "drive", "gone.txt", account="111122223333")
                is False
            )

    def test_non_404_failure_raises_instead_of_reading_as_absent(self):
        # The move handler treats False on the DESTINATION probe as permission
        # to copy over that key. A throttle, timeout, or owner-pin 403 folded
        # into "absent" would turn one failed HEAD into an overwrite plus a
        # source delete — so anything S3 did not answer 404 to must RAISE.
        err = "An error occurred (403) when calling the HeadObject operation: Forbidden"
        with mock.patch.object(engine, "run_aws", return_value=(254, "", err)):
            with pytest.raises(AWSError):
                storage.object_exists("p", "r", "b", "drive", "a.txt", account="111122223333")

    def test_head_object_meta_returns_the_parsed_response(self):
        # The download path reads ContentType off this, so the HEAD asks for
        # JSON and the parsed dict comes back whole.
        out = '{"ContentType": "application/pdf", "ContentLength": 5}'
        with mock.patch.object(engine, "run_aws", return_value=(0, out, "")) as run:
            meta = storage.head_object_meta("p", "r", "b", "drive", "a.pdf", account="111122223333")
        assert meta == {"ContentType": "application/pdf", "ContentLength": 5}
        argv = run.call_args.args[0]
        assert argv[argv.index("--output") + 1] == "json"

    def test_head_object_meta_is_none_for_a_missing_key_and_empty_for_odd_output(self):
        with mock.patch.object(engine, "run_aws", return_value=(254, "", "(404) Not Found")):
            assert storage.head_object_meta("p", "r", "b", "drive", "a", account="1") is None
        # A present object with unparseable output still reads as PRESENT -- the
        # absent/raise contract is object_exists's, and it must not flip on a
        # formatting hiccup.
        with mock.patch.object(engine, "run_aws", return_value=(0, "not json", "")):
            assert storage.head_object_meta("p", "r", "b", "drive", "a", account="1") == {}


# ---------------------------------------------------------------------------
# Presign — a bearer URL. Clamp both ends of the expiry, and REFUSE any output
# that is not an https URL rather than handing back a broken share.
# ---------------------------------------------------------------------------


class TestPresign:
    def _presign(self, expires_secs, out="https://example.com/signed\n"):
        seen: dict[str, str] = {}

        def checked(args, profile, *, action, timeout=30):
            seen["expires"] = args[args.index("--expires-in") + 1]
            seen["uri"] = args[2]
            seen["region"] = args[args.index("--region") + 1]
            return out

        with mock.patch.object(storage, "_checked", side_effect=checked):
            url = storage.presign("p", "us-east-1", "b", "drive", "k.txt", expires_secs)
        return url, seen

    def test_expiry_is_clamped_to_the_sigv4_ceiling(self):
        _url, seen = self._presign(10**9)
        assert seen["expires"] == str(storage.PRESIGN_MAX_SECS)

    def test_expiry_floor_is_sixty_seconds(self):
        _url, seen = self._presign(1)
        assert seen["expires"] == "60"

    def test_a_value_inside_the_window_is_left_untouched(self):
        _url, seen = self._presign(3600)
        assert seen["expires"] == "3600"

    def test_the_uri_is_section_scoped(self):
        _url, seen = self._presign(3600)
        assert seen["uri"] == "s3://b/drive/k.txt"

    def test_empty_region_falls_back_to_the_engine_default(self):
        seen: dict[str, str] = {}

        def checked(args, profile, *, action, timeout=30):
            seen["region"] = args[args.index("--region") + 1]
            return "https://example.com/x"

        with mock.patch.object(storage, "_checked", side_effect=checked):
            storage.presign("p", "", "b", "drive", "k.txt", 3600)
        assert seen["region"] == engine.DEFAULT_REGION

    def test_non_https_output_is_refused(self):
        # A CLI that prints anything but an https URL (empty, an error line) must
        # raise — never hand a caller a "share URL" that isn't one.
        with pytest.raises(AWSError, match="no URL"):
            self._presign(3600, out="not-a-url\n")

    def test_empty_output_is_refused(self):
        with pytest.raises(AWSError, match="no URL"):
            self._presign(3600, out="")


# ---------------------------------------------------------------------------
# Usage — objects + bytes per section, folded from a full-bucket listing. The
# contract: attribute each key to exactly ONE section by prefix, tolerate
# malformed rows, and sum honestly.
# ---------------------------------------------------------------------------


class TestUsage:
    def _usage(self, out: str):
        with mock.patch.object(storage, "_checked", return_value=out):
            return storage.usage("p", "us-east-1", "b", account="111122223333")

    def test_objects_are_attributed_to_their_section(self):
        rows = json.dumps(
            [
                {"Key": "drive/a.txt", "Size": 10},
                {"Key": "drive/b.txt", "Size": 5},
                {"Key": "artifacts/c.bin", "Size": 100},
                {"Key": "backup/snap.tar.gz", "Size": 1000},
            ]
        )
        result = self._usage(rows)
        assert result["sections"]["drive"] == {"objects": 2, "bytes": 15}
        assert result["sections"]["library"] == {"objects": 1, "bytes": 100}
        assert result["sections"]["backup"] == {"objects": 1, "bytes": 1000}
        assert result["objects"] == 4
        assert result["bytes"] == 1115

    def test_a_key_outside_every_section_is_ignored(self):
        # A stray object at the bucket root belongs to no section and must not
        # inflate any section's totals (the loop breaks on first prefix match).
        rows = json.dumps([{"Key": "loose.txt", "Size": 42}, {"Key": "drive/x", "Size": 1}])
        result = self._usage(rows)
        assert result["objects"] == 1
        assert result["bytes"] == 1
        assert all(s["objects"] == 0 for name, s in result["sections"].items() if name != "drive")

    def test_malformed_json_reads_as_zero_usage(self):
        # A garbled --query result must yield an all-zero report, never a 500 on
        # the read-only usage panel.
        result = self._usage("{not json")
        assert result["objects"] == 0
        assert result["bytes"] == 0
        assert result["sections"]["drive"] == {"objects": 0, "bytes": 0}

    def test_empty_body_reads_as_zero_usage(self):
        result = self._usage("")
        assert result["objects"] == 0
        assert result["bytes"] == 0

    def test_a_null_size_counts_the_object_but_no_bytes(self):
        # S3 can return a null Size on odd rows; it must count as an object with
        # zero bytes, not raise on int(None).
        rows = json.dumps([{"Key": "drive/a", "Size": None}])
        result = self._usage(rows)
        assert result["sections"]["drive"] == {"objects": 1, "bytes": 0}


# ---------------------------------------------------------------------------
# Upload Content-Type — inline preview rendering depends on the stored header,
# so the guess must land on the argv when the extension is known and must NOT
# override S3's default when it is not.
# ---------------------------------------------------------------------------


class TestPutFileContentType:
    def _argv(self, key: str, tmp_path):
        local = tmp_path / "body"
        local.write_bytes(b"x")
        with mock.patch.object(storage, "_checked") as checked:
            storage.put_file("p", "us-east-1", "b", "drive", key, str(local), account="1")
        return checked.call_args.args[0]

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("doc.pdf", "application/pdf"),
            ("pic.png", "image/png"),
            ("clip.mp4", "video/mp4"),
            ("track.mp3", "audio/mpeg"),
        ],
    )
    def test_known_extensions_pin_the_inline_renderable_type(self, key, expected, tmp_path):
        # Without this S3 stores binary/octet-stream and a presigned URL forces
        # a download; the browser only renders inline what the header names.
        argv = self._argv(key, tmp_path)
        assert argv[argv.index("--content-type") + 1] == expected
        # The guess must never displace the owner pin.
        assert "--expected-bucket-owner" in argv

    def test_an_unknown_extension_keeps_the_s3_default(self, tmp_path):
        # Guessing wrong is worse than not guessing: S3's default at least
        # never lies about what the bytes are.
        argv = self._argv("mystery.zzzqqq", tmp_path)
        assert "--content-type" not in argv
        assert "--expected-bucket-owner" in argv

    @pytest.mark.parametrize("key", ["page.html", "page.htm", "logo.svg", "notes.txt", "app.js"])
    def test_types_a_browser_would_execute_or_render_as_a_document_stay_opaque(self, key, tmp_path):
        # A stored text/html or image/svg+xml makes a shared or downloaded
        # object render as a LIVE document on the bucket origin, script and all,
        # while the same file opened in-app is inert bytes in the text preview.
        # Text types are not needed for preview either (that read is proxied),
        # so only the media/PDF types the dialog embeds by URL are declared.
        argv = self._argv(key, tmp_path)
        assert "--content-type" not in argv


# ---------------------------------------------------------------------------
# Preview head bytes — the gateway-proxied read behind /drive/{account}/preview.
# The range bound, the owner pin, the full-size answer, and the temp-file
# cleanup are each load-bearing.
# ---------------------------------------------------------------------------


class TestGetObjectHeadBytes:
    def _run(self, body: bytes, meta: object, max_bytes: int = 1024, tmp_path=None, writer=None):
        captured: dict = {}

        def fake_checked(argv, profile, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            captured["tmp"] = argv[-1]
            # The gateway created the destination before the CLI ran, so the
            # CLI finds a file to write into rather than a name to claim.
            captured["preexisting"] = os.path.exists(argv[-1])
            if writer is not None:
                writer(argv[-1])
            else:
                with open(argv[-1], "wb") as fh:
                    fh.write(body)
            return meta if isinstance(meta, str) else json.dumps(meta)

        # The staging parent is the sandbox-hidden root under the REAL home;
        # a test never touches that, so the seam points at a temp dir.
        parent = tempfile.TemporaryDirectory() if tmp_path is None else None
        parent_dir = Path(parent.name) if parent else Path(tmp_path)
        captured["parent"] = parent_dir
        try:
            with (
                mock.patch.object(storage, "_checked", side_effect=fake_checked),
                mock.patch.object(storage, "_preview_staging_parent", return_value=parent_dir),
            ):
                data, size = storage.get_object_head_bytes(
                    "p",
                    "us-east-1",
                    "b",
                    "drive",
                    "a.txt",
                    account="111122223333",
                    max_bytes=max_bytes,
                )
        finally:
            if parent:
                parent.cleanup()
        return data, size, captured

    def test_the_transfer_is_range_bounded_and_owner_pinned(self):
        # A preview is a WINDOW, not a download: the range caps what one click
        # moves through the gateway, and the owner pin is put_file's name-reuse
        # defence applied to the read side.
        _data, _size, cap = self._run(b"head", {"ContentRange": "bytes 0-3/4"})
        argv = cap["argv"]
        assert argv[:2] == ["s3api", "get-object"]
        assert argv[argv.index("--range") + 1] == "bytes=0-1023"
        assert argv[argv.index("--expected-bucket-owner") + 1] == "111122223333"
        assert argv[argv.index("--key") + 1] == "drive/a.txt"
        # The size answer below is parsed from this response, so the format
        # cannot be left to the user's ~/.aws/config.
        assert argv[argv.index("--output") + 1] == "json"

    def test_the_full_size_comes_from_content_range(self):
        # ContentLength on a ranged GET is the WINDOW's length; only
        # ContentRange's total tells a truncated preview from a complete one.
        data, size, _cap = self._run(b"head", {"ContentRange": "bytes 0-3/100", "ContentLength": 4})
        assert (data, size) == (b"head", 100)

    def test_content_length_is_the_fallback_without_a_range_header(self):
        data, size, _cap = self._run(b"head", {"ContentLength": 4})
        assert (data, size) == (b"head", 4)

    def test_a_garbled_response_never_understates_the_size(self):
        # Reporting a size below the bytes in hand would read as "complete" on
        # a truncated preview, so the floor is what was actually read.
        data, size, _cap = self._run(b"abc", "not json at all")
        assert (data, size) == (b"abc", 3)

    def test_the_staging_temp_file_does_not_outlive_the_call(self):
        import os as _os

        _data, _size, cap = self._run(b"head", {"ContentRange": "bytes 0-3/4"})
        assert not _os.path.exists(cap["tmp"])
        # The whole per-call directory goes, not just the file.
        assert not _os.path.exists(_os.path.dirname(cap["tmp"]))

    def test_the_staging_file_lives_in_a_fresh_directory_under_the_hidden_root(self, tmp_path):
        # Never a bare path in the shared temp dir: a same-UID watcher there can
        # swap the file for a link before the CLI opens it, and the CLI would
        # write the object through the link. The file is cut inside a fresh
        # directory under the root every agent sandbox masks.
        _data, _size, cap = self._run(b"head", {"ContentRange": "bytes 0-3/4"}, tmp_path=tmp_path)
        staged = Path(cap["tmp"])
        assert staged.parent.parent == tmp_path
        assert staged.parent.name.startswith("drive-preview-")
        assert staged.name == "object"

    def test_the_cli_spawn_is_granted_exactly_its_own_staging_directory(self, tmp_path):
        # The mask that keeps the agent out would keep the sandboxed CLI out
        # too, so the per-call directory -- and only that directory -- is named
        # as visible for this one spawn.
        _data, _size, cap = self._run(b"head", {"ContentRange": "bytes 0-3/4"}, tmp_path=tmp_path)
        staged = Path(cap["tmp"])
        assert cap["kwargs"]["extra_visible_dirs"] == (str(staged.parent),)

    def test_the_destination_is_the_gateways_own_file_before_the_cli_runs(self, tmp_path):
        # Identity pinning for the host with no sandbox mask (Windows): the
        # gateway creates the destination exclusively and holds it open, so the
        # CLI writes into a file that already belongs to the gateway and cannot
        # be swapped underneath it. The fake CLI sees it there.
        data, _size, cap = self._run(b"head", {"ContentRange": "bytes 0-3/4"}, tmp_path=tmp_path)
        assert cap["preexisting"] is True
        assert data == b"head"

    def test_an_empty_object_reads_as_an_empty_preview_not_a_failure(self, tmp_path):
        # A bytes=0-N range against a 0-byte object is unsatisfiable and S3
        # answers 416 InvalidRange -- the file is readable and simply empty.
        def refuse_range(_path):
            raise AWSError(
                "s3:GetObject failed: An error occurred (InvalidRange) when calling the "
                "GetObject operation: The requested range is not satisfiable"
            )

        data, size, cap = self._run(b"", {}, tmp_path=tmp_path, writer=refuse_range)
        assert (data, size) == (b"", 0)
        # The staging directory is gone on this path too.
        assert not os.path.exists(os.path.dirname(cap["tmp"]))

    def test_other_cli_failures_still_raise(self, tmp_path):
        def deny(_path):
            raise AWSError("s3:GetObject failed: An error occurred (AccessDenied) ...")

        with pytest.raises(AWSError):
            self._run(b"", {}, tmp_path=tmp_path, writer=deny)

    def test_the_root_and_the_call_directory_are_pinned_while_the_cli_runs(self, tmp_path):
        # The file pin alone leaves the DIRECTORY swappable: rename it away and
        # plant a junction at its name before the create, and the CLI's path
        # resolves into the planted target. Both directories are therefore held
        # open -- root first, then the per-call directory -- for the whole call.
        from kiro_crew import platform_compat

        pinned: list[str] = []
        real_pin = platform_compat.pin_directory

        def spy(path):
            pinned.append(os.fspath(path))
            return real_pin(path)

        with mock.patch.object(storage.platform_compat, "pin_directory", side_effect=spy):
            _data, _size, cap = self._run(
                b"head", {"ContentRange": "bytes 0-3/4"}, tmp_path=tmp_path
            )
        staged = Path(cap["tmp"])
        assert pinned == [str(tmp_path), str(staged.parent)]

    def test_a_destination_replaced_during_the_transfer_is_refused(self, tmp_path):
        # A CLI (or anything racing it) that lands the bytes in a DIFFERENT
        # file under the same name is caught by the identity check -- the bytes
        # in hand would otherwise be read from whatever now sits at the path.
        # On Windows the held handle refuses the unlink itself (the pin), so
        # the swap never gets as far as the check.
        def swap(path):
            os.unlink(path)
            with open(path, "wb") as fh:
                fh.write(b"swapped")

        expected = ValueError if os.name == "posix" else PermissionError
        with pytest.raises(expected):
            self._run(b"head", {"ContentRange": "bytes 0-3/4"}, tmp_path=tmp_path, writer=swap)

    @pytest.mark.skipif(os.name != "posix", reason="hard links need a privilege on Windows CI")
    def test_a_destination_hard_linked_elsewhere_is_refused(self, tmp_path):
        # A second name on the staged file means something outside the fence
        # can read the object bytes after the staging directory is removed.
        def link_out(path):
            with open(path, "wb") as fh:
                fh.write(b"head")
            os.link(path, tmp_path / "leak")

        with pytest.raises(ValueError):
            self._run(b"head", {"ContentRange": "bytes 0-3/4"}, tmp_path=tmp_path, writer=link_out)

    def test_the_staging_root_is_fenced_on_both_the_tool_gate_and_the_sandbox(self):
        # Moving the staging root out of the fenced leaf would silently
        # re-open the link swap; the three spellings are pinned together.
        from kiro_crew import sandbox, security

        assert storage.STAGING_DIR_LEAF in security._CREW_SECRET_LEAVES
        assert storage.STAGING_DIR_LEAF in sandbox._CREW_HIDDEN_LEAVES
        # And created BEFORE any sandbox spawns: a mask binds over a name that
        # exists, so a root created lazily on first preview would be visible to
        # every sandbox already running.
        assert storage.STAGING_DIR_LEAF in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES

    def test_the_real_staging_root_sits_under_the_fenced_leaf(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "data_home", lambda: tmp_path)
        parent = storage._preview_staging_parent()
        assert parent == tmp_path / storage.STAGING_DIR_LEAF
        assert parent.is_dir()
        # A TOP-LEVEL leaf: its only ancestors are the data home and $HOME, so
        # there is no agent-writable directory between the fence and the root
        # that could be renamed out from under a transfer.
        assert "/" not in storage.STAGING_DIR_LEAF

    @pytest.mark.skipif(os.name != "posix", reason="symlinks need a privilege on Windows")
    def test_a_link_planted_at_the_staging_root_is_refused(self, tmp_path, monkeypatch):
        # A linked root puts every staged file outside the fence, which no
        # per-file check can see -- so the root itself is checked first.
        monkeypatch.setattr(storage, "data_home", lambda: tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, tmp_path / storage.STAGING_DIR_LEAF)
        with pytest.raises(ValueError):
            storage._preview_staging_parent()

    def test_run_aws_forwards_the_visible_dir_grant_to_the_sandbox(self, monkeypatch, tmp_path):
        seen: dict = {}

        def fake_wrap(argv, mode="auto", **kw):
            seen["mode"] = mode
            seen["kw"] = kw
            return list(argv), None

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(engine, "wrap_argv", fake_wrap)
        monkeypatch.setattr(engine, "cgroup_scope_argv", lambda argv: argv)
        monkeypatch.setattr(engine, "run_limited", lambda *a, **k: _Proc())
        engine.run_aws(["s3api", "get-object"], "p", timeout=5, extra_visible_dirs=(str(tmp_path),))
        assert seen["mode"] == "standard"
        assert seen["kw"]["extra_visible_dirs"] == (str(tmp_path),)

    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is POSIX-only")
    def test_a_link_planted_at_the_staging_path_is_refused_on_read(self, tmp_path):
        # Even if a link appeared anyway, the read-back must not follow it and
        # hand the link's target to the browser.
        target = tmp_path / "secret"
        target.write_bytes(b"not for the browser")

        def fake_checked(argv, profile, **kwargs):
            os.symlink(target, argv[-1])
            return json.dumps({"ContentRange": "bytes 0-3/4"})

        with (
            mock.patch.object(storage, "_checked", side_effect=fake_checked),
            mock.patch.object(storage, "_preview_staging_parent", return_value=tmp_path),
        ):
            with pytest.raises(OSError):
                storage.get_object_head_bytes(
                    "p", "us-east-1", "b", "drive", "a.txt", account="111122223333", max_bytes=1024
                )


# ---------------------------------------------------------------------------
# Filename search — bounded pagination is the contract: a broad query on a
# large drive must stop at the cap instead of listing the world.
# ---------------------------------------------------------------------------


def _search_page(keys: list[str], next_token: str = "") -> str:
    contents = [{"Key": k, "Size": 7, "LastModified": "2026-01-01T00:00:00+00:00"} for k in keys]
    page: dict = {"Contents": contents}
    if next_token:
        page["NextToken"] = next_token
    return json.dumps(page)


class TestSearchKeys:
    def _search(self, pages: list[str], query: str):
        with mock.patch.object(storage, "_checked", side_effect=pages) as checked:
            results, capped = storage.search_keys(
                "p", "us-east-1", "b", "drive", query, account="111122223333"
            )
        return results, capped, checked

    def test_matching_is_case_insensitive_over_the_whole_relative_key(self):
        # "reports/20" must find a file by its FOLDER, not just its basename —
        # the user searching a drive thinks in paths, not leaf names.
        pages = [_search_page(["drive/Reports/2026.TXT", "drive/other.txt"])]
        results, capped, _ = self._search(pages, "reports/20")
        assert [r["key"] for r in results] == ["Reports/2026.TXT"]
        assert results[0]["size"] == 7
        assert results[0]["modified"] == "2026-01-01T00:00:00+00:00"
        assert capped is False

    def test_folder_placeholders_are_not_files(self):
        # The zero-byte "photos/" marker matches the query textually but is
        # navigation structure; surfacing it would offer a preview of nothing.
        pages = [_search_page(["drive/photos/", "drive/photos/a.txt"])]
        results, _capped, _ = self._search(pages, "photos")
        assert [r["key"] for r in results] == ["photos/a.txt"]

    def test_the_walk_is_owner_pinned_and_section_prefixed(self):
        pages = [_search_page(["drive/a.txt"])]
        _results, _capped, checked = self._search(pages, "a")
        argv = checked.call_args.args[0]
        assert argv[argv.index("--prefix") + 1] == "drive/"
        assert argv[argv.index("--expected-bucket-owner") + 1] == "111122223333"

    def test_hitting_the_cap_stops_before_the_next_page(self):
        # The second page must never be REQUESTED once a match past the cap
        # is seen — that early exit is what bounds a broad query on a large
        # drive. The cap is a module constant (not a caller-tunable), so the
        # test lowers it in place rather than passing one.
        pages = [
            _search_page(
                ["drive/a-match.txt", "drive/b-match.txt", "drive/c-match.txt"],
                next_token="t2",
            ),
            _search_page(["drive/d-match.txt"]),
        ]
        with mock.patch.object(storage, "SEARCH_MAX_RESULTS", 2):
            results, capped, checked = self._search(pages, "match")
        assert [r["key"] for r in results] == ["a-match.txt", "b-match.txt"]
        assert capped is True
        assert checked.call_count == 1

    def test_exactly_the_cap_is_a_complete_result_not_a_truncated_one(self):
        # ``capped`` promises "there were more" — a drive holding exactly the
        # cap's worth of matches must not be told its results were cut off.
        pages = [_search_page(["drive/a-match.txt", "drive/b-match.txt"])]
        with mock.patch.object(storage, "SEARCH_MAX_RESULTS", 2):
            results, capped, _ = self._search(pages, "match")
        assert len(results) == 2
        assert capped is False

    def test_the_next_page_resumes_from_the_token(self):
        pages = [
            _search_page(["drive/miss.txt"], next_token="t2"),
            _search_page(["drive/hit.txt"]),
        ]
        results, capped, checked = self._search(pages, "hit")
        assert [r["key"] for r in results] == ["hit.txt"]
        assert capped is False
        second_argv = checked.call_args_list[1].args[0]
        assert second_argv[second_argv.index("--starting-token") + 1] == "t2"
