"""Strict source-provider pull-request target parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlparse

from kiro_crew.monitoring.github_pull_request import parse_github_pull_request_target
from kiro_crew.security import redact_credentials

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
_AZURE_SEGMENT_RE = re.compile(r"^[^/:\\~&%;@'\"?<>|#$*}{,+=\[\]\x00-\x1f\x7f-\x9f\u2028\u2029]+$")
_GITLAB_MARKER = ("-", "merge_requests")


class GitLabHostNotAllowed(ValueError):
    """The target is a GitLab merge request on an unconfigured host."""


class InvalidPullRequestTarget(ValueError):
    """The monitor target is not a supported provider URL."""


def _parts(
    raw: str,
    *,
    host: str | None = None,
    allow_port: bool = False,
    segment_re: re.Pattern[str] = _SEGMENT_RE,
) -> tuple[str, list[str]]:
    if not isinstance(raw, str) or not raw:
        raise ValueError("target must be a supported pull-request URL")
    parsed = urlparse(raw)
    actual_host = (parsed.hostname or "").lower()
    port = parsed.port
    authority = f"{actual_host}:{port}" if port is not None else actual_host
    if (
        parsed.scheme != "https"
        or not actual_host
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not allow_port)
        or parsed.query
        or parsed.fragment
        or (host is not None and authority != host)
    ):
        raise ValueError("target must be a supported pull-request URL")
    parts = [unquote(part) for part in PurePosixPath(parsed.path).parts if part != "/"]
    if any(part in {".", ".."} or not part or segment_re.fullmatch(part) is None for part in parts):
        raise ValueError("target must be a supported pull-request URL")
    return authority, parts


@dataclass(frozen=True)
class GitLabMergeRequestTarget:
    host: str
    project_path: str
    iid: int

    @property
    def identity(self) -> str:
        return f"{self.host}/{self.project_path}!{self.iid}"

    @property
    def url(self) -> str:
        path = PurePosixPath(
            *(quote(part, safe="._~-") for part in PurePosixPath(self.project_path).parts)
        ).as_posix()
        return f"https://{self.host}/{path}/-/merge_requests/{self.iid}"


@dataclass(frozen=True)
class AzureDevOpsPullRequestTarget:
    organization: str
    project: str
    repository: str
    pull_request_id: int

    @property
    def identity(self) -> str:
        return (
            f"dev.azure.com/{self.organization}/{self.project}/"
            f"{self.repository}#{self.pull_request_id}"
        )

    @property
    def url(self) -> str:
        parts = (self.organization, self.project, self.repository)
        organization, project, repository = (quote(part, safe="._~-") for part in parts)
        return (
            f"https://dev.azure.com/{organization}/{project}/_git/{repository}/"
            f"pullrequest/{self.pull_request_id}"
        )


@dataclass(frozen=True)
class BitbucketPullRequestTarget:
    workspace: str
    repository: str
    pull_request_id: int

    @property
    def identity(self) -> str:
        return f"bitbucket.org/{self.workspace}/{self.repository}#{self.pull_request_id}"

    @property
    def url(self) -> str:
        workspace = quote(self.workspace, safe="._~-")
        repository = quote(self.repository, safe="._~-")
        return (
            f"https://bitbucket.org/{workspace}/{repository}/"
            f"pull-requests/{self.pull_request_id}"
        )


def _positive_id(raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("pull-request id must be positive") from exc
    if value <= 0 or str(value) != raw:
        raise ValueError("pull-request id must be positive")
    return value


def parse_gitlab_merge_request_target(
    raw: str,
    *,
    gitlab_hosts: list[str] | tuple[str, ...],
) -> GitLabMergeRequestTarget:
    actual_host, parts = _parts(raw, allow_port=True)
    allowed = {"gitlab.com", *(host.lower() for host in gitlab_hosts)}
    if len(parts) < 5 or tuple(parts[-3:-1]) != _GITLAB_MARKER:
        raise ValueError("target must be an allowed GitLab merge request")
    if actual_host not in allowed:
        raise GitLabHostNotAllowed("GitLab target host is not allowed")
    # Path shape is <namespace...>/<repo>/-/merge_requests/<iid>.
    project_parts = parts[:-3]
    if len(project_parts) < 2:
        raise ValueError("target must be an allowed GitLab merge request")
    iid = _positive_id(parts[-1])
    target = GitLabMergeRequestTarget(actual_host, PurePosixPath(*project_parts).as_posix(), iid)
    if target.url != raw:
        raise ValueError("target must use the canonical GitLab URL")
    return target


def parse_azure_devops_pull_request_target(raw: str) -> AzureDevOpsPullRequestTarget:
    _, parts = _parts(raw, host="dev.azure.com", segment_re=_AZURE_SEGMENT_RE)
    if len(parts) != 6 or parts[2] != "_git" or parts[4] != "pullrequest":
        raise ValueError("target must be an Azure DevOps Services pull request")
    target = AzureDevOpsPullRequestTarget(parts[0], parts[1], parts[3], _positive_id(parts[5]))
    return target


def parse_bitbucket_pull_request_target(raw: str) -> BitbucketPullRequestTarget:
    _, parts = _parts(raw, host="bitbucket.org")
    if len(parts) != 4 or parts[2] != "pull-requests":
        raise ValueError("target must be a Bitbucket Cloud pull request")
    target = BitbucketPullRequestTarget(parts[0], parts[1], _positive_id(parts[3]))
    if target.url != raw:
        raise ValueError("target must use the canonical Bitbucket Cloud URL")
    return target


def infer_pull_request_kind(
    raw: str,
    *,
    gitlab_hosts: list[str] | tuple[str, ...],
) -> str:
    """Return the provider kind only after its strict parser accepts the URL."""
    parsers = (
        ("github_pull_request", parse_github_pull_request_target),
        (
            "gitlab_merge_request",
            lambda value: parse_gitlab_merge_request_target(value, gitlab_hosts=gitlab_hosts),
        ),
        ("azure_devops_pull_request", parse_azure_devops_pull_request_target),
        ("bitbucket_pull_request", parse_bitbucket_pull_request_target),
    )
    for kind, parser in parsers:
        try:
            parser(raw)
        except GitLabHostNotAllowed:
            raise
        except ValueError:
            continue
        return kind
    raise ValueError("target must be a supported pull-request URL")


def normalize_pull_request_target(
    kind: str,
    raw: str,
    *,
    gitlab_hosts: list[str] | tuple[str, ...],
) -> str:
    """Validate that *kind* owns *raw* and return its canonical URL."""
    if kind == "github_pull_request":
        target = parse_github_pull_request_target(raw).url
    elif kind == "gitlab_merge_request":
        target = parse_gitlab_merge_request_target(raw, gitlab_hosts=gitlab_hosts).url
    elif kind == "azure_devops_pull_request":
        target = parse_azure_devops_pull_request_target(raw).url
    elif kind == "bitbucket_pull_request":
        target = parse_bitbucket_pull_request_target(raw).url
    else:
        raise ValueError("kind is not a supported pull-request monitor")

    redacted, warnings = redact_credentials(target)
    if warnings or redacted != target:
        raise ValueError("target must not contain credential-shaped text")
    return target
