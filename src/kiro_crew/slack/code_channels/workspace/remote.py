"""Turn a git remote URL into a forge repo reference and its web links.

The URL is repository config, which the agent working in the worktree can edit,
so it is parsed strictly and only ever rebuilt from the validated host, owner and
name: userinfo (an ``https://user:token@host/...`` credential) and any query or
fragment never reach a context-bar link or a ``gh`` argument.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

#: URL path separator (a URL, not a filesystem path: "/" on every platform).
_URL_SEP = "/"

_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
#: ``git@host:owner/name(.git)`` — the scp-like form git accepts for SSH.
_SCP_RE = re.compile(r"^[A-Za-z0-9._-]+@(?P<host>[^:/]+):(?P<path>[^\\]+)$")


@dataclass(frozen=True)
class RepoRef:
    """A forge repository: ``host`` / ``owner`` / ``name``."""

    host: str
    owner: str
    name: str

    @property
    def slug(self) -> str:
        """``owner/name``."""
        return f"{self.owner}/{self.name}"

    @property
    def web_url(self) -> str:
        """The repository's https page."""
        return f"https://{self.host}/{self.owner}/{self.name}"

    def tree_url(self, branch: str) -> str:
        """The https page for ``branch``."""
        return f"{self.web_url}/tree/{quote(branch, safe='/')}"


def parse_remote(url: str) -> RepoRef | None:
    """``RepoRef`` for an https/ssh/scp-style remote with an ``owner/name`` path, else None.

    Only two-segment paths parse (GitHub's shape): a nested GitLab group path is
    not a ``gh`` repository, and a link built from a guess would be wrong.
    """
    url = (url or "").strip()
    if not url or any(ord(ch) < 33 for ch in url):
        return None
    host = path = ""
    scp = _SCP_RE.match(url)
    if scp and "://" not in url:
        host, path = scp.group("host"), scp.group("path")
    else:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return None
        if parts.scheme not in {"https", "http", "ssh", "git"} or port not in (None, 22, 443):
            return None
        host, path = parts.hostname or "", parts.path
    host = host.lower()
    if not _HOST_RE.match(host):
        return None
    segments = [s for s in path.strip(_URL_SEP).split(_URL_SEP) if s]
    if len(segments) != 2:
        return None
    owner, name = segments
    if name.endswith(".git"):
        name = name[: -len(".git")]
    if not (_SEGMENT_RE.match(owner) and _SEGMENT_RE.match(name)) or {owner, name} & {".", ".."}:
        return None
    return RepoRef(host, owner, name)
