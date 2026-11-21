"""Cross-platform secret storage for provider credentials.

Provider configuration intentionally stores only an opaque ``credential_ref``.
The secret itself is delegated to the operating system credential store:

* Windows: Windows Credential Manager (CredRead/CredWrite)
* macOS: Keychain via the system ``security`` utility
* Linux: Secret Service via ``secret-tool`` when available

The interface is provider-neutral and stores opaque UTF-8 strings.  Kiro BYO
currently serializes a small JSON object containing the API key and any secret
headers, allowing future provider adapters to reuse the same abstraction.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Protocol


class CredentialStoreError(RuntimeError):
    pass


class CredentialStore(Protocol):
    @property
    def backend_name(self) -> str: ...

    def get(self, ref: str) -> str | None: ...

    def set(self, ref: str, value: str) -> None: ...

    def delete(self, ref: str) -> None: ...

    def exists(self, ref: str) -> bool: ...


@dataclass(frozen=True)
class ProviderSecrets:
    api_key: str = ""
    headers: dict[str, str] = field(default_factory=dict)


_SECRET_SCHEMA_VERSION = 1


SENSITIVE_HEADER_NAMES = frozenset({
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "x-auth-token",
    "x-goog-api-key",
})


def is_sensitive_header(name: str) -> bool:
    return str(name or "").strip().lower() in SENSITIVE_HEADER_NAMES


def encode_provider_secrets(secrets: ProviderSecrets) -> str:
    return json.dumps(
        {
            "version": _SECRET_SCHEMA_VERSION,
            "api_key": secrets.api_key,
            "headers": dict(secrets.headers),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_provider_secrets(value: str | None) -> ProviderSecrets:
    if not value:
        return ProviderSecrets()
    try:
        raw = json.loads(value)
    except json.JSONDecodeError:
        # Compatibility with an early/simple store that contained only the API
        # key as the raw credential value.
        return ProviderSecrets(api_key=value)
    if not isinstance(raw, dict):
        return ProviderSecrets(api_key=value)
    api_key = raw.get("api_key", "")
    if not isinstance(api_key, str):
        api_key = ""
    headers_raw = raw.get("headers", {})
    headers = (
        {str(k): str(v) for k, v in headers_raw.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(headers_raw, dict)
        else {}
    )
    return ProviderSecrets(api_key=api_key, headers=headers)


def provider_credential_ref(provider_id: str) -> str:
    provider_id = str(provider_id or "").strip()
    if not provider_id:
        raise CredentialStoreError("provider id is required for a credential reference")
    return f"provider:{provider_id}"


class MemoryCredentialStore:
    """Small deterministic store used by tests and embedded callers."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    @property
    def backend_name(self) -> str:
        return "memory"

    def get(self, ref: str) -> str | None:
        return self._values.get(ref)

    def set(self, ref: str, value: str) -> None:
        self._values[ref] = value

    def delete(self, ref: str) -> None:
        self._values.pop(ref, None)

    def exists(self, ref: str) -> bool:
        return ref in self._values


class EnvironmentCredentialStore:
    """Read-only resolver for explicit ``env:VARIABLE`` references."""

    @property
    def backend_name(self) -> str:
        return "environment"

    @staticmethod
    def _name(ref: str) -> str:
        if not ref.startswith("env:") or not ref[4:].strip():
            raise CredentialStoreError("environment credential references must use env:VARIABLE")
        return ref[4:].strip()

    def get(self, ref: str) -> str | None:
        return os.environ.get(self._name(ref))

    def set(self, ref: str, value: str) -> None:
        raise CredentialStoreError("environment credential references are read-only")

    def delete(self, ref: str) -> None:
        raise CredentialStoreError("environment credential references are read-only")

    def exists(self, ref: str) -> bool:
        return self._name(ref) in os.environ


class WindowsCredentialManagerStore:
    """Generic credentials stored under the current Windows user's vault."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168
    SERVICE_PREFIX = "KiroBYO/"

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    class _CREDENTIALW(ctypes.Structure):
        pass

    _PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)
    _CREDENTIALW._fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise CredentialStoreError("Windows Credential Manager is available only on Windows")
        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._cred_write = self._advapi32.CredWriteW
        self._cred_write.argtypes = [ctypes.POINTER(self._CREDENTIALW), ctypes.c_uint32]
        self._cred_write.restype = ctypes.c_int
        self._cred_read = self._advapi32.CredReadW
        self._cred_read.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(self._PCREDENTIALW)]
        self._cred_read.restype = ctypes.c_int
        self._cred_delete = self._advapi32.CredDeleteW
        self._cred_delete.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        self._cred_delete.restype = ctypes.c_int
        self._cred_free = self._advapi32.CredFree
        self._cred_free.argtypes = [ctypes.c_void_p]
        self._cred_free.restype = None

    @property
    def backend_name(self) -> str:
        return "windows-credential-manager"

    @classmethod
    def _target(cls, ref: str) -> str:
        if not ref:
            raise CredentialStoreError("credential reference must not be empty")
        return cls.SERVICE_PREFIX + ref

    def get(self, ref: str) -> str | None:
        out = self._PCREDENTIALW()
        if not self._cred_read(self._target(ref), self.CRED_TYPE_GENERIC, 0, ctypes.byref(out)):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return None
            raise CredentialStoreError(f"CredReadW failed with Windows error {error}")
        try:
            cred = out.contents
            if not cred.CredentialBlob or cred.CredentialBlobSize == 0:
                return ""
            raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CredentialStoreError("stored Kiro BYO credential is not valid UTF-8") from exc
        finally:
            self._cred_free(out)

    def set(self, ref: str, value: str) -> None:
        if not isinstance(value, str):
            raise CredentialStoreError("credential value must be a string")
        raw = value.encode("utf-8")
        if len(raw) > 2560:
            raise CredentialStoreError("credential is too large for Windows Credential Manager")
        blob = (ctypes.c_ubyte * max(1, len(raw)))()
        if raw:
            ctypes.memmove(blob, raw, len(raw))
        cred = self._CREDENTIALW()
        cred.Flags = 0
        cred.Type = self.CRED_TYPE_GENERIC
        cred.TargetName = self._target(ref)
        cred.Comment = "Kiro BYO provider credential"
        cred.CredentialBlobSize = len(raw)
        cred.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        cred.AttributeCount = 0
        cred.Attributes = None
        cred.TargetAlias = None
        cred.UserName = "Kiro BYO"
        if not self._cred_write(ctypes.byref(cred), 0):
            error = ctypes.get_last_error()
            raise CredentialStoreError(f"CredWriteW failed with Windows error {error}")
        # Best effort zeroing of the temporary native buffer.
        ctypes.memset(blob, 0, max(1, len(raw)))

    def delete(self, ref: str) -> None:
        if self._cred_delete(self._target(ref), self.CRED_TYPE_GENERIC, 0):
            return
        error = ctypes.get_last_error()
        if error != self.ERROR_NOT_FOUND:
            raise CredentialStoreError(f"CredDeleteW failed with Windows error {error}")

    def exists(self, ref: str) -> bool:
        return self.get(ref) is not None


class MacOSKeychainStore:
    SERVICE = "Kiro BYO"

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise CredentialStoreError("macOS Keychain is available only on macOS")
        if not shutil.which("security"):
            raise CredentialStoreError("macOS security utility was not found")

    @property
    def backend_name(self) -> str:
        return "macos-keychain"

    def get(self, ref: str) -> str | None:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", self.SERVICE, "-a", ref, "-w"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.rstrip("\n")

    def set(self, ref: str, value: str) -> None:
        # Apple's security(1) does not provide a stdin form for -w.  This is kept
        # behind the CredentialStore abstraction so a future native Security
        # framework implementation can replace it without touching providers/UI.
        proc = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", self.SERVICE, "-a", ref, "-w", value],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise CredentialStoreError(proc.stderr.strip() or "could not write macOS Keychain credential")

    def delete(self, ref: str) -> None:
        subprocess.run(
            ["security", "delete-generic-password", "-s", self.SERVICE, "-a", ref],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def exists(self, ref: str) -> bool:
        return self.get(ref) is not None


class LinuxSecretServiceStore:
    def __init__(self) -> None:
        if not sys.platform.startswith("linux"):
            raise CredentialStoreError("Secret Service backend is available only on Linux")
        if not shutil.which("secret-tool"):
            raise CredentialStoreError("secret-tool is not installed; install libsecret tools or use env: references")

    @property
    def backend_name(self) -> str:
        return "linux-secret-service"

    @staticmethod
    def _attrs(ref: str) -> list[str]:
        return ["service", "KiroBYO", "credential_ref", ref]

    def get(self, ref: str) -> str | None:
        proc = subprocess.run(
            ["secret-tool", "lookup", *self._attrs(ref)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.rstrip("\n")

    def set(self, ref: str, value: str) -> None:
        proc = subprocess.run(
            ["secret-tool", "store", "--label=Kiro BYO provider credential", *self._attrs(ref)],
            input=value,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise CredentialStoreError(proc.stderr.strip() or "could not write Secret Service credential")

    def delete(self, ref: str) -> None:
        subprocess.run(
            ["secret-tool", "clear", *self._attrs(ref)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def exists(self, ref: str) -> bool:
        return self.get(ref) is not None


class RoutedCredentialStore:
    """Route env: references directly and lazily construct the native OS store."""

    def __init__(self, os_store: CredentialStore | None = None) -> None:
        self._os_store = os_store
        self.env_store = EnvironmentCredentialStore()

    @property
    def backend_name(self) -> str:
        if self._os_store is not None:
            return self._os_store.backend_name
        if sys.platform == "win32":
            return "windows-credential-manager"
        if sys.platform == "darwin":
            return "macos-keychain"
        if sys.platform.startswith("linux"):
            return "linux-secret-service"
        return f"unsupported:{sys.platform}"

    def _native_store(self) -> CredentialStore:
        if self._os_store is not None:
            return self._os_store
        if sys.platform == "win32":
            self._os_store = WindowsCredentialManagerStore()
        elif sys.platform == "darwin":
            self._os_store = MacOSKeychainStore()
        elif sys.platform.startswith("linux"):
            self._os_store = LinuxSecretServiceStore()
        else:
            raise CredentialStoreError(f"no native credential backend is available for platform {sys.platform!r}")
        return self._os_store

    def _store(self, ref: str) -> CredentialStore:
        return self.env_store if ref.startswith("env:") else self._native_store()

    def get(self, ref: str) -> str | None:
        return self._store(ref).get(ref)

    def set(self, ref: str, value: str) -> None:
        self._store(ref).set(ref, value)

    def delete(self, ref: str) -> None:
        self._store(ref).delete(ref)

    def exists(self, ref: str) -> bool:
        return self._store(ref).exists(ref)


def default_credential_store() -> CredentialStore:
    """Return a router for env refs and the platform-native credential backend.

    Native backend construction is lazy, so an ``env:VARIABLE`` configuration
    works on a minimal Linux host even when ``secret-tool`` is not installed.
    """

    return RoutedCredentialStore()
