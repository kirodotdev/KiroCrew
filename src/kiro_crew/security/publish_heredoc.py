"""Heredoc and sink reading for the git-publish floor's program-word reader.

Moved out of ``publish_program_word`` unchanged so each file stays under the
per-file line cap: the heredoc delimiter and body reader, the sink and companion
tables, and the file-write reader. ``publish_program_word`` imports every name
here and stays the import path callers use.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from .shell_normalizer import (
    _fold_line_continuations,
    _iter_shell_chars,
)

#: How many heredoc bodies nested one inside another are read.
_HEREDOC_NESTING_LIMIT = 8

#: ``push`` with no letter, digit or ``_`` glued to either side, read on the
#: dequoted text.  Glued (``pushed``, ``xpush``) it is part of another word, as
#: the shell reads it too; an expansion or glob beside it leaves a ``$``,
#: ``}``, ``*`` or ``[`` there, which still counts.
_PUSH_WORD_RE = re.compile(r"(?<![a-z0-9_])push(?![a-z0-9_])")

#: Characters that end the word a heredoc's delimiter is read from.
_HEREDOC_TAG_BREAK = frozenset(" \t\n;&|()<>")

#: How far past ``<<`` a heredoc delimiter is read.
_HEREDOC_TAG_WINDOW = 256


class _Heredoc(NamedTuple):
    """A heredoc whose body starts after the next newline."""

    tag: str
    strip_tabs: bool
    #: True when the command it feeds is a ``_HEREDOC_SINKS`` subcommand.
    data: bool = False
    #: True when any part of the delimiter word is quoted or escaped, so the
    #: body is read raw, without line continuations.
    quoted: bool = False
    #: True when the command only writes it to a named file (``_FILE_WRITE_RE``).
    file: bool = False
    #: True when the sink it feeds is a ``_HOOKLESS_SINKS`` program.
    hookless: bool = False


#: A ``<<`` whose delimiter cannot be read, so where its body ends is unknown.
_UNREADABLE_HEREDOC = _Heredoc("", False)

#: What ``_next_cut`` returns for a heredoc whose delimiter cannot be read.
_UNREADABLE = -1

#: Programs that read a heredoc only as data and print none of it back as
#: text another program could run, each with the subcommands that do: the
#: body is a pull request, issue or release text, or a commit, note or tag
#: message.
_HEREDOC_SINKS = {
    "gh": frozenset({"issue", "pr", "release"}),
    "git": frozenset({"commit", "notes", "tag"}),
    "glab": frozenset({"issue", "mr", "release"}),
}

#: The sinks that run no hook on what they read: a ``commit-msg`` hook gets a
#: ``git commit`` message, but nothing gets a ``gh`` or ``glab`` body, which
#: runs only through a program that replaces ``gh`` or ``glab`` itself.
_HOOKLESS_SINKS = frozenset({"gh", "glab"})

#: The options of each sink that take the NEXT word as their value, lowercased
#: (``gh pr create --head "$BRANCH"``): that word is the value, never an option
#: such as the editor, unless an unquoted expansion splits it into more words.
#: A letter whose other case takes no value (``gh -f``/``--fill``, ``git tag
#: -f``) is left out.
_SINK_VALUE_OPTIONS = {
    "gh": frozenset(
        "-a -b -l -m -n -p -r -t --assignee --base --body --body-file --discussion-category"
        " --head --label --milestone --notes --notes-file --notes-start-tag --project --repo"
        " --reviewer --target --title".split()
    ),
    "git": frozenset("-m --author --date --file --message --trailer".split()),
    "glab": frozenset(
        "-a -b -d -l -m -s -t --assignee --description --label --milestone --reviewer"
        " --source-branch --target-branch --title".split()
    ),
}

#: Programs that may share a command text with a sink, each with the
#: subcommands it may run (an empty set: any arguments): none of them writes a
#: file, sets configuration, defines anything in the shell, or runs code its
#: arguments carry, so none can arrange for a sink's input to be executed.  A
#: ``git fetch`` or ``git push`` moves refs and objects only, so it is one too
#: unless an option names a program for the transport to run
#: (``_GIT_TRANSPORT_PROGRAM_OPTION_RE``); the push itself is judged by the floor.
_SINK_COMPANIONS = {
    ":": frozenset(),
    "[": frozenset(),
    "cat": frozenset(),
    "cd": frozenset(),
    "echo": frozenset(),
    "git": frozenset(
        {"add", "branch", "diff", "fetch", "log", "push", "rev-parse", "show", "status"}
    ),
    "ls": frozenset(),
    "pwd": frozenset(),
    "sleep": frozenset(),
    "test": frozenset(),
    "true": frozenset(),
}

#: A test, lint or build runner spelled with plain words only: it runs code
#: already on disk before the command (a Makefile, a ``package.json`` script, a
#: hook configuration), never code the command text carries.  Beside one a
#: sink's body is not data, since that code could plant a hook, but a markdown
#: bullet in it is not a program word (``_bullet_reading``).
_PLAIN_RUNNER_RE = re.compile(
    r"[ \t]*(?:make|(?:uv[ \t]+run[ \t]+|python3?[ \t]+-m[ \t]+)?(?:pytest|ruff[ \t]+(?:check|format))"
    r"|(?:npm|pnpm|yarn)[ \t]+(?:test|run[ \t]+[\w:.-]+)|pre-commit[ \t]+run)"
    r"(?:[ \t]+(?!-[a-z]*[ef]|--(?:eval|file|makefile))[\w./:-]+)*[ \t]*\Z"
)

#: A command beside which a markdown bullet in a ``_HOOKLESS_SINKS`` body is
#: not a program word either, on top of ``_PLAIN_RUNNER_RE``: ``git checkout``,
#: ``switch`` or ``stash``, or ``python``/``python3`` running a ``.py`` script
#: or ``bash``/``sh`` a ``.sh`` one by its path, with plain words only and no
#: ``--o...`` option (``git stash show --output=FILE`` writes a file), or
#: ``git rebase`` onto plain refs with no option at all (``-x``/``--exec`` runs
#: a command the text carries).  Each
#: runs only code that was on disk before the command (a checkout hook, the
#: script), never code the command text carries, and a ``gh`` or ``glab`` body
#: runs only through a program replacing that sink, which such code would have
#: to set up outside the text.
_HOOKLESS_SIBLING_RE = re.compile(
    r"[ \t]*(?:(?:git[ \t]+(?:checkout|switch|stash)|python3?[ \t]+(?!-)[\w./-]+\.py"
    r"|(?:ba)?sh[ \t]+(?!-)[\w./-]+\.sh)"
    r"(?:[ \t]+(?!--o)[\w./:@%+~^=-]+)*"
    r"|git[ \t]+rebase(?:[ \t]+(?!-)[\w./:@%+~^=-]+)+)[ \t]*\Z"
)

#: A sink option that hands the body to an editor, which can be any program:
#: ``--edit``, ``--editor`` or any abbreviation of them git or gh accepts
#: (``--edi``), or a short-option cluster whose leading letters carry ``e``
#: (``-e``, ``-eF-``).
_SINK_EDITOR_OPTION_RE = re.compile(r"--e(?:d(?:i(?:t(?:o(?:r)?)?)?)?)?\Z|-[a-z]*e")

#: A git sink option that loads a message template, which opens the editor on
#: it (``-t``, ``--template`` or an abbreviation of it): a template read from
#: stdin is the heredoc body.
_GIT_TEMPLATE_OPTION_RE = re.compile(r"--te(?:m(?:p(?:l(?:a(?:t(?:e)?)?)?)?)?)?(?:=|\Z)|-[a-z]*t")

#: A ``git`` option that writes its output to a file: ``--output=FILE`` and
#: ``--output-directory`` (``git log``/``diff``/``format-patch``).  An inert
#: companion carrying one can plant a hook, filter or config file the sink
#: then runs, so it is not inert.
_GIT_OUTPUT_FILE_OPTION_RE = re.compile(r"--output(?:-directory)?(?:=|\Z)")

#: A ``git fetch``/``git push`` option naming a program the transport runs
#: (``--upload-pack``, ``--receive-pack``, ``--exec``), in full or by any prefix
#: git's option parser could read as one of them.
_GIT_TRANSPORT_PROGRAM_OPTION_RE = re.compile(
    "(?:"
    + "|".join(
        re.escape(option[:end])
        for option in ("--upload-pack", "--receive-pack", "--exec")
        for end in range(3, len(option) + 1)
    )
    + r")(?:=|\Z)"
)

#: Shell syntax whose presence outside the data spans voids the sink exemption:
#: an output redirection or a process substitution, read unquoted, can write a
#: hook, filter or config file; a substitution or ``${...}`` expansion, read
#: unquoted or inside double quotes, can run or assign anything.
_SINK_VOIDING_UNQUOTED = (">", "<(")
_SINK_VOIDING_EXPANSIONS = ("$(", "`", "${")

#: A ``${NAME}`` expansion with no operator: it reads a variable as ``$NAME``
#: does, and runs and assigns nothing.
_PLAIN_BRACED_EXPANSION_RE = re.compile(r"\$\{[a-z_][a-z0-9_]*\}")


def _voids_sinks(executable: str) -> bool:
    """True if *executable* carries syntax that voids the sink exemption.

    Read with the quoting bash applies, so a ``>``, ``()`` or backtick inside a
    single-quoted title is text, while ``$(`` inside double quotes still runs.
    A function definition (unquoted) voids it too.  A plain ``${NAME}`` reads a
    variable and does not.
    """
    unquoted: list[str] = []
    expanding: list[str] = []
    for step in _iter_shell_chars(executable):
        plain = len(step.text) == 1
        unquoted.append(step.char if step.active else " ")
        expanding.append(step.char if step.active or (plain and step.state == 2) else " ")
    bare = "".join(unquoted)
    live = _PLAIN_BRACED_EXPANSION_RE.sub("", "".join(expanding))
    if any(token in bare for token in _SINK_VOIDING_UNQUOTED):
        return True
    return any(token in live for token in _SINK_VOIDING_EXPANSIONS) or bool(
        _FUNCTION_DEFINITION_RE.search(bare)
    )


#: A run of ``*`` leading a line, with a blank or the line's end after it: a
#: markdown bullet (``* push the branch``), which reads as a glob program word.
_BULLET_RE = re.compile(r"(?m)^([ \t]*)\*+(?=[ \t]|$)")

#: A heredoc delimiter word as a printer or file write spells it.
_HEREDOC_TAG = r"<<-?[ \t]*(?:'[^'\n]+'|\"[^\"\\$`\n]+\"|[a-z_][a-z0-9_]*)"

#: A plainly spelled path; ``_writes_file`` refuses device, process and git paths.
_FILE_PATH = r"([\w.@%+~/-]+)"

#: A command that only writes its heredoc to a named file: ``cat`` with the
#: heredoc and one ``>``/``>>`` redirection, in either order, or ``tee`` (``-a``)
#: of one path with the heredoc and an optional ``>/dev/null``.
_FILE_WRITE_RE = re.compile(
    rf"[ \t]*(?:cat[ \t]+(?:{_HEREDOC_TAG}[ \t]*>>?[ \t]*{_FILE_PATH}"
    rf"|>>?[ \t]*{_FILE_PATH}[ \t]*{_HEREDOC_TAG})"
    rf"|tee(?:[ \t]+-a)?[ \t]+{_FILE_PATH}(?:[ \t]*(?:{_HEREDOC_TAG}|>[ \t]*/dev/null)){{1,2}})"
    r"[ \t]*\Z"
)

#: ``cat`` printing one heredoc into a pipe (``cat <<'EOF' | gh pr create
#: --body-file -``): the command after the ``|`` is the one the body feeds.
_CAT_PIPE_RE = re.compile(rf"[ \t]*cat[ \t]+{_HEREDOC_TAG}[ \t]*\|(?![|&])")


def _writes_file(piece: str) -> bool:
    """True if command *piece* only writes its heredoc to a plainly named file.

    A device or process path (``/dev/fd/3``) can hand the body to a program, and
    a path git reads (``.git/...``, ``.gitattributes``, ``config/git``) can make a
    companion run it, so neither counts.
    """
    match = _FILE_WRITE_RE.match(piece)
    return match is not None and _plain_file(next(group for group in match.groups() if group))


def _plain_file(path: str) -> bool:
    """True if *path* is neither a device or process path nor a path git reads.

    A component carrying ``.git`` (``.git``, ``.gitattributes``, ``repo.git``)
    or exactly ``git`` is git's; ``.github``, which git never reads, is not.
    """
    parts = re.split(r"[\\/]", path)
    return not (
        path.startswith(("/dev/", "/proc/"))
        or "git" in parts
        or any(".git" in part and part != ".github" for part in parts)
    )


#: ``chmod`` of plainly spelled modes and paths (``chmod +x scripts/release.sh``):
#: it changes a mode and runs nothing.
_CHMOD_RE = re.compile(r"[ \t]*chmod((?:[ \t]+[\w.@%+~/,=-]+)+)[ \t]*\Z")

#: ``mkdir`` of plainly spelled modes and paths (``mkdir -p docs/notes``): it
#: makes directories and runs nothing.
_MKDIR_RE = re.compile(r"[ \t]*mkdir((?:[ \t]+[\w.@%+~/,=-]+)+)[ \t]*\Z")


def _changes_mode(piece: str) -> bool:
    """True if command *piece* is a ``_CHMOD_RE`` mode change of ``_plain_file`` paths."""
    match = _CHMOD_RE.match(piece)
    return match is not None and all(_plain_file(word) for word in match.group(1).split())


def _makes_dirs(piece: str) -> bool:
    """True if command *piece* is a ``_MKDIR_RE`` directory make of ``_plain_file`` paths.

    A path whose first component starts with ``git`` is not one: ``mkdir git``
    makes a name a bullet's ``*`` can expand to, as ``touch git`` does.
    """
    match = _MKDIR_RE.match(piece)
    if match is None:
        return False
    for word in match.group(1).split():
        while word.startswith("./"):
            word = word[2:]
        if not _plain_file(word) or re.split(r"[\\/]", word)[0].startswith("git"):
            return False
    return True


#: The names git runs as hooks (``$GIT_DIR/hooks`` by default, or ``core.hooksPath``
#: when set): a file written to a plainly named path with one of these basenames is
#: executable code git fires on commit, push, merge, checkout, rebase and the rest,
#: so its body is read as commands, not data.  git runs no other basename as a hook.
_GIT_HOOK_NAMES = frozenset(
    "applypatch-msg pre-applypatch post-applypatch pre-commit pre-merge-commit"
    " prepare-commit-msg commit-msg post-commit pre-rebase post-checkout post-merge"
    " pre-push pre-receive update proc-receive post-receive post-update"
    " reference-transaction push-to-checkout pre-auto-gc post-rewrite sendemail-validate"
    " fsmonitor-watchman post-index-change".split()
)


#: The file names ``make`` reads by default for its recipes (``make`` with no
#: ``-f``): a heredoc written to a plainly named path with one of these basenames
#: is a recipe ``make`` runs through the shell when a ``make`` sibling fires it, so
#: its body is read as commands, not data -- a markdown bullet in a recipe is a
#: glob program word, read like a literal push.  Keyed on ``make``'s own default
#: input names, so the whole class is covered, not one spelling.  Spelled in the
#: lower case the publish floor folds a command to before it reads it, so
#: ``Makefile`` and ``makefile`` both land here, as does ``GNUmakefile``.
_RUNNER_RECIPE_NAMES = frozenset(("gnumakefile", "makefile"))


#: A function definition: ``name()``, ``name ()``, or the ``function`` keyword.
_FUNCTION_DEFINITION_RE = re.compile(r"\(\s*\)|(?<![\w-])function(?![\w-])")


def _store_bodies(
    text: str, spans: "list[tuple[int, int]]", pending: "list[_Heredoc]", cut: "tuple[list, ...]"
) -> None:
    """File each body of *pending* heredocs, at its span of *text*, as command
    text, a sink body, a file body or a ``_HOOKLESS_SINKS`` body.

    Each body is filed on its own, so a quote inside one cannot reach the
    reading of the next (``cat <<'A'; bash <<'B'``).  A body is a sink body
    when its heredoc feeds a sink, and a file body when its heredoc is written
    to a file by ``_writes_file``; either only when its delimiter is quoted or
    it carries no expansion bash runs while it reads the body.
    """
    _data, bodies, sink_bodies, file_bodies, hookless_bodies = cut
    for heredoc, (start, end) in zip(pending, spans):
        span = text[start:end]
        expands = not heredoc.quoted and any(token in span for token in ("$(", "`", "${"))
        if not expands and heredoc.data:
            (hookless_bodies if heredoc.hookless else sink_bodies).append(span)
        elif not expands and heredoc.file:
            file_bodies.append(span)
        else:
            bodies.append(span)


def _heredoc_at(text: str, offset: int) -> "_Heredoc | None":
    """The heredoc whose ``<<`` operator starts at *offset*, if one does.

    A delimiter that cannot be read whole -- empty, longer than
    ``_HEREDOC_TAG_WINDOW``, or carrying a substitution bash reads as one word
    with it -- is ``_UNREADABLE_HEREDOC``.
    """
    if not text.startswith("<<", offset) or text.startswith("<<<", offset):
        return None
    if offset and text[offset - 1] == "<":
        return None
    after = offset + 2
    strip_tabs = text.startswith("-", after)
    after += 1 if strip_tabs else 0
    while after < len(text) and text[after] in " \t":
        after += 1
    window = text[after : after + _HEREDOC_TAG_WINDOW + 1]
    end = after
    broke = False
    for step in _iter_shell_chars(window):
        if step.active and step.char in _HEREDOC_TAG_BREAK:
            broke = True
            break
        end = after + step.offset + len(step.text)
    raw_tag = text[after:end]
    if not raw_tag or len(raw_tag) > _HEREDOC_TAG_WINDOW or not (broke or end == len(text)):
        return _UNREADABLE_HEREDOC
    if "$(" in raw_tag or "${" in raw_tag or "`" in raw_tag or text[end : end + 1] == "(":
        return _UNREADABLE_HEREDOC
    tag = _heredoc_delimiter(raw_tag)
    if tag is None:
        return _UNREADABLE_HEREDOC
    quoted = any(char in _fold_line_continuations(raw_tag) for char in "'\"\\")
    return _Heredoc(tag, strip_tabs, quoted=quoted)


def _heredoc_delimiter(raw: str) -> "str | None":
    """The delimiter bash reads from raw heredoc word *raw*: quote removal only.

    An unquoted backslash is removed; inside double quotes one is removed only
    before ``$``, a backtick, ``"``, ``\\`` or a newline.  ``$'...'`` is read
    as its literal text and ``$"..."`` as double quotes.  ``$`` is otherwise
    literal, since bash expands nothing in a delimiter.  None when a ``$'...'``
    span carries an escape, which this reading does not decode.
    """
    out: list[str] = []
    ansi: "list[str] | None" = None
    state = 0
    dollars = 0
    for step in _iter_shell_chars(raw):
        quote_syntax = len(step.text) == 1 and step.char in "'\"" and step.state != state
        state = step.state
        if ansi is not None:
            if "\\" in step.text:
                return None
            if not quote_syntax:
                ansi.append(step.text)
                continue
            out.extend(ansi)
            ansi = None
        elif quote_syntax:
            if step.state != 0 and dollars % 2:
                out.pop()
                ansi = [] if step.ansi else None
        elif len(step.text) == 2:
            keep_both = step.state == 2 and step.char not in '$`"\\\n'
            # A backslash-newline is a line continuation: both characters go.
            out.append("" if step.char == "\n" else step.text if keep_both else step.char)
        else:
            out.append(step.char)
        dollars = dollars + 1 if len(step.text) == 1 and step.char == "$" and not state else 0
    return "".join(out)


def _heredoc_bodies_end(
    text: str, start: int, pending: "list[_Heredoc]", spans: "list[tuple[int, int]] | None" = None
) -> int:
    """Where the bodies of *pending* heredocs, starting at *start*, end.

    Each body ends after the first line that is exactly its delimiter (leading
    tabs removed for ``<<-``).  In the body of an unquoted delimiter a line
    ending in an unescaped backslash continues on the next line before the
    comparison, as bash joins it.  A body whose delimiter line never comes
    runs to the end of *text*, as bash reads it.  ``_UNREADABLE`` for a ``<<-``
    body of an unquoted delimiter that continues a line, whose tab stripping
    this reading does not reproduce.  *spans*, when given, receives each
    body's own span, delimiter line included, so each can be read on its own.
    """
    pos = start
    for heredoc in pending:
        body_start = pos
        while True:
            if pos >= len(text):
                if spans is not None:
                    spans.append((body_start, len(text)))
                return len(text)
            line = ""
            while True:
                newline = text.find("\n", pos)
                line_end = len(text) if newline == -1 else newline
                physical = text[pos:line_end]
                pos = line_end + 1
                trailing = len(physical) - len(physical.rstrip("\\"))
                if heredoc.quoted or newline == -1 or trailing % 2 == 0:
                    line += physical
                    break
                if heredoc.strip_tabs:
                    return _UNREADABLE
                line += physical[:-1]
            if (line.lstrip("\t") if heredoc.strip_tabs else line) == heredoc.tag:
                break
        if spans is not None:
            spans.append((body_start, min(pos, len(text))))
    return min(pos, len(text))
