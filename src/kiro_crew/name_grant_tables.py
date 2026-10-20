"""The fixed name tables a program named in a command line is judged against.

The characters that make a program token expand, the programs that run another program
named in their arguments, the inherited shell-preload variables (with
:func:`_inherited_preload`), the inert builtins, and the PowerShell 5.1 tables: its
inert builtins, default aliases and core cmdlets, the characters it expands, and the
extensions Windows runs.

An owner module of :mod:`kiro_crew.name_grant`, the one import path: callers, tests and
documents reach every name here as ``kiro_crew.name_grant.<name>``. It imports no other
owner, and nothing here reads a name the tests patch through the facade
(``test/test_name_grant_facade.py`` holds it to that). A new table entry goes here.
"""

from __future__ import annotations

import os

#: Characters that make a PROGRAM token something other than a literal name --
#: the shell expands them, so what runs is decided after this check reads it.
_EXPANDING_CHARS = ("$", "`", "*", "?", "[")


#: Programs whose JOB is to run another program named in their arguments. The
#: walk sees ``env head file`` as the single program ``env`` with two operands, so
#: it would vouch for ``/usr/bin/env`` while ``head`` is resolved from ``PATH`` at
#: exec time -- the same substitution the shebang chain closes, reached through
#: argv instead. Each one has its own flag grammar (``env -i -u X CMD``,
#: ``timeout -s KILL 5 CMD``, ``xargs -I{} CMD``), so rather than model them the
#: walk refuses: a grant naming a dispatcher cannot identify what it dispatches.
_DISPATCHERS = frozenset(
    {
        # Command SHELLS. `sh -c 'head file'` runs an arbitrary command string, so
        # vouching for `/bin/sh` says nothing about what executes: a grant naming
        # a shell is a grant to run anything, which is a decision for the approval
        # card, not for a name check. Interpreters that take CODE (`python3 -c`)
        # are deliberately NOT
        # here -- the read-only tier already restricts them through its own
        # denied-programs list, and listing them would refuse `python3 --version`,
        # which that tier grants on purpose.
        "sh",
        "bash",
        "dash",
        "zsh",
        "ksh",
        "csh",
        "tcsh",
        "ash",
        "fish",
        "busybox",
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        # Shell BUILTINS that CHANGE HOW A LATER NAME RESOLVES. `export
        # PATH=/agent/bin:$PATH && head file` re-points the lookup for every
        # command after it, and `hash`/`alias` re-point one name directly -- so
        # the resolution this check performs describes the PREVIOUS state, not
        # the one the later command will use. They cannot be evaluated by
        # resolving a name, so a line carrying one is refused. (A bare
        # `PATH=... cmd` PREFIX is caught separately by `_EXEC_ENV_VARS`.)
        "export",
        "hash",
        "alias",
        "unalias",
        "declare",
        "typeset",
        "readonly",
        "local",
        "set",
        "shopt",
        # Builtins that WRITE A VARIABLE from their arguments, so they can set
        # `PATH` without an assignment token: `printf -v PATH /writable; payload`
        # leaves the walk checking `payload` against the previous search path.
        "printf",
        "read",
        "mapfile",
        "readarray",
        "getopts",
        "let",
        # Shell BUILTINS that hand off to a program named in their arguments.
        # These are the sharpest case because `shutil.which` cannot see them at
        # all: an unresolvable name is otherwise treated as "nothing to shadow,
        # nothing to vouch for", so `exec head file` passed while `head` was
        # resolved from PATH at exec time and never inspected.
        "exec",
        "eval",
        "builtin",
        "source",
        ".",
        # External wrappers whose whole job is to run something else.
        "env",
        "nohup",
        "nice",
        "ionice",
        "chrt",
        "stdbuf",
        "unbuffer",
        "setsid",
        "timeout",
        "xargs",
        "command",
        "watch",
        "parallel",
        "sudo",
        "doas",
        "su",
        "runuser",
        "pkexec",
        "systemd-run",
        "script",
        "strace",
        "ltrace",
    }
)


#: Windows programs and PowerShell built-ins whose whole job is to run a program
#: named in their arguments -- the Windows shape of ``env`` and ``xargs``.
#: ``start`` and ``saps`` are Start-Process; ``iex``/``icm`` evaluate a string or
#: script block; ``ii`` opens a file with its association; the rest are system
#: executables that spawn what they are told to. Held separately from
#: :data:`_DISPATCHERS` and consulted only when :data:`platform_compat.IS_WINDOWS`
#: is true, because the names collide with ordinary programs on POSIX -- ``iex``
#: is Elixir's REPL, ``start`` is a program a user is free to install, and so on.
#: Matched lower-cased and without extension on Windows (``_program_key``),
#: because ``POWERSHELL.EXE`` runs the same program as ``powershell``.
_WINDOWS_DISPATCHERS = frozenset(
    {
        "start",
        "saps",
        "start-process",
        "iex",
        "invoke-expression",
        "icm",
        "invoke-command",
        "ii",
        "invoke-item",
        "call",
        "forfiles",
        "wmic",
        "rundll32",
        "mshta",
        "cscript",
        "wscript",
        "msiexec",
        "schtasks",
        "wsl",
        "runas",
        "explorer",
        "conhost",
        "powershell_ise",
    }
)


#: Environment variables that, when INHERITED (not written in the command line),
#: make bash run code before the named program and can define a shell FUNCTION
#: that shadows it. `BASH_ENV=/writable/rc` holding `head() { payload; }` means
#: `bash -c 'head file'` runs the function, while this check resolves the name to
#: `/usr/bin/head` and calls it a trusted system program.
#:
#: This is the same threat as the command-line `BASH_ENV=...` prefix that
#: `_EXEC_ENV_VARS` already refuses, arriving through the process environment
#: instead. Neither is visible in the command line, so the check reads its own
#: environment -- the one a child shell inherits by default.
_ENV_PRELOAD_VARS = ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS")


#: An EXPORTED SHELL FUNCTION, which shadows a program name directly rather than
#: via a file bash is told to source. Bash exports `head() { payload; }` as an
#: environment entry and re-imports it in the child, so `bash -c 'head file'`
#: runs the function while this check resolves the name to `/usr/bin/head` and
#: calls it a trusted system program -- the same unsoundness as
#: :data:`_ENV_PRELOAD_VARS`, needing no writable file at all.
#:
#: The key is matched on its `BASH_FUNC_` PREFIX alone, deliberately: the suffix
#: has been spelled `()` and `%%` by different bash versions, and pinning a
#: spelling would hand the bypass back on any build that picks another one.
#:
#: The value form is matched too, for the pre-2014 spelling where the key is the
#: bare function name and only the `() {` value marks it. Supported bash no
#: longer imports that, so this is belt-and-braces rather than the live vector.
_BASH_FUNC_KEY_PREFIX = "BASH_FUNC_"
_BASH_FUNC_VALUE_PREFIX = "() {"


def _inherited_preload() -> str | None:
    """The inherited variable that makes a name-based grant unsound, if any."""

    for var in _ENV_PRELOAD_VARS:
        if os.environ.get(var):
            return var
    for key, value in os.environ.items():
        if key.startswith(_BASH_FUNC_KEY_PREFIX) or value.startswith(_BASH_FUNC_VALUE_PREFIX):
            # The FAMILY, not the key: the key embeds an attacker-chosen function
            # name and this string reaches a log sink and the dashboard card.
            return f"{_BASH_FUNC_KEY_PREFIX}*"
    return None


#: Shell builtins that resolve to NO file and are still allowed, because they can
#: neither run a program named in their arguments nor change how a later name
#: resolves. Anything not here that fails to resolve is refused -- see the
#: `not found` branch in :func:`_program_refusal` for why the default is refuse.
#:
#: Deliberately NOT here, and each for a measured reason: `time` and `command`
#: run a program named in their arguments; `trap` and `enable` install code that
#: runs later (`trap 'payload' DEBUG` before every command, `enable -f` loading a
#: shared object as a builtin); `:` is here because it is a true no-op, while
#: `eval`/`exec`/`source`/`.` are the sharpest dispatchers there are.
_INERT_BUILTINS = frozenset(
    {
        ":",
        "cd",
        "echo",
        "pwd",
        "true",
        "false",
        "test",
        "[",
        "wait",
    }
)

# ── Windows: PowerShell's resolution order ──
#
# The shell kiro-cli spawns on Windows is PowerShell (`powershell -Command`), and
# PowerShell resolves a command word in a fixed order: alias, function, cmdlet,
# then the executables on `PATH`. The first three come from the session itself,
# so a name in one of those tables runs a BUILT-IN whatever `PATH` holds --
# `sort` is Sort-Object with `sort.exe` sitting right there in System32, and
# `curl` is Invoke-WebRequest. Resolving such a name from `PATH` would vouch for
# a file the shell never runs. The tables below are the default alias and
# function names of Windows PowerShell 5.1 (`Get-Alias`, plus the functions a
# `-NoProfile` session defines), so a name in them is judged as the built-in it
# is: allowed when the built-in is inert, refused otherwise. Module auto-loading
# does not enter into this: measured, an application found on `PATH` wins over
# an auto-loadable module function, and a name found nowhere is refused anyway.

#: The Windows shell whose built-in set the tables below mirror, as the argv of a
#: session equivalent to the one the command will run in. ONE spelling, because
#: two places have to mean the same shell: these tables, and the derivation check
#: that enumerates a live session to prove they are not stale.
#:
#: ``-NoProfile`` is here and NOT in what kiro-cli spawns, deliberately. The
#: tables record the shell's DEFAULT names, which is what a profile-free session
#: reports; a profile's own functions are not table material because a profile
#: existing refuses every grant outright (:func:`windows_environment_refusal`).
#:
#: This is also ``name_grant``'s single point of exposure to kiro-cli's choice of
#: shell (kirodotdev/Kiro#9537). If that ever becomes pwsh 7, changing it here
#: re-points the derivation check at the new shell, and the check then fails on
#: every name whose behaviour the tables get wrong -- which is the loud failure a
#: hand-written mirror of another program's state needs.
MODELLED_WINDOWS_SHELL: tuple[str, ...] = ("powershell", "-NoProfile", "-NonInteractive")

#: PowerShell default aliases and session functions that are INERT in this
#: module's sense -- they neither run a program named in their arguments nor
#: change how a later name resolves -- plus the cmdlets they stand for, so a
#: grant written in PowerShell's own idiom (`Get-ChildItem *`) works too. This
#: is the Windows counterpart of :data:`_INERT_BUILTINS`, held to the same
#: standard: `where`/`foreach`/`%` take script blocks and are absent; `ri`,
#: `rni`, `ni`, `si` reach the `alias:` and `function:` drives and are absent;
#: `more`, `help` and `man` pipe through an external pager and are absent.
#:
#: Taking a script block in ANY parameter is disqualifying, not just in the
#: pipeline position `where`/`foreach` use. A calculated property is a script
#: block PowerShell evaluates once per input object, so `sort`, `select`,
#: `group`, `compare` and the whole `format-*` family run whatever their
#: `-Property` or `-GroupBy` argument contains and are absent for the same
#: reason. Measured on 5.1 with object input (scalar input makes the format
#: engine ignore `-Property`, which hides this): the block runs 2-3 times per
#: two-object pipeline for each of them, while `measure-object` types
#: `-Property` as `String[]`, so it coerces the block to its source text and
#: never evaluates it -- it stays. A name is judged as a whole command, so
#: `select` is absent even though its `-ExpandProperty` is `String`-typed: the
#: same command's `-Property` evaluates, and the check sees the name, not which
#: parameter a given line happens to use.
_WINDOWS_INERT_BUILTINS = frozenset(
    {
        "cat",
        "gc",
        "type",
        "get-content",
        "ls",
        "dir",
        "gci",
        "get-childitem",
        "pwd",
        "gl",
        "get-location",
        "cd",
        "sl",
        "chdir",
        "cd..",
        "set-location",
        "pushd",
        "popd",
        "echo",
        "write",
        "write-output",
        "write-host",
        "measure",
        "measure-object",
        "gm",
        "get-member",
        "oh",
        "out-host",
        "out-string",
        "gi",
        "get-item",
        "gp",
        "get-itemproperty",
        "gpv",
        "get-itempropertyvalue",
        "sls",
        "select-string",
        "gps",
        "ps",
        "get-process",
        "gsv",
        "get-service",
        "gv",
        "get-variable",
        "gal",
        "get-alias",
        "gcm",
        "get-command",
        "ghy",
        "history",
        "h",
        "get-history",
        "gdr",
        "get-psdrive",
        "gu",
        "get-unique",
        "rvpa",
        "resolve-path",
        "cvpa",
        "convert-path",
        "test-path",
        "get-date",
        "join-path",
        "split-path",
        "cls",
        "clear",
        "clear-host",
        "sleep",
        "start-sleep",
    }
)

#: Every default alias and session function of Windows PowerShell 5.1, lower-cased
#: (`Get-Alias | % Name` plus `Get-ChildItem function: | % Name`, on 5.1.26100).
#: An ALIAS and a FUNCTION both resolve ahead of an application in PowerShell's
#: precedence, unconditionally and with no module to load first, which is why the
#: two sets share one table. A name here that is not in
#: :data:`_WINDOWS_INERT_BUILTINS` resolves to a built-in PowerShell runs INSTEAD
#: of any same-named file on `PATH`, so it is refused rather than resolved:
#: vouching for `sc.exe` when the shell runs Set-Content would be answering the
#: wrong question. PowerShell 7 drops a few of these (`curl`, `wget`, `sc`);
#: keeping them costs a prompt there, never a wrong answer. Spelling a program
#: WITH its extension (`sort.exe`) bypasses the alias in PowerShell, and
#: correspondingly bypasses this table.
#:
#: The set is not asserted, it is CHECKED: on a Windows host
#: ``test_the_builtin_tables_cover_every_name_this_shell_resolves`` enumerates the
#: live shell and fails if any name it resolves would reach the `PATH` walk
#: unrefused. That test is what caught `cfs` (-> ConvertFrom-String) and the
#: `get-verb` function, both of which an earlier hand-transcribed list had missed.
_POWERSHELL_DEFAULT_ALIASES = frozenset("""
    % ? ac asnp cat cd cfs chdir clc clear clhy cli clp cls clv cnsn compare copy
    cp cpi cpp curl cvpa dbp del diff dir dnsn ebp echo epal epcsv epsn erase
    etsn exsn fc fhx fl foreach ft fw gal gbp gc gci gcm gcs gdr get-verb ghy gi
    gjb gl gm gmo gp gps gpv group gsn gsnp gsv gu gv gwmi h history icm iex ihy
    ii ipal ipcsv ipmo ipsn irm ise iwmi iwr kill lp ls man md measure mi mount
    move mp mv nal ndr ni nmo npssc nsn nv ogv oh popd ps pushd pwd r rbp rcjb
    rcsn rd rdr ren ri rjb rm rmdir rmo rni rnp rp rsn rsnp rujb rv rvpa rwmi
    sajb sal saps sasv sbp sc select set shcm si sl sleep sls sort sp spjb spps
    spsv start sujb sv swmi tee trcm type wget where wjb write
    cd.. help mkdir more oss pause prompt tabexpansion2 importsystemmodules
    """.split())
# `cd\` and the 26 drive functions `a:`..`z:` are 5.1 session functions too, but a
# backslash or a colon makes each of them a PATH in this walk, and each is refused
# as a relative one before any table is consulted (measured, and pinned by the
# coverage test above, which accepts either a table hit or an earlier refusal).

#: Every command exported by the three modules that ship AS PowerShell itself --
#: `Microsoft.PowerShell.Core`, `.Management`, `.Utility` -- lower-cased
#: (`Get-Command -Module <those three> -CommandType Cmdlet,Function`, 258 names on
#: 5.1.26100). A name here that is not in :data:`_WINDOWS_INERT_BUILTINS` is
#: refused, for a reason the alias table does not cover: these are FULL cmdlet
#: names, not aliases, and whether one beats a same-named file on `PATH` depends
#: on what is already loaded in the session, which is not a property of the
#: command being judged.
#:
#: Measured on 5.1: a session `powershell -Command` starts with `Utility` loaded
#: and `Management` NOT, and there a `Set-Content.cmd` on `PATH` DOES win -- an
#: auto-loadable module loses to an application, as ``name_grant``'s module docstring says. But
#: `Core` is always loaded, so `Where-Object` or `Invoke-Command` beats any file
#: unconditionally; and one command from `Management` or `Utility` earlier in the
#: same line auto-loads that whole module, after which every later name in it
#: beats `PATH` too. Resolving such a name to a file would vouch for a file the
#: shell may not run, so the whole class is refused instead.
#:
#: Exactly these three modules and no other shipped module (`NetAdapter`,
#: `Defender`, `.Security`, `.Diagnostics` ...): a module is auto-loaded only by
#: one of its OWN commands, and the only names this walk lets past without a
#: refusal are the inert ones, which are all `Core`/`Management`/`Utility`. So no
#: permitted prefix can load anything else, and a lone `Get-NetAdapter` runs the
#: file on `PATH` (measured) and stays resolvable. Spelling the extension
#: (`set-content.exe`) is not a cmdlet name in PowerShell and is not one here.
_POWERSHELL_CORE_COMMANDS = frozenset("""
    add-computer add-content add-history add-member add-pssnapin add-type
    checkpoint-computer clear-content clear-eventlog clear-history clear-item
    clear-itemproperty clear-recyclebin clear-variable compare-object
    complete-transaction connect-pssession convertfrom-csv convertfrom-json
    convertfrom-sddlstring convertfrom-string convertfrom-stringdata convert-path
    convert-string convertto-csv convertto-html convertto-json convertto-xml
    copy-item copy-itemproperty debug-job debug-process debug-runspace
    disable-computerrestore disable-psbreakpoint disable-psremoting
    disable-pssessionconfiguration disable-runspacedebug disconnect-pssession
    enable-computerrestore enable-psbreakpoint enable-psremoting
    enable-pssessionconfiguration enable-runspacedebug enter-pshostprocess
    enter-pssession exit-pshostprocess exit-pssession export-alias export-clixml
    export-console export-csv export-formatdata export-modulemember
    export-pssession foreach-object format-custom format-hex format-list
    format-table format-wide get-alias get-childitem get-clipboard get-command
    get-computerinfo get-computerrestorepoint get-content get-controlpanelitem
    get-culture get-date get-event get-eventlog get-eventsubscriber get-filehash
    get-formatdata get-help get-history get-host get-hotfix get-item
    get-itemproperty get-itempropertyvalue get-job get-location get-member
    get-module get-process get-psbreakpoint get-pscallstack get-psdrive
    get-pshostprocessinfo get-psprovider get-pssession get-pssessioncapability
    get-pssessionconfiguration get-pssnapin get-random get-runspace
    get-runspacedebug get-service get-timezone get-tracesource get-transaction
    get-typedata get-uiculture get-unique get-variable get-wmiobject group-object
    import-alias import-clixml import-csv import-localizeddata import-module
    import-powershelldatafile import-pssession invoke-command invoke-expression
    invoke-history invoke-item invoke-restmethod invoke-webrequest
    invoke-wmimethod join-path limit-eventlog measure-command measure-object
    move-item move-itemproperty new-alias new-event new-eventlog new-guid
    new-item new-itemproperty new-module new-modulemanifest new-object new-psdrive
    new-psrolecapabilityfile new-pssession new-pssessionconfigurationfile
    new-pssessionoption new-pstransportoption new-service new-temporaryfile
    new-timespan new-variable new-webserviceproxy out-default out-file
    out-gridview out-host out-null out-printer out-string pop-location
    push-location read-host receive-job receive-pssession
    register-argumentcompleter register-engineevent register-objectevent
    register-pssessionconfiguration register-wmievent remove-computer remove-event
    remove-eventlog remove-item remove-itemproperty remove-job remove-module
    remove-psbreakpoint remove-psdrive remove-pssession remove-pssnapin
    remove-typedata remove-variable remove-wmiobject rename-computer rename-item
    rename-itemproperty reset-computermachinepassword resolve-path restart-computer
    restart-service restore-computer resume-job resume-service save-help
    select-object select-string select-xml send-mailmessage set-alias set-clipboard
    set-content set-date set-item set-itemproperty set-location set-psbreakpoint
    set-psdebug set-pssessionconfiguration set-service set-strictmode set-timezone
    set-tracesource set-variable set-wmiinstance show-command
    show-controlpanelitem show-eventlog sort-object split-path start-job
    start-process start-service start-sleep start-transaction stop-computer
    stop-job stop-process stop-service suspend-job suspend-service tee-object
    test-computersecurechannel test-connection test-modulemanifest test-path
    test-pssessionconfigurationfile trace-command unblock-file undo-transaction
    unregister-event unregister-pssessionconfiguration update-formatdata
    update-help update-list update-typedata use-transaction wait-debugger
    wait-event wait-job wait-process where-object write-debug write-error
    write-eventlog write-host write-information write-output write-progress
    write-verbose write-warning
    """.split())

#: Characters that make a PROGRAM token something other than a literal name on
#: Windows: :data:`_EXPANDING_CHARS` plus cmd.exe's `%VAR%` and `^` escape --
#: kept although PowerShell is the modelled shell, because a program named
#: through either cannot be a literal name in any shell -- and PowerShell's `@`
#: splatting / array prefix.
_WINDOWS_EXPANDING_CHARS = _EXPANDING_CHARS + ("%", "^", "@")

#: Extensions Windows runs DIRECTLY: an image the loader maps (`.exe`, `.com`),
#: a batch file `COMSPEC` interprets, or a script the running PowerShell reads
#: itself. Anything else that `PATHEXT` lets the shell resolve (`.py`, `.js`,
#: `.vbs` ...) is launched through the program the REGISTRY associates with the
#: extension, under `HKCU` as readily as `HKLM` -- so the interpreter is chosen
#: by a key the user (and so the agent) can write, not by the file this check
#: can pin. Such a hit is refused.
_WINDOWS_RUNNABLE_EXTENSIONS = frozenset({".exe", ".com", ".bat", ".cmd", ".ps1"})

#: The two of those that `COMSPEC` interprets, so the interpreter has to be the
#: system `cmd.exe` for the pin on the script to mean anything.
_COMSPEC_SCRIPT_EXTENSIONS = frozenset({".bat", ".cmd"})

#: `PATHEXT` when the environment does not supply one -- Windows' own default.
_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"
