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

EXIT_TRAILER = re.compile(r"Exit Code: (-?\d+)\s*\Z")
GH_SAID_PULL = re.compile(
    r"pull request ([A-Za-z0-9][\w.-]*)/([\w.-]+)#([0-9]{1,12})\b", re.IGNORECASE
)
OWNER_REPO_NUMBER = re.compile(r"^([A-Za-z0-9][\w.-]*)/([\w.-]+)#([0-9]{1,12})$")
API_PULL = re.compile(r"^repos/([^/?#{}]+)/([^/?#{}]+)/pulls/([0-9]{1,12})(?:[/?#]|$)")
GH_ALREADY_EXISTS = re.compile(r"a pull request for branch\b.*\balready exists", re.IGNORECASE)
#: The write verbs now come from the batch guard's own table
#: (``acp.batch_guard._GH_WRITE_VERBS``), which also has ``reopen``. The old
#: assignment and wrapper-argument patterns have no replacement here: the card
#: finds a command's program with the guard's ``security.argv_rules._program_args``.
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
    # A prefilter: every text the old pattern matched must still pass (a superset),
    # and so must calls the guard reads that the pattern missed.
    assert [text for text in corpus if GH_WRITE_HINT.search(text)] == [
        text for text in corpus if GH_WRITE_HINT.search(text) and ownership._gh_write_hint(text)
    ]
    for text in ("gh.exe pr merge 5", 'gh "pr" edit 5', "gh pr -R o/r merge 5", "gh -R o/r api x"):
        assert ownership._gh_write_hint(text), text
    for text in ("gh pr view 5", "gh issue list", "git commit -m edit", ""):
        assert not ownership._gh_write_hint(text), text


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


def _shell_reader_corpus() -> list[str]:
    """The shared reader's own parity corpus (``test_shell_commands_reader``: its hand
    cases and its seeded random strings over quotes, escapes, ``$(``, backticks, ``<>``
    and parens, many unterminated), the heredoc reader's cases, and substitution-heavy
    strings drawn the same seeded way."""
    import random

    import test_shell_commands_reader as reader

    rng = random.Random(35327)  # flake-ok: seeded, so the corpus is the same every run
    alphabet = [*"$<>()`'\"\\ ;&|\n#x", "$(", "<(", ">(", "$((", "\\$(", "gh pr edit 5"]
    drawn = ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 14))) for _ in range(4000)]
    hand = [
        "url=$(gh pr create --fill) && echo $url",
        'gh pr edit 5 --body "$(cat f)"',
        "diff <(ls a) >(cat) && gh api x",
        "echo '$(not)' && gh pr merge 5",
        "a=$(b $(c)) ; gh pr close 5",
        "echo \\$(x) && gh pr view",
        "x=$( unclosed",
        "x=$(printf ')') && gh pr ready 5",
        "echo $((1 + 2)) && gh pr ready 5",
        "x=$(cat <<EOF\n)\nEOF\n) && gh pr edit 5",
    ]
    return [*reader._HAND_CORPUS, *reader._random_corpus(4000), *hand, *drawn]


def _cut_commands(text: str) -> object:
    """What the shared reader cuts *text* into: each command's separator and words.
    A run of ``_`` reads as one, since the blanking writes one ``_`` per character
    where ``without_substitutions`` writes one per span; an error is part of the answer."""
    from kiro_crew.security import shell_commands

    def squeezed(raw: str) -> str:
        out: list[str] = []
        for ch in raw:
            if not (ch == "_" and out and out[-1] == "_"):
                out.append(ch)
        return "".join(out)

    try:
        read = shell_commands.read_commands(text, nested=False)
    except Exception as exc:  # noqa: BLE001 - parity includes the error
        return type(exc)
    return [(cmd.separator, [squeezed(w.raw) for w in cmd.argv]) for cmd in read]


def test_substitution_blanking_cuts_what_the_shared_reader_cuts():
    """Kept on purpose: ``ownership._blank_substitutions`` repeats the scan of
    ``security.shell_commands.without_substitutions`` only to keep the text's length
    (``_gh_calls`` matches a command across two reads by offset). This holds the two
    together over the shared reader's own corpus, so a change to either side fails here."""
    from kiro_crew.security import shell_commands

    corpus = _shell_reader_corpus()
    # Not vacuous: many of the inputs hold a substitution that both sides blank.
    assert sum(shell_commands.without_substitutions(t) != t for t in corpus) > 500
    assert [
        text
        for text in corpus
        if len(ownership._blank_substitutions(text)) != len(text)
        or _cut_commands(ownership._blank_substitutions(text))
        != _cut_commands(shell_commands.without_substitutions(text))
    ] == []
