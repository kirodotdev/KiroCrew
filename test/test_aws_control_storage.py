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
        # A missing object heads with rc!=0; presign relies on this so a typo'd
        # key can't mint a working-looking URL that 404s for the recipient.
        with mock.patch.object(engine, "run_aws", return_value=(255, "", "Not Found")):
            assert (
                storage.object_exists("p", "r", "b", "drive", "gone.txt", account="111122223333")
                is False
            )


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
