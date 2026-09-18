"""Process-record policy consumes the descriptor our real lockdown writer emits."""

from __future__ import annotations

import os

import pytest
from test_windows_acl import _OWNER_PTR, ADMINS, ME, SYSTEM, _build_acl, _FakeDlls

from kiro_crew import member_process_records as records
from kiro_crew import platform_compat as pc
from kiro_crew import windows_acl

OWNER_RIGHTS = "S-1-3-4"
OTHER = "S-1-5-21-9-9-9-1002"
TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"


class _RoundTripDlls(_FakeDlls):
    """Capture real writer calls and expose their ACE bytes to the real reader.

    Only Win32 is simulated. No host ACL is read or changed. In particular,
    setting DACL_SECURITY_INFORMATION does not change the object's owner.
    """

    def __init__(self, owner):
        super().__init__(sids={_OWNER_PTR: owner})
        self.pending = []
        self.sid_pointers = {}
        self.security_information = None

    def ConvertStringSidToSidW(self, sid, out_ref):
        pointer = 0x2000 + len(self.sid_pointers) * 16
        self.sid_pointers[pointer] = sid
        out_ref._obj.value = pointer
        return 1

    def GetLengthSid(self, _pointer):
        return 12

    def InitializeAcl(self, _pacl, _size, _revision):
        self.pending = []
        return 1

    def AddAccessAllowedAceEx(self, _pacl, _revision, flags, mask, pointer):
        self.pending.append((0, flags, mask, self.sid_pointers[pointer.value]))
        return 1

    def SetNamedSecurityInfoW(self, _path, _obj, info, owner, _group, _dacl, _sacl):
        assert owner is None
        assert not info & windows_acl.OWNER_SECURITY_INFORMATION
        self.security_information = info
        self.rebuild()
        return 0

    def rebuild(self):
        rows = []
        for index, (kind, flags, mask, sid) in enumerate(self.pending, 1):
            self._sids[index] = sid
            rows.append((kind, flags, mask, bytes([index]) + bytes(11)))
        self._acl_buf = _build_acl(tuple(rows))


@pytest.fixture
def lockdown(tmp_path, monkeypatch):
    """Use a real fd/stat; simulate only the Windows platform adapter."""

    def prepare(owner, directory):
        path = tmp_path / "protected"
        if directory:
            path.mkdir()
            fd = pc.pin_directory(path)
        else:
            path.touch()
            fd = os.open(path, os.O_RDONLY)
        fake = _RoundTripDlls(owner)
        monkeypatch.setattr(pc, "IS_POSIX", False)
        monkeypatch.setattr(pc, "current_user_sid", lambda: ME)
        monkeypatch.setattr(windows_acl, "_load", lambda: (fake, fake))
        # Local-volume classification is a separate native seam; the ACL parser
        # and validator remain real, including all their refusal branches.
        monkeypatch.setattr(windows_acl, "_volume_is_local", lambda *args: True)
        monkeypatch.setattr(windows_acl, "owner_only_dacl_matches", lambda *a, **kw: False)
        try:
            if directory:
                pc.restrict_dir_to_owner(path)
            else:
                pc.restrict_to_owner(path)
        except BaseException:
            os.close(fd)
            raise
        return path, fd, fake

    return prepare


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("owner", [ME, ADMINS, SYSTEM, TRUSTED_INSTALLER, OTHER, OWNER_RIGHTS])
def test_lockdown_writer_parser_validator_roundtrip(lockdown, owner, directory):
    path, fd, fake = lockdown(owner, directory)
    try:
        security = windows_acl.describe(path)
        assert security.owner_sid == owner
        assert {writer.sid for writer in security.writers} == {OWNER_RIGHTS, ME}
        assert fake.security_information == 0x80000004
        assert all(flags == (3 if directory else 0) for _, flags, _, _ in fake.pending)
        assert OWNER_RIGHTS not in windows_acl.WELL_KNOWN_TRUSTED_SIDS
        if owner in (OTHER, OWNER_RIGHTS):
            with pytest.raises(OSError, match="unsafe process record ACL"):
                records._require_owner(fd, path, directory=directory)
        else:
            assert records._require_owner(fd, path, directory=directory) == os.fstat(fd)
    finally:
        os.close(fd)


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize(
    "fault", ["missing-sid", "writer", "remote", "null", "unknown", "unreadable"]
)
def test_lockdown_descriptor_faults_still_refuse(lockdown, monkeypatch, directory, fault):
    path, fd, fake = lockdown(ME, directory)
    try:
        if fault == "missing-sid":
            monkeypatch.setattr(pc, "current_user_sid", lambda: None)
        elif fault == "writer":
            windows_acl.apply_owner_only(path, inherit=directory, sids=(OWNER_RIGHTS, ME, OTHER))
        elif fault == "remote":
            monkeypatch.setattr(windows_acl, "_volume_is_local", lambda *args: False)
        elif fault == "null":
            fake._null_dacl = True
        elif fault == "unknown":
            _, flags, mask, sid = fake.pending[0]
            fake.pending[0] = (9, flags, mask, sid)
            fake.rebuild()
        elif fault == "unreadable":
            fake._rc = 5
        with pytest.raises(OSError, match="process record ACL"):
            records._require_owner(fd, path, directory=directory)
    finally:
        os.close(fd)


@pytest.mark.skipif(not pc.IS_WINDOWS, reason="requires native Windows ACLs and directory pins")
def test_native_windows_lockdown_publication_roundtrip(tmp_path, monkeypatch):
    """No ACL mocks: the narrowly exempted fixture uses real lockdown helpers."""
    home = tmp_path / "home"
    pc.make_owner_only_dir(home)
    pc.restrict_dir_to_owner(home)
    sid = pc.current_user_sid()
    assert sid
    assert windows_acl.owner_only_dacl_matches(home, inherit=True, sids=(OWNER_RIGHTS, sid))
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: "fixture-incarnation")
    # No process is created, inspected or signalled; all records are fixture-owned.
    pid = 424242
    for key in ("test:first", "test:replacement"):
        records.publish_binding(home, pid, key, "")
        with records.record_directory(home) as directory:
            result = records.read_record(directory, f"{pid}.json")
            assert result is not None
            assert result[0] == {
                "version": 2,
                "session_key": key,
                "process_start": "fixture-incarnation",
                "memory_store": "",
            }
            assert directory.lstat(records.LOCK_NAME) is not None
            assert windows_acl.owner_only_dacl_matches(
                directory.describe(f"{pid}.json"), inherit=False, sids=(OWNER_RIGHTS, sid)
            )
