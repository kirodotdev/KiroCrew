"""The string code in ``source_providers/ownership.py`` answers as its old regexes did.

Product code keeps regex out, so each pattern became plain string checks. The
patterns are kept here, verbatim, as the reference: every replacement must agree
with its pattern over a shared corpus of matching and near-miss inputs (Unicode
digits and letters, empty parts, ``?``/``#`` cuts, 13-digit numbers, newlines).
Each test loops over its corpus and lists every input where the two disagree.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Iterable

from kiro_crew.dashboard.source_providers import ownership

ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
WRAPPER_ARG = re.compile(r"^(-.*|\d+(\.\d+)?[smhd]?)$")
EXIT_TRAILER = re.compile(r"Exit Code: (-?\d+)\s*\Z")
GH_SAID_PULL = re.compile(
    r"pull request ([A-Za-z0-9][\w.-]*)/([\w.-]+)#([0-9]{1,12})\b", re.IGNORECASE
)
OWNER_REPO_NUMBER = re.compile(r"^([A-Za-z0-9][\w.-]*)/([\w.-]+)#([0-9]{1,12})$")
API_PULL = re.compile(r"^repos/([^/?#{}]+)/([^/?#{}]+)/pulls/([0-9]{1,12})(?:[/?#]|$)")
GH_ALREADY_EXISTS = re.compile(r"a pull request for branch\b.*\balready exists", re.IGNORECASE)
#: The write verbs now come from the batch guard's own table
#: (``acp.batch_guard._GH_WRITE_VERBS``), which also has ``reopen``.
GH_WRITE_HINT = re.compile(
    r"gh\s+(?:pr\s+(?:create|edit|comment|close|merge|ready|review|reopen)|issue\s+create|api)\b"
)
API_NUMBER = re.compile(r"/(?:pulls|issues)/([0-9]{1,12})(?:[/?#]|$)")
API_HOST = re.compile(r"^https://([^/]+)/(?:api/v3/)?")
REDIRECT = re.compile(r"^(?:1?>|>>|&>)")

NUMBERS = ["1", "42", "123456789012", "1234567890123", "", "٣", "4٣", "0042", "x1"]
NAMES = ["acme", "a", "", "a.b-c_d", "-x", ".x", "Ünï", "ſx", "İx", "ıx", "Kx", "a b", "{o}"]
TAILS = ["", "\n", "\n\n", "/", "?q=1", "#frag", "x", "_", ".", " ", "٣", "\nx"]


def _disagree(
    corpus: Iterable[str], ours: Callable[[str], object], theirs: Callable[[str], object]
) -> list[tuple[str, object, object]]:
    return [(text, mine, ref) for text in corpus if (mine := ours(text)) != (ref := theirs(text))]


def _groups(pattern: re.Pattern[str], how: str = "match") -> Callable[[str], object]:
    def read(text: str) -> object:
        found = getattr(pattern, how)(text)
        return found.groups() if found else None

    return read


def _owner_repo_corpus() -> list[str]:
    out = [
        f"{owner}/{repo}#{number}{tail}"
        for owner, repo, number, tail in itertools.product(NAMES, NAMES, NUMBERS, TAILS)
    ]
    return out + ["a/b/c#1", "a#b/c#1", "a/b#1#2", "#1", "a/b", "a/b#", "/b#1", "a/#1", ""]


def _api_corpus() -> list[str]:
    out = [
        f"repos/{owner}/{repo}/{kind}/{number}{tail}"
        for owner, repo, kind, number, tail in itertools.product(
            NAMES, NAMES, ["pulls", "issues"], NUMBERS, TAILS
        )
    ]
    return out + [
        "repos/a/b/pulls",
        "repos/a/b/pull/1",
        "repo/a/b/pulls/1",
        "repos/a/b/c/pulls/1",
        "repos/a?/b/pulls/1",
        "repos/a/b#/pulls/1",
        "repos//b/pulls/1",
        "repos/a/b/pulls/1/comments",
        "repos/a/b/issues/x/pulls/7",
        "repos/a/b/pulls/x/issues/7?y",
        "x/pulls/7",
        "/pulls/7/issues/8",
        "pulls/7",
        "",
    ]


def test_assignment():
    corpus = [
        f"{name}={value}"
        for name, value in itertools.product(NAMES + ["X1", "_", "1A", "ab-c"], ["", "v", "="])
    ] + ["noequals", "=x", "", "A", "a_b=", "a\n=x"]
    assert _disagree(corpus, ownership._is_assignment, lambda w: bool(ASSIGNMENT.match(w))) == []


def test_wrapper_arg():
    corpus = [
        f"{n}{dot}{frac}{unit}{tail}"
        for n, dot, frac, unit, tail in itertools.product(
            NUMBERS,
            ["", "."],
            ["", "5", "٣"],
            ["", "s", "m", "h", "d", "x", "ss"],
            ["", "\n", "\nx"],
        )
    ] + ["-", "-x", "-x\n", "-x\ny", "--long=1", "", "s", ".5", "1.", "1..2"]
    assert _disagree(corpus, ownership._is_wrapper_arg, lambda w: bool(WRAPPER_ARG.match(w))) == []


def test_exit_trailer():
    corpus = [
        f"{head}Exit Code: {code}{tail}"
        for head, code, tail in itertools.product(
            ["", "out\n", "Exit Code: 9\n", "Exit Code: 0x"],
            ["0", "1", "-1", "--1", "", "٣", "0 1", "12a", "- 1"],
            ["", "\n", " \n\t", "\nmore", "x"],
        )
    ] + ["", "no trailer", "Exit Code:0"]

    def theirs(text: str) -> object:
        found = EXIT_TRAILER.search(text)
        return found.group(1) if found else None

    assert _disagree(corpus, ownership._exit_code, theirs) == []


def test_owner_repo_number():
    assert (
        _disagree(_owner_repo_corpus(), ownership._owner_repo_number, _groups(OWNER_REPO_NUMBER))
        == []
    )


def test_said_pulls():
    corpus = [
        f"{lead}{phrase} {owner}/{repo}#{number}{tail}"
        for lead, phrase, owner, repo, number, tail in itertools.product(
            ["", "✓ Merged ", "x"],
            ["pull request", "Pull Request", "PULL REQUEST", "pull requeſt", "pull  request"],
            ["acme", "", "Ünï", "ſx", "İx", "Kx", "-x"],
            ["w", "", "a.b"],
            ["5", "1234567890123", "4٣", ""],
            ["", " (fix #9)", "_", ".", "\nnext pull request o/r#7"],
        )
    ] + ["pull request o/r#1 pull request p/q#2", "pull request pull request o/r#3", ""]
    assert (
        _disagree(
            corpus,
            lambda text: list(ownership._said_pulls(text)),
            lambda text: [m.groups() for m in GH_SAID_PULL.finditer(text)],
        )
        == []
    )


def test_api_pull():
    assert _disagree(_api_corpus(), ownership._api_pull, _groups(API_PULL)) == []


def test_api_number():
    def theirs(text: str) -> object:
        found = API_NUMBER.search(text)
        return int(found.group(1)) if found else None

    assert _disagree(_api_corpus(), ownership._api_number, theirs) == []


def test_api_host():
    corpus = [
        "https://api.github.com/repos/a/b/pulls/1",
        "https://ghe.example.com/api/v3/repos/a/b/pulls/1",
        "https://ghe.example.com/api/v3",
        "https://ghe.example.com/",
        "https://ghe.example.com",
        "https:///repos",
        "http://api.github.com/repos",
        "repos/a/b/pulls/1",
        "https://h/api/v3/api/v3/x",
        "",
    ]

    def theirs(text: str) -> object:
        found = API_HOST.match(text)
        return (found.group(1), text[found.end() :]) if found else None

    assert _disagree(corpus, ownership._api_host, theirs) == []


def test_gh_already_exists():
    corpus = [
        'a pull request for branch "x" into branch "main" already exists:\nhttps://x',
        "A PULL REQUEST FOR BRANCH x ALREADY EXISTS",
        "a pull request for branch x\nalready exists",
        "a pull request for branchx already exists",
        "a pull request for branch x xalready exists",
        "a pull request for branch already exists",
        "a pull request for branch_ already exists",
        "a pull request for branch x already exiſts",
        "a pull request for branch x alreadİ exists",
        "a pull request for branch x already existsx",
        "noise a pull request for branch x; a pull request for branch y already exists",
        "",
    ]
    assert (
        _disagree(corpus, ownership._gh_already_exists, lambda t: bool(GH_ALREADY_EXISTS.search(t)))
        == []
    )


def test_gh_write_hint():
    corpus = [
        f"{lead}gh{gap}{rest}"
        for lead, gap, rest in itertools.product(
            ["", "x", "\\n", "cd x && ", "ghgh"],
            ["", " ", "  ", "\t", "\n", "\u3000"],
            [
                "pr create",
                "pr  edit",
                "pr editx",
                "pr\ncomment",
                "prcomment",
                "issue create",
                "issue view",
                "issue  create_",
                "api",
                "apix",
                "api/x",
                "pr view",
                "pr ready",
                "pr reopen",
                "pr reopenx",
                "pr",
            ],
        )
    ] + ["gh pr view && gh api x", ""]
    assert (
        _disagree(corpus, ownership._gh_write_hint, lambda t: bool(GH_WRITE_HINT.search(t))) == []
    )


def test_redirect_to_stdout_file():
    corpus = [">", "1>", ">>", "&>", "2>", "<", "1>>", "&>>", ">&2", "", "x>"]
    assert (
        _disagree(
            corpus,
            lambda raw: raw.startswith((">", "1>", "&>")),
            lambda raw: bool(REDIRECT.match(raw)),
        )
        == []
    )


def test_repo_parts():
    corpus = [
        "acme/w",
        "https://github.com/acme/w",
        "http://github.com/acme/w",
        "https://https://x/y",
        "//acme//w//",
        "github.com/acme/w/",
        "",
        "/",
        "ftp://x/y",
        "github.com/{owner}/{repo}",
    ]

    def theirs(repo: str) -> object:
        parts = re.findall(r"[^/]+", re.sub(r"^https?://", "", repo))
        if len(parts) == 2:
            parts.insert(0, "h.example")
        if len(parts) == 3 and not any("{" in p or "}" in p for p in parts):
            return f"https://{parts[0]}/{parts[1]}/{parts[2]}/pull/1"
        return None

    # The call's own host hint wins, so a two-part ``owner/repo`` does not depend on
    # the process-wide host settings.
    assert (
        _disagree(corpus, lambda repo: ownership._repo_url(repo, 1, host_hint="h.example"), theirs)
        == []
    )
