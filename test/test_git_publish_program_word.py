"""The git-publish floor reads the PROGRAM word of every command it can reach.

A push run under a program word the shell resolves at run time -- a quoted
expansion (``"$G"``), a glob that can name git (``g?t``), a command
substitution -- is found wherever it sits: at the top of the line, after a
newline, a glued or background separator, inside a compound construct
(``if``/``while``/``for``/``select`` bodies, ``case`` arms, subshells, brace
groups, function bodies), behind a precommand and its options or a
redirection, or in a nested shell payload.

When the program word reaches exec as ONE word (a double-quoted parameter
expansion) and the words up to ``push`` are literal, git's argv is known, so the
push is judged by its target exactly like a literal git push: a protected or
bare target is denied, a feature target is allowed. Any other runtime-program
push is unverifiable.

No other command answers for such a push: not a sibling command, and not a
feature push carried among its own arguments.

A program whose BASENAME is literal (``"$SDK/platform-tools/adb"``) is that
program, not a runtime-resolved one, and commands that only MENTION a push stay
allowed.
"""

import pytest

from kiro_crew.security import (
    _is_git_publish,
    _is_push_to_protected_branch,
    is_denied,
    publish_program_word,
)

# "pus" + "h" keeps a literal blocked command out of the test source.
P = "pus" + "h"
GIT_FEATURE = f"git {P} origin feature"
UNVERIFIABLE = "git-publish-target-unverifiable"
PROTECTED = "git-publish-push-protected-branch-name"
BARE = "git-publish-push-bare"

#: One-word program words: git's argv is known, so the TARGET decides.
JUDGED_BY_TARGET = [
    (f'"$G" {P} origin main', PROTECTED),
    (f'"$G" -C . {P} origin main', PROTECTED),
    (f'"$G" {P} origin HEAD:main', PROTECTED),
    (f'"$G" {P}', BARE),
    (f'G=git "$G" {P} origin main', PROTECTED),
    (f'"/usr/bin/$G" {P} origin main', PROTECTED),
    (f'"$HOME\\bin\\git.exe" {P} origin main', PROTECTED),
    (f'& "$env:ProgramFiles\\Git\\cmd\\git.exe" {P} origin main', PROTECTED),
    # A redirection target ends at the next unquoted operator, so a chained
    # ``</dev/null>`` leaves its last target to the next word, as bash reads it.
    (f'"${{1:-git}}" </dev/null> /dev/null {P} origin main', PROTECTED),
    (f'"$G" </dev/null> /dev/null {P} origin main', PROTECTED),
    (f'"$G" {P}>log> /dev/null origin main', PROTECTED),
    # A ``>|`` noclobber redirection is one operator, not a pipe: the program
    # word before it and ``push`` after it are one command.
    (f'"${{1:-git}}" >|/dev/null {P} origin main', PROTECTED),
    (f'"$G" >| /dev/null {P} origin main', PROTECTED),
    (f"git >|/dev/null {P} origin main", PROTECTED),
    (f"git 2>|/dev/null {P} origin main", PROTECTED),
    # A literal git with a redirection before ``push`` is judged here too.
    (f"git </dev/null> /dev/null {P} origin main", PROTECTED),
    (f"git </dev/null {P} origin main", PROTECTED),
    (f"git 2>/dev/null {P} origin main", PROTECTED),
    (f"sudo git </dev/null {P} origin main", PROTECTED),
    (f"if true; then git >/dev/null {P} origin main; fi", PROTECTED),
    (f"/usr/bin/git </dev/null {P} origin main", PROTECTED),
    (f"git </dev/null {P}", BARE),
    (f'"$G" --git-dir="$REPO/.git" {P} origin main', PROTECTED),
    (f'"$G" --work-tree="$REPO" {P} origin main', PROTECTED),
]

#: Program words whose expansion can add words, or whose basename is resolved by
#: the shell: git's argv is not known, so the push is unverifiable.
UNVERIFIABLE_PROGRAM_WORDS = [
    f"g?t {P} origin feature",
    f"/usr/bin/g?t {P} origin feature",
    f"/usr/bin/[g]it {P} origin feature",
    f"$(echo git) {P} origin feature",
    f"`echo git` {P} origin feature",
    f"$G {P} origin feature",
    f'"$@" {P} origin feature',
    f"{{git,}} {P} origin feature",
    f"$(printf %s git) -C $(printf %s .) {P} origin main",
    f"if true; then $(printf %s git | cat) {P} origin main; fi",
    # A literal tail a git program name can end in leaves the word able to name git.
    f'"$X"\\git {P} origin main',
    f'"$X"t {P} origin main',
    f'"$X".exe {P} origin main',
    f'"$(echo git)x" {P} origin main',
    # Split, a variable path prefix may be git with options of its own that move
    # the written target (``-c remote.origin.push=feature:main``).
    f"X='git -c remote.origin.push=feature:main -c a.b='; $X/adb {P} origin feature",
    f"$ANDROID_HOME/platform-tools/adb {P} app-debug.apk /data/local/tmp/",
    f"${{VENV}}/bin/dvc {P} --repo=origin -r",
    f'"$X\\*" {P} origin main',
]

#: Every program position is read; each row runs a protected push.
PROGRAM_POSITIONS = [
    f'echo ok\n"$G" {P} origin main',
    f'G=git; A+=x "$G" {P} origin main',
    f"G='git -C .'; $G/repo {P} origin main",
    f'G=git; echo "$(true # "\n)"; "$G" {P} origin main',
    f'x="$(echo "\'")"; "$G" {P} origin main',
    f'x="`echo "\'"`"; "$G" {P} origin main',
    f'echo "$(printf \'"\')"; "$G" {P} origin main',
    f'x="$(cat <<\'EOF\'\n"\nEOF\n)"; "$G" {P} origin main',
    f'x="${{y:-"\'"}}"; "$G" {P} origin main',
    f'x="$("$G" {P} origin main)"',
    f'G=git; env -iu UNUSED "$G" {P} origin main',
    f'G=git; sudo -Eu root "$G" {P} origin main',
    # Lowercased, a no-value option reads like its value-taking other case, so
    # both readings are taken.
    f'xargs -l "$G" {P} origin main',
    f'xargs -e "$G" {P} origin main',
    f'xargs -p "$G" {P} origin main',
    f'xargs -i "$G" {P} origin main',
    f'sudo -H "$G" {P} origin main',
    f'sudo -P "$G" {P} origin main',
    f'sudo -p prompt "$G" {P} origin main',
    # A word that can expand to no word leaves program position to the next.
    f'G=git; set --; "$@$@" "$G" {P} origin main',
    f'G=git; ${{x:-${{y:-}}}} "$G" {P} origin main',
    f'G=git; shopt -s nullglob extglob; @(nope) "$G" {P} origin main',
    f'G=git; "${{@:2}}${{a[@]}}" "$G" {P} origin main',
    # A word that may vanish may instead be a program running the words after
    # it, so a publish after it has no known argv.
    f'set -- git; "$@" "$G" {P} origin feature',
    f'set -- bash -c; "$@" "${{3:-git {P} origin main}}" {P} origin feature',
    # An open precommand's operand is not shell syntax.
    f'flock function "${{1:-git}}" {P} origin main',
    f"/mingw64/bin/g?t.exe {P} origin main",
    # A runtime program may be ``eval``, which runs a publish an argument carries.
    f"\"${{HOME:0:0}}eval\" {P} origin feature ';git {P} origin main'",
    f"\"$G\" {P} origin main eval '{GIT_FEATURE}'",
    f'"$G" {P} origin main eval {GIT_FEATURE}',
    f"read -r g <<<eval; \"$g\" {P} origin 'x;git' {P}",
    # A runtime word not followed by ``push`` may be a dispatcher (``env``).
    f'set -- env git; "$1" "$2" {P} origin main',
    # A POSIX bracket expression in a glob program word can name git.
    f"/usr/bin/g[[:lower:]]t {P} origin main",
    f"g[[=i=]]t {P} origin main",
    # Lowercased, ``-C`` cannot be told from ``-c``, whose value can carry
    # ``remote.origin.push`` and redirect a source-only refspec.
    f'G=git; cfg=remote.origin.{P}=feature:main; "$G" -c "${{cfg}}" {P} origin feature',
    f'"$GIT" -C "$REPO" {P} origin feat/example',
    # A prefix word naming ``push`` may be a shell's ``-c`` payload.
    f"G=$(printf bash); \"$G\" -c 'git {P} origin main' {P} origin feature",
    f'G=git; command -p "$G" {P} origin main',
    f'a[0]=1 "$G" {P} origin main',
    f'G=git; echo \\>&"$G" {P} origin main',
    f'G=git; echo \\<&"$G" {P} origin main',
    f"echo '>'&\"$G\" {P} origin main",
    f'set -e\nG=git\n"$G" {P} origin main',
    f'x=1;"$G" {P} origin main',
    f'cd x;"$G" {P} origin main',
    f'cd x&&"$G" {P} origin main',
    f'cd x||"$G" {P} origin main',
    f'true|"$G" {P} origin main',
    f'true & "$G" {P} origin main',
    f'true &"$G" {P} origin main',
    f'true |& "$G" {P} origin main',
    f"set -- git; echo '`' & \"$1\" {P} origin main",
    f'"$G" \\\n{P} origin main',
    # A continuation-split publish is judged on the folded source, so an
    # expansion in its target (a comma/range brace, a substitution, a backtick)
    # is unverifiable there just as the per-segment reader finds it unsplit.
    f'"$G" \\\n{P} origin feat:{{main,x}}',
    f'"$G" \\\n{P} origin `echo main`',
    f'"$G" \\\n{P} origin feat:$(echo main)',
    f'2>/dev/null "$G" {P} origin main',
    f'>out "$G" {P} origin main',
    f'"$G" 2>&1 {P} origin main',
    f'"$G" > log {P} origin main',
    "\"$G\" p'ush' origin main",
    f'sudo "$G" {P} origin main',
    f'sudo -u root "$G" {P} origin main',
    f'env -i "$G" {P} origin main',
    f'env -u X "$G" {P} origin main',
    f'exec -a x "$G" {P} origin main',
    f'nice -n 5 "$G" {P} origin main',
    f'timeout 5 "$G" {P} origin main',
    f'setsid "$G" {P} origin main',
    f'ionice -c3 "$G" {P} origin main',
    f'stdbuf -o0 "$G" {P} origin main',
    f'xargs "$G" {P} origin main',
    f'xargs -I{{}} "$G" {P} origin main',
    f'coproc "$G" {P} origin main',
    f'command "$G" {P} origin main',
    f'time -p "$G" {P} origin main',
    f'case x in x) "$G" {P} origin main;; esac',
    f'set -- git; case x in x)"$1" {P} origin main;; esac',
    f'case x in (x) "$G" {P} origin main;; esac',
    f'if true; then "$G" {P}; fi',
    f"if true; then $G {P} origin main; fi",
    f'if "$G" {P} origin main; then :; fi',
    f'if false; then :; else "$G" {P}; fi',
    f'for i in 1; do "$G" {P}; done',
    f'while true; do "$G" {P} origin main; done',
    f'until false; do "$G" {P} origin main; done',
    f'select x in a; do "$G" {P} origin main; done',
    f'if true\nthen\n"$G" {P} origin main\nfi',
    f'( "$G" {P} origin main )',
    f'("$G" {P} origin main)',
    f'{{ "$G" {P} origin main; }}',
    f'f() {{ "$G" {P} origin main; }}; f',
    f'f(){{ "$G" {P} origin main;}};f',
    f'function f {{ "$G" {P} origin main; }}; f',
    f"if true; then `echo git` {P} origin main; fi",
    f'! "$G" {P} origin main',
    f'time "$G" {P} origin main',
    f'"$G" {P} origin main ; {GIT_FEATURE}',
    f'"$G" {P} origin main && {GIT_FEATURE}',
    f'{GIT_FEATURE} ; "$G" {P} origin main',
    f'if true; then "$G" {P}; fi; {GIT_FEATURE}',
    f'case x in x) "$G" {P} origin main;; esac; {GIT_FEATURE}',
    f'( "$G" {P} origin main ); {GIT_FEATURE}',
    f'{{ "$G" {P} origin main; }} && {GIT_FEATURE}',
    f'"$G" {P} origin feature & git log & git {P} origin main',
    f'"$G" {P} origin feature & git {P} origin main',
    f"bash -c '\"$G\" {P} origin main'",
    f"bash -c '\"$G\" {P} origin main' && {GIT_FEATURE}",
    f"eval '\"$G\" {P} origin main'",
    f"sh -c 'if true; then \"$G\" {P}; fi'",
    f"\"$G\" {P} origin main bash -c '{GIT_FEATURE}'",
    f'"$G" {P} origin main <({GIT_FEATURE})',
    f'if true; then /usr/bin/g"i"[t] {P} origin main; fi',
    f"g'i'[t] {P} origin main",
    f"g\\i[t] {P} origin main",
    f'G=git; # "\n"$G" {P} origin main',
    f'# don\'t {P} yet\n"$G" {P} origin main',
    f'echo hi # it\'s\n"$G" {P} origin main',
    f'# trailing \\\n"$G" {P} origin main',
    f"cat <<'EOF'\nnote\nEOF\n\"$G\" {P} origin main",
    f"cat <<'EOF' ; \"$G\" {P} origin main\nnote\nEOF",
    f"bash <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"cat <<'EOF' | bash\n\"$G\" {P} origin main\nEOF",
    f"cat > x.sh <<'EOF' | bash\n\"$G\" {P} origin main\nEOF",
    f"bash <(cat <<'EOF'\n\"$G\" {P} origin main\nEOF\n)",
    f"bash <(cat >&2 <<'EOF'\n\"$G\" {P} origin main\nEOF\n)",
    f"bash <(cat >/dev/stdout <<'EOF'\n\"$G\" {P} origin main\nEOF\n)",
    f'cat <<EOF\n$("$G" {P} origin main)\nEOF',
    f"cat <<'EOF'\n\"$G\" {P} origin main",
    f"echo $((1<<'x'))\n\"$G\" {P} origin main\nx",
    f"cat <<$'x'\n\"$G\" {P} origin main\nx",
    f'sudo -u root "$G" {P} origin main',
    f'nice -n5 "$G" {P} origin main',
    f'timeout -s kill 5 "$G" {P} origin main',
    f"G=g; ${{G}}\\it {P} origin main",
    f"G=g; $G\\i\\t {P} origin main",
    f'G=git; >$(echo /dev/null) "$G" {P} origin main',
    f'G=git; > $(echo /dev/null) "$G" {P} origin main',
    f'G=git; "$G" >$(echo /dev/null) {P} origin main',
    f'G=git; coproc publisher {{ "$G" {P} origin main; }}',
    f'"${{G}}" {P} origin main',
    f'"${{G}}" {P} origin "${{B}}"',
    f'"${{G}}" {P} origin feature "${{G}}"',
    f'"$G" -C "$R" {P} origin main',
    f'"$G" -C "$(pwd)" {P} origin feature',
    f'"$(command -v git)" {P} origin feature',
    f'G=git; "$G"</dev/null {P} origin main',
    f'G=git; "$G" {P}</dev/null origin main',
    f'"$G">/dev/null {P} origin main',
    f'"$G"&>/dev/null {P} origin main',
    f"bash -O extglob -c '/usr/bin/g@(i)t {P} origin main'",
    f"g+(i)t {P} origin main",
    f"shopt -s extglob; g*(i)t {P} origin main",
    f"set -- git; \"$1\" $'{P}' origin main",
    f'"$G" $"{P}" origin main',
    "\"$G\" $'\\x70ush' origin main",
    '"$G" pu\\\nsh origin main',
    f"'/usr/bin/'g?t {P} origin main",
    f'"/usr/bin/"g?t {P} origin main',
    f"/usr\\/bin\\/g?t {P} origin main",
    f"bash -O extglob -c '/usr/bin/!(!(git)) {P} origin main'",
    f"!(!(git)) {P} origin main",
    f'd=5; timeout "$d" "$G" {P} origin main',
    f'"$G" -C "${{REPO}}" {P} origin main',
    f'coproc publisher ( "$G" {P} origin main )',
    # ``env -S``/``--split-string`` rebuilds the program and argv from a string
    # the floor cannot split, so the publish arguments after it are unknown.
    f"set -- '/usr/bin/g?t {P} origin main'; env -S 'bash -c' \"$1\" {P} origin feature",
    f"env --split-string 'bash -c' \"$1\" {P} origin feature",
    f"env --split-string='bash -c' \"${{1}}\" {P} origin feature",
    f"env --split 'bash -c' \"$1\" {P} origin feature",
    f"env -S'bash -c' \"$1\" {P} origin feature",
    f"env -iS'bash -c' \"$1\" {P} origin feature",
    f"env -iS 'bash -c' \"$1\" {P} origin feature",
    f"env -S 'bash -c' \"$G\" {P} origin main",
    # A runtime-resolved precommand option could itself be a split-string option.
    f'set -- git; env $2 "$G" {P} origin main',
    # An unquoted expansion that can vanish does not settle program position;
    # the reachable program word after it is judged.
    f'set -- git; ${{2:-}} "$1" {P} origin main',
    f'$x "$G" {P} origin main',
    f'set --; G=git; $@ "$G" {P} origin main',
    f'G=git; $! "$G" {P} origin main',
    # A ``case`` inside a command substitution is read, so a real push in its
    # body is found rather than invented or missed.
    f'$(case x in x) "$G" {P} origin main;; esac)',
    # A ``case`` opening a command after ``if``/``while``/``until`` is tracked,
    # so its pattern ``)`` does not close the substitution around it early.
    f'echo "$(if case x in x) printf \'\\"\';; esac; then :; fi)"; "${{1:-git}}" {P} origin main',
    f'echo "$(while case x in x) :;; esac; do :; done)"; "${{1:-git}}" {P} origin main',
    f'echo "$(until case x in x) :;; esac; do :; done)"; "${{1:-git}}" {P} origin main',
    # A runtime push joined to its program word by a backslash-newline is judged
    # on the folded source, so a sibling feature push does not hide it.
    f'"${{1:-git}}" \\\n{P} origin main; git {P} origin feature',
    # ``flock <file> -c '<script>'`` runs the script through a shell, so a publish
    # in its command string is read as the command line it is.
    f"flock /dev/null -c '\"${{1:-git}}\" {P} origin main'",
    f"flock /dev/null -c 'git {P} origin main'",
    f"flock /dev/null --command 'git {P} origin main'",
    # A quoted expansion that can produce zero words does not settle program
    # position either, and a precommand option value is not a reserved word.
    f'G=git; set --; "$@" "$G" {P} origin main',
    f'G=git; set --; "${{@}}" "$G" {P} origin main',
    f'G=git; set --; "$@""${{@}}" "$G" {P} origin main',
    f'G=git; a=(); "${{a[@]}}" "$G" {P} origin main',
    f'G=git; env -u function "$G" {P} origin main',
    # An unquoted glob matches nothing under ``nullglob`` and so can vanish.
    f'G=git; *.nope "$G" {P} origin main',
    # A redirection token carries no program word, and a vanishing word fills no
    # precommand operand slot, so the program after each is still judged.
    f'set -- git; &>/dev/null "$1" {P} origin main',
    f'set -- git; {{fd}}>/dev/null "$1" {P} origin main',
    f'set -- git; env *.nope "$1" {P} origin main',
    f'set -- git x; "${{@:2}}" "$1" {P} origin main',
    # A precommand word is read as the shell hands it over, ANSI-C decoded.
    f"$'env' \"${{1:-git}}\" {P} origin main",
    f"env $'-u' X \"$G\" {P} origin main",
    # A long option is read by any prefix of a value-taking one; when that prefix
    # may also be a no-value option (``sudo --login``), the program is not known.
    f'env --un UNUSED "${{1:-git}}" {P} origin main',
    f'timeout --sig TERM 5 "${{1:-git}}" {P} origin main',
    f'sudo --login "$G" {P} origin main',
    # Value options across GNU, BSD and util-linux spellings.
    f'env -a name "$G" {P} origin main',
    f'env -P /usr/bin "$G" {P} origin main',
    f'sudo --chroot / "$G" {P} origin main',
    f'sudo -a type "$G" {P} origin main',
    f'watch -q 5 "$G" {P} origin main',
    f'"$G" --attr-source HEAD {P} origin main',
    # A word whose arity is not known -- a runtime word that may be a dispatcher
    # or an option, an option spelled with an expansion or a glob, an option of a
    # precommand whose options are not modelled -- leaves the program unknown,
    # so a later runtime word running ``push`` is unverifiable.
    f'"${{1:-env}}" -i "${{2:-git}}" {P} origin main',
    f'env "-${{1:-u}}" UNUSED "${{2:-git}}" {P} origin main',
    f'flock "$F" echo "$G" {P} origin main',
    f'duration=5; G=git; timeout "$duration" echo "$G" {P} origin main',
    f'env -* UNUSED "$G" {P} origin main',
    f'parallel --sshlogin host "$G" {P} origin main',
    # A quoted line is data only when no command line is spelled like it, it
    # lies wholly inside the quotes, and no substitution runs it.
    f'{GIT_FEATURE}; echo "a\n$G {P} origin main\n"\n$G {P} origin main',
    f'{GIT_FEATURE}; echo "a\nb"; "$G" {P} origin main',
    f'{GIT_FEATURE} && echo "$(true\n$G {P} origin main\n)"',
    # A line reopened after its quote closes is read even when an expansion follows.
    f"{GIT_FEATURE}; echo 'note\n'; \"${{1:-git}}\" {P} origin main",
    f'{GIT_FEATURE}; echo "note\n"; "${{1:-git}}" {P} origin main',
    f"{GIT_FEATURE}; echo 'note\n'; \"$(echo git)\" {P} origin main",
    f"{GIT_FEATURE}; echo 'a\nb' && x=$(true) \"${{1:-git}}\" {P} origin main",
    # An option value an unquoted expansion can split leaves the program unknown.
    f'set -- "UNUSED -S bash -c" "git pu""sh origin main"; env -u $1 "$2" {P} origin feature',
    # ``--config-env`` takes the next word, like ``-c``.
    f'set -- git; export REVIEW_VALUE=true; "$1" --config-env review.safe=REVIEW_VALUE {P}'
    " origin main",
    # A closer closes only the opener it matches.
    f'set -- git; "${{1:-x)/adb}}" {P} origin main',
    f"bash -O extglob -c '/usr/bin/g@(i|}})t {P} origin main'",
    # A line continuation inside a substitution opener still opens it, so the
    # quote inside the body does not reach the text after it.
    f'echo "$\\\n(printf "\'")"; "${{1:-git}}" {P} origin main',
    f'echo $\\\n\\\n(printf "\'"); "${{1:-git}}" {P} origin main',
    f'echo "$\\\n{{x:-\'}}"; "${{1:-git}}" {P} origin main',
    f'cat <\\\n(printf "\'"); "${{1:-git}}" {P} origin main',
    f'"$\\\n(echo git)" {P} origin main',
    # A variable path whose argv git accepts is not known to publish nothing.
    f"$X/adb {P} -d -r",
    f"env -u $Y $X/bin/dvc {P} -r storage",
    # Split, an unquoted variable path can be any program first (``bash -c``
    # with a payload of its own), so an argv git would reject proves nothing.
    f"X='bash -c git${{IFS}}{P}${{IFS}}origin${{IFS}}main x'; $X/bin/dvc {P} -r storage",
    f"$VENV/bin/dvc {P} -r storage",
    f"${{VENV}}/bin/dvc {P} -r storage",
    f"timeout 60 $VENV/bin/dvc {P} -r storage",
    # Whitespace inside a parameter expansion stays inside the one shell word.
    f"set -- git; if true; then ${{1:-unused words}} {P} origin main; fi",
    f"set -- git; ${{1:-unused words}} {P} origin main",
    f'set -- git; "${{1:-unused words}}" {P} origin main',
    f"set -- git; ${{1:-a ${{2:-b c}} d}} {P} origin main",
    f"set -- git; ${{1:-$(echo a b)}} {P} origin main",
    f"set -- git; ${{1:-`echo a b`}} {P} origin main",
    f"set -- git; ${{1:-'a }} b'}} {P} origin main",
    f"set -- git; env ${{1:-unused words}} {P} origin main",
    f"set -- git; ${{1:-x}}/${{2:-a b}} {P} origin main",
]

STILL_ALLOWED = [
    GIT_FEATURE,
    f'"$G" {P} origin feature',
    # A continuation-split publish to a feature target stays allowed, and an
    # expansion in a SIBLING segment never settles a feature push's own target.
    f'"$G" \\\n{P} origin feature',
    f'echo {{a,b}}; "$G" \\\n{P} origin feature',
    f'echo {{a,b}}; "$G" {P} origin feature',
    # A markdown bullet in a body written to a file whose basename is no git
    # hook is prose beside a git sibling, and a hook body no git sibling fires
    # (only a plain runner is present) is prose too.
    f"cat > CHANGELOG.md <<'EOF'\n* {P} origin main\nEOF\ngit commit -m x",
    f"cat > .husky/helper.sh <<'EOF'\n* {P} origin main\nEOF\ngit commit -m x",
    f"cat > .husky/pre-commit <<'EOF'\n* {P} origin main\nEOF\nmake test",
    f'"/usr/bin/$G" {P} origin feature',
    f'for i in 1; do "$G" {P} origin feature; done',
    f'"$G" stash {P}',
    '"$G" status',
    f'"$G" log --grep {P}',
    f'echo "$G" {P}',
    f"printf '%s\\n' \"$x\" {P}",
    f'printf \'%s\\n\' "$(date)" "$name" {P}',
    f'echo $(date) "$x" {P}',
    f'echo $(date +%s) "$x" {P}',
    f'echo "a;" "$x" {P}',
    f'echo "a)" "$G" {P} x',
    f'echo "(" "$x" {P} now',
    f"grep -e '{{' \"$f\" {P}",
    f'printf "%s|" "$x" {P}',
    f'echo do "$X" {P} y',
    f'"$PAGER" {P}.txt',
    f"make {P}",
    f"echo {P}",
    '"$EDITOR" file.txt',
    "$CC -o x x.c",
    'for f in *.py; do black "$f"; done',
    f'cd "$dir" && {GIT_FEATURE}',
    f'if [ -n "$X" ]; then {GIT_FEATURE}; fi',
    f"nice -n 5 {GIT_FEATURE}",
    f"bash -c '{GIT_FEATURE}'",
    f"bash -c 'if true; then {GIT_FEATURE}; fi'",
    f"'git' {P} origin feature",
    f"sleep 1 & {GIT_FEATURE}",
    f'"$G" status 2>&1 | grep {P}',
    f'echo $(cat a b) "$x" {P}',
    'case "$1" in a) echo a;; esac',
    f"time {GIT_FEATURE}",
    f'"$ANDROID_HOME/platform-tools/adb" {P} app-debug.apk /data/local/tmp/',
    f'cd android && "$ANDROID_SDK_ROOT/platform-tools/adb" {P} app.apk /sdcard/',
    f'"$VENV/bin/dvc" {P} -r storage',
    f'"$HOME/bin/deploy" {P} origin main',
    f'"$HOME/bin/git" {P} origin feature',
    f'"$HELM" {P} chart-0.1.0.tgz oci://ghcr.io/example-org/charts',
    f'"$CACHIX" {P} example-cache ./result',
    f'sudo echo "$x" {P}',
    f'nice -n 5 make "$T" {P}',
    f'sudo docker "$x" {P}',
    f'command -p echo "$G" {P} origin main',
    f'nice -n5 echo "$G" {P} origin main',
    f'sudo -E echo "$G" {P} origin main',
    f'g"[i]"t {P} origin main',
    f"cat > notes.md <<'EOF'\nrun \"$G\" {P} origin main later\nEOF\necho done",
    f'# "$G" {P} origin main\necho ok',
    f'coproc worker {{ echo "$G" {P}; }}',
    f'"${{GIT}}" {P} origin feat/example',
    f'"$GIT" --git-dir "$REPO/.git" {P} origin feat/example',
    f'"${{HELM}}" {P} chart-0.1.0.tgz oci://ghcr.io/example-org/charts',
    f'"${{G}}" {P} origin feature; "${{G}}" {P} origin topic',
    f'"$G" {P}>log.txt origin feature',
    f'"$G"</dev/null {P} origin feature',
    f'echo "$x">out.txt {P}',
    f"ls !(build) {P}",
    f'"$G" --work-tree "${{REPO}}" {P} origin feature',
    f"'/usr/bin/'echo \"$G\" {P}",
    f'echo @(a|b) "$x" {P}',
    f'echo hi >$(echo out.log) "$x" {P}',
    f'echo ok # "$G" {P} origin main',
    f"$'adb' {P} app.apk /sdcard/",
    f"cat <<'EOF' # note\ndon't\nEOF\n\"$G\" {P} origin feature",
    f'echo $((1<<2))\n"$G" {P} origin feature',
    f'nice "$G" {P} origin feature',
    f'timeout 5 "$G" {P} origin feature',
    f"gh pr create --title 'fix: example' --body-file - <<'EOF'\n## Test plan\n* {P} origin main only after CI is green\nEOF",
    f"git commit -F - <<'EOF'\nfix(security): example\n\n* {P} origin main is refused\nEOF",
    f"gh pr comment 9332 --body-file - <<'EOF'\nDisposition:\n\"$G\" {P} origin main is refused\nEOF",
    f"gh issue comment 1 --body-file - <<'EOF'\n* {P} origin main, alias and export are prose\nEOF",
    f"cd repo && git add -A && git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"git commit -F - <<'EOF'\nE\\\nOF\nEOF\n\"$G\" {P} origin feature",
    f'G=git; command -v "$G" {P}',
    f'G=git; command -V "$G" {P}',
    f"git commit -m \"$(cat <<'EOF'\nfix: don't drop the {P} target\nEOF\n)\"",
    # A double-quoted ``$(cat <<'EOF' ...)`` that only prints its body into a
    # sink argument is message text, so a markdown ``* push`` bullet in it is prose.
    f"git commit -m \"$(cat <<'EOF'\nfix: example\n\n* {P} only feature branches\nEOF\n)\"",
    f"gh pr create --title 'fix: example' --body \"$(cat <<'EOF'\n## Summary\n* {P} the feature branch only after CI is green\nEOF\n)\"",
    f"git commit -m \"$(cat <<-'EOF'\n\t* {P} only feature branches\n\tEOF\n)\"",
    f'git commit -m "$(cat <<"EOF"\n* {P} only feature branches\nEOF\n)"',
    f"git commit -m \"$(cat << 'EOF'\n* {P} only feature branches\nEOF\n)\"",
    f'git commit -m "$(cat <<\'EOF\'\n"$G" {P} origin main\nEOF\n)"',
    f"git add -A && git commit -m \"$(cat <<'EOF'\n/usr/bin/g?t {P} origin main\nEOF\n)\"",
    f"gh pr comment 9332 --body \"$(cat <<'EOF'\nDisposition:\n* {P} feat/example once review is green\nEOF\n)\"",
    f"echo \"$(cat <<'EOF'\n* {P} notes\nEOF\n)\"",
    f"git commit --no-edit -F - <<'EOF'\n* {P} only feature branches\nEOF",
    f'git commit -m "$(cat <<EOF\nfix: example\n\n* {P} only feature branches\nEOF\n)"',
    # Quoted text is text: a ``>`` or ``()`` inside a single-quoted title leaves
    # the sink exemption standing.
    f"gh pr create --title 'refactor: base -> head reader' --body-file - <<'EOF'\n* {P} origin feature after CI is green\nEOF",
    f"gh pr create --title 'fix: _is_git_publish() reads program position' --body-file - <<'EOF'\n* {P} origin feature after CI is green\nEOF",
    f"gh pr create --title \"fix: function names\" --body-file - <<'EOF'\n* {P} origin feature\nEOF",
    '"$EDITOR" notes.md',
    f'"$CC" -o out {P}.c',
    f'"$PAGER" {P}.log',
    f"ls [[:upper:]]*.md {P}",
    f"git log --oneline -5; gh pr create --body-file - <<'EOF'\n* {P} notes for the release\nEOF",
    f'echo "$(case x in x) echo {P};; esac)"',
    f'echo "$(case x in (x) echo {P};; esac)"',
    f'git commit -m "$(echo case) {P}"',
    f'"${{GIT:-git}}" {P} origin feature',
    f'"$HOME\\bin\\git.exe" {P} origin feat/example',
    f'& "$env:ProgramFiles\\Git\\cmd\\git.exe" {P} origin feat/example',
    f'"$env:ANDROID_HOME\\platform-tools\\adb.exe" {P} app-debug.apk /data/local/tmp/',
    # A backslash path whose literal basename names another program is that
    # program, read with ``\`` as a separator or as an escape.
    f'& "$env:ProgramFiles\\Docker\\Docker\\resources\\bin\\docker.exe" {P} ghcr.io/example-org/app:1.2.3',
    f'"$X\\bin\\docker.exe" {P} ghcr.io/example-org/app:1.2.3',
    f'"$X\\bin\\docker" {P} img:1',
    # A line wholly inside a quoted argument is part of it, not a command.
    f"git {P} -u origin feat/example && gh pr create --title 'fix: example' --body \"## Summary\n"
    f'* {P} the feature branch only after CI is green\n* never {P} main"',
    f'git {P} origin feat/x && git commit -m "line one\n* {P} the feature branch"',
    f'git {P} origin feat/example; @"\n## Summary\n* {P} the feature branch after CI is green\n"@'
    " | gh pr create --title 'fix: example' --body-file -",
    # ``git fetch`` and ``git push`` move refs only, so a sink body beside them is data.
    f"git {P} -u origin feat/example && gh pr create --title 'fix: example' --body \"$(cat <<'EOF'\n"
    f'## Summary\n* {P} the feature branch only after CI is green\nEOF\n)"',
    f"git fetch origin main && gh pr comment 9332 --body-file - <<'EOF'\nDisposition:\n"
    f"* {P} feat/example once review is green\nEOF",
    f"git tag v1.2.3 && git {P} origin v1.2.3 && gh release create v1.2.3 --notes-file - <<'EOF'\n"
    f"* {P} notifications retry\nEOF",
    # A markdown bullet in a body ``cat`` only writes to a file is not a program.
    f"cat > /tmp/pr-body.md <<'EOF'\n## Test plan\n* {P} the feature branch after CI is green\nEOF",
    f"cat >> CHANGELOG.md <<'EOF'\n* {P} notifications now retry on 429\nEOF",
    f"cat <<-'EOF' > notes.md\n\t* {P} notes\n\tEOF",
    f"{GIT_FEATURE} && cat > notes.md <<'EOF'\n* {P} the branch later\nEOF",
    # A body only written to a plainly named file, beside sinks and companions
    # alone, is data: it runs only through a reader set up before the command.
    f"cat > notes.md <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"cat >>notes.md <<-'EOF'\n\t\"$G\" {P} origin main\n\tEOF",
    f"cat > scripts/release.sh <<'EOF'\n#!/usr/bin/env bash\nset -euo pipefail\n"
    f'"${{GIT:-git}}" {P} origin "$TAG"\nEOF',
    f"tee docs/release-notes.md >/dev/null <<'EOF'\n## Changes\n* {P} notifications retry on 429\nEOF",
    # ``cat`` piped into a sink feeds it; ``glab`` is a sink; a plain ``${NAME}``
    # and a sink option's value name no editor; a CRLF printer is a printer.
    f"cat <<'EOF' | gh pr create --title 'fix: example' --body-file -\n## Test plan\n"
    f"* {P} the feature branch after CI is green\nEOF",
    f"glab mr create --title 'fix: example' --description \"$(cat <<'EOF'\n"
    f'* {P} only feature branches\nEOF\n)"',
    f"gh pr create --head \"${{BRANCH}}\" --title 'fix: example' --body \"$(cat <<'EOF'\n"
    f'## Summary\n* {P} the feature branch only after CI is green\nEOF\n)"',
    f"git commit -m \"$(cat <<'EOF'\r\nfix: example\r\n\r\n* {P} only feature branches\r\nEOF\r\n)\"",
    # Beside a plain test or build runner a sink's markdown bullet is not a program.
    f"uv run pytest -q && git commit -m \"$(cat <<'EOF'\nfix: example\n\n"
    f'* {P} only feature branches\nEOF\n)"',
    f"make test && gh pr create --title 'fix: example' --body-file - <<'EOF'\n"
    f"* {P} the feature branch only after CI is green\nEOF",
    f"npm test && git commit -m \"$(cat <<'EOF'\nfix: example\n\n* {P} retries on 429\nEOF\n)\"",
    f"pre-commit run --all-files && git commit -F - <<'EOF'\nfix: example\n\n"
    f"* {P} only feature branches\nEOF",
    f"ruff check . && gh pr create --title 'fix: example' --body-file - <<'EOF'\n## Summary\n"
    f"* {P} the feature branch only after CI is green\nEOF",
    f"yarn run lint && git commit -F - <<'EOF'\n* {P} only feature branches\nEOF",
    f"uv run ruff format src && git commit -F - <<'EOF'\n* {P} only feature branches\nEOF",
    # A body only written to a file, beside a plain runner.
    f"cat > notes.md <<'EOF'\n* {P} notifications fixed\nEOF\nmake test",
    # A bullet in a body written to a file no runner reads (no ``make`` sibling,
    # or a file ``make`` never reads for its recipes) is prose: ``make`` reading
    # a ``Makefile`` runs its recipes, but a lone write, or one beside a command
    # that is not that ``make``, plants nothing anyone runs.
    f"cat > notes.md <<'EOF'\n* {P} origin main\nEOF",
    f"cat > Makefile <<'EOF'\npublish:\n\t* {P} origin main\nEOF\necho prepared the release notes",
    # A file written but never run stays data even beside a companion name:
    # ``chmod +x notes.md; sleep 1`` installs nothing anyone invokes.
    f"cat > notes.md <<'EOF'\n\"$G\" {P} origin main\nEOF\nchmod +x notes.md; sleep 1",
    f"cat > docs/notes.md <<'EOF'\n## Changes\n* {P} retries on 429\nEOF\nuv run pytest -q",
    f"make test && cat >> CHANGELOG.md <<'EOF'\n* {P} notifications retry\nEOF",
    # A shell script run by its path is a hookless sibling, like a ``.py`` one.
    f"bash scripts/check.sh && gh pr create --title 'fix: example' --body-file - <<'EOF'\n"
    f"## Test plan\n* {P} the feature branch after CI is green\nEOF",
    f"sh scripts/lint.sh && gh pr create --body-file - <<'EOF'\n* {P} after CI is green\nEOF",
    # A target-neutral flag's one-word value may be attached with ``=``.
    f'"$GIT" --git-dir="$REPO/.git" {P} origin feat/example',
    f'"$GIT" --work-tree="$REPO" {P} origin feat/example',
    # ``push`` glued to a letter is another word, even in text bash cannot read.
    f'echo "{P}ed feat/example`n"',
    f'Write-Host "{P}ed feat/example`n"',
    # ``gh`` and ``glab`` run no hook on a body, so beside a checkout, switch or
    # stash, or a script run by its path, a markdown bullet in one is not a program.
    f"python3 scripts/docs_lint.py && gh pr create --title 'docs: example' --body-file - <<'EOF'\n"
    f"## Summary\n* {P} the feature branch after CI is green\nEOF",
    f"git switch -c feat/example origin/main && gh pr create --base main --title 'fix: example'"
    f" --body-file - <<'EOF'\n* {P} only after CI is green\nEOF",
    # ``git branch --show-current`` is a read-only companion, so a gh body bullet
    # beside it is text, like beside git log or status.
    f"git branch --show-current && gh pr create --title 'fix: example' --body-file - <<'EOF'\n"
    f"## Test plan\n* {P} the feature branch after CI is green\nEOF",
    f"git stash pop && gh pr create --title 'fix: example' --body-file - <<'EOF'\n## Test plan\n"
    f"* {P} the feature branch after CI is green\nEOF",
    f"git checkout -b feat/example && gh pr create --title 'fix: example' --body-file - <<'EOF'\n"
    f"* {P} the branch after review\nEOF",
    f"git stash pop && cat <<'EOF' | gh pr create --title 'fix: example' --body-file -\n"
    f"* {P} the feature branch after CI is green\nEOF",
    f"git switch -c feat/example && gh pr create --title 'fix: example' --body \"$(cat <<'EOF'\n"
    f'* {P} the feature branch after CI is green\nEOF\n)"',
    f"python3 scripts/docs_lint.py && gh pr create --title 'fix: example' --body \"$(cat <<'EOF'\n"
    f'* {P} the feature branch after CI is green\nEOF\n)"',
    f"git switch -c feat/x && glab mr create --title 'fix: example' --description \"$(cat <<'EOF'\n"
    f'* {P} the feature branch after CI is green\nEOF\n)"',
    f"git rebase origin/main && gh pr create --title 'fix: example' --body-file - <<'EOF'\n"
    f"* {P} after rebase onto main\nEOF",
    f"git fetch origin main && git rebase origin/main && git {P} --force-with-lease origin feat/example"
    f' && gh pr create --title "fix: example" --body "$(cat <<\'EOF\'\n* {P} after rebase onto main\n'
    f'EOF\n)"',
    # ``mkdir`` makes directories and runs nothing, and git reads no ``.github`` path.
    f"mkdir -p docs/notes && cat > docs/notes/release.md <<'EOF'\n## Changes\n"
    f"* {P} notifications now retry on 429\nEOF",
    f"mkdir -p docs/notes && tee docs/notes/release.md >/dev/null <<'EOF'\n"
    f"* {P} notifications now retry on 429\nEOF",
    f"mkdir docs && cat > docs/x.md <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"cat > .github/pull_request_template.md <<'EOF'\n## Checklist\n* {P} only feature branches\nEOF",
    f"mkdir -p .github && cat > .github/pull_request_template.md <<'EOF'\n## Checklist\n"
    f"* {P} only feature branches\nEOF",
    # ``flock -c`` of a feature push is the push it names, judged on its own.
    f"flock /dev/null -c 'git {P} origin feature'",
    "flock /dev/null -c 'echo pushing to the registry'",
    # A mode change runs nothing, so a body only written to a file stays data beside it.
    f"cat > scripts/release.sh <<'EOF'\n#!/usr/bin/env bash\nset -euo pipefail\n"
    f'"$DOCKER" {P} "$IMAGE"\nEOF\nchmod +x scripts/release.sh',
    # A redirection before or after the push is consumed by the shell, so a
    # feature push behind one is judged by its feature target.
    f"git 2>/dev/null {P} origin feature",
    f"git </dev/null {P} -u origin feat/example",
    f'git >"$LOG" {P} origin feature',
    f'"$G" {P} origin feature </dev/null> /dev/null',
    f"git {P} origin feature </dev/null> /dev/null",
]

#: Heredoc bodies are read as command text where what consumes one may run it:
#: a program, a redirection or link the text sets up, or a hook beside it.
HEREDOC_BODIES = [
    f"bash <(cat <<'EOF' 3>&1 > notes.md >&3\n\"$1\" {P} origin main\nEOF\n) git",
    f"cat <<'EOF' > notes.md >&3\n\"$G\" {P} origin main\nEOF",
    f"mkfifo f; bash <f & cat <<'EOF' >f\n\"$G\" {P} origin main\nEOF",
    f"ln -s /dev/stdout o; bash <(cat <<'EOF' >o\n\"$1\" {P} origin main\nEOF\n) git",
    f"cat <<'EOF' > notes.md\ndon't\nEOF\n\"$G\" {P} origin main",
    f"cat <<'EOF' # note\ndon't\nEOF\n\"$G\" {P} origin main",
    f"x=$(cat <<'EOF'\ndon't\nEOF\n); \"$G\" {P} origin main",
    f'echo $((1<<2))\n"$G" {P} origin main',
    f"G=git; cat <<$'EOF'\n'\nEOF\n\"$G\" {P} origin main",
    f'cat <<$"EOF"\n\'\nEOF\n"$G" {P} origin main',
    f'cat <<E\\OF\n\'\nEOF\n"$G" {P} origin main',
    f'cat <<"E\\OF"\n\'\nE\\OF\n"$G" {P} origin main',
    f'cat <<E""OF\n\'\nEOF\n"$G" {P} origin main',
    f'cat <<$X\n\'\n$X\n"$G" {P} origin main',
    f"cat <<''\n'\n\n\"$G\" {P} origin main",
    f"cat <<'E O'\n'\nE O\n\"$G\" {P} origin main",
    f"git() {{ bash; }}; git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"PATH=.:$PATH; git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"source ./env.sh; gh pr create --body-file - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"git -c alias.x='!bash' x <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"gh api --input - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"./git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"G=1 git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"git commit -F - <<'A'; bash <<'B'\nmsg\nA\n\"$G\" {P} origin main\nB",
    f"shopt -s expand_aliases\na\"\"lias git='bash -s --'\ngit commit -F - <<'EOF'\nG=git; \"$G\" {P} origin main\nEOF",
    f"\\export PATH=.; git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"if true; then git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF\nfi",
    f"printf x > .git/hooks/commit-msg; git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"export PATH=.; git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"PATH=.\ngit commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f". ./env.sh && git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"gh () {{ bash; }}; gh pr create --body-file - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"/bin/eval x; git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"time source x.sh; git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f'"$X" setup && git commit -F - <<\'EOF\'\n"$G" {P} origin main\nEOF',
    f"uv run pytest -q && git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"make test && gh pr create --body-file - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f'git commit -F - <<EOF\nmsg\nE\\\nOF\n"${{G:-git}}" {P} origin main',
    f'git commit -F - <<E\\\nOF\nmsg\nEOF\n"$G" {P} origin main\nEOF',
    f"git hash-object --stdin <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"echo '* filter=run' >.gitattributes; git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"git config filter.run.clean bash; git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"git commit -e -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"gh pr create --editor --body-file - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"git commit --allow-empty --edi -F - <<'EOF'\n/usr/bin/g?t {P} origin main\nEOF",
    f"git commit --allow-empty --ed -F - <<'EOF'\n/usr/bin/g?t {P} origin main\nEOF",
    f"gh pr create --edito --body-file - <<'EOF'\n/usr/bin/g?t {P} origin main\nEOF",
    f"git commit --allow-empty -eF- <<'EOF'\n/usr/bin/g?t {P} origin main\nEOF",
    f'git commit -m "$(cat <<EOF\n* {P} $x\nEOF\n)"',
    f"git log -1 --format=x $'--output=.git/config'; git commit --allow-empty $'--edit' -m \"$(cat <<'EOF'\n/usr/bin/g?t {P} ../remote.git HEAD:main\nEOF\n)\"",
    f"git commit --allow-empty -t /dev/stdin <<'EOF'\n/usr/bin/g?t {P} origin main\nEOF",
    f"git commit --allow-empty --template=/dev/stdin <<'EOF'\n/usr/bin/g?t {P} origin main\nEOF",
    f"git commit $opt -F - <<'EOF'\n/usr/bin/g?t {P} origin main\nEOF",
    f'gh pr create --title "$(id)" --body-file - <<\'EOF\'\n"$G" {P} origin main\nEOF',
    f'gh pr create --title "`id`" --body-file - <<\'EOF\'\n"$G" {P} origin main\nEOF',
    f"gh pr create --title x > out --body-file - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"cat <<EOF\n'`\"$G\" {P} origin main`'\nEOF",
    f"echo '`x`' `\"$G\" {P} origin main`",
    f"echo '`\"${{1:-git}}\" {P} origin main`'; bash -c '\"${{1:-git}}\" {P} origin main'",
    f"PATH=. git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f'git commit -F - <<EOF\n$(true)\n"$G" {P} origin main\nEOF',
    f"git fetch --upload-pack='cp /bin/bash .git/hooks/commit-msg; false' .; git commit --allow-empty -F - <<'EOF'\n/bin/g?t {P} origin main\nEOF",
    # A quoted option fragment still names the editor or output file after
    # dequoting, so the sink exemption does not hold.
    f'git log -1 --format=x --out""put=.git/config; git commit --allow-empty --e""dit -F - <<\'EOF\'\n/usr/bin/[g]it {P} origin main\nEOF',
    # ``git log``/``diff`` with ``--output`` writes a file the sink then runs,
    # so the inert-companion exemption does not hold and the body is command text.
    f"git log -1 --format=x --output=.git/hooks/commit-msg; git commit --allow-empty -F - <<'EOF'\n/bin/g?t {P} origin main\nEOF",
    f"git log --output=hook.sh; git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"git diff --output-directory out; git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    # A heredoc printer whose output can be run, split, or reach anything but
    # a sink or an inert companion is read as command text.
    f'bash -c "$(cat <<\'EOF\'\n"$G" {P} origin main\nEOF\n)"',
    f'eval "$(cat <<\'EOF\'\n"$G" {P} origin main\nEOF\n)"',
    f'echo "$(cat <<\'EOF\'\n"$G" {P} origin main\nEOF\n)" | bash',
    f"\"$(cat <<'EOF'\ngit\nEOF\n)\" {P} origin main",
    f"git commit -m $(cat <<'EOF'\n\"$G\" {P} origin main\nEOF\n)",
    f'git commit -m "$(cat <<EOF\n$("$G" {P} origin main)\nEOF\n)"',
    f'git commit -m "$(cat <<\'EOF\'\nx\nEOF\n"$G" {P} origin main)"',
    f'git commit -m "$(cat /dev/stdin <<\'EOF\'\n"$G" {P} origin main\nEOF\n)"',
    f'git commit -e -m "$(cat <<\'EOF\'\n"$G" {P} origin main\nEOF\n)"',
    f'GIT_EDITOR=bash git commit -m "$(cat <<\'EOF\'\n"$G" {P} origin main\nEOF\n)"',
    f'cat() {{ bash; }}; git commit -m "$(cat <<\'EOF\'\n"$G" {P} origin main\nEOF\n)"',
    f"source <(cat <<'EOF'\n\"$G\" {P} origin main\nEOF\n)",
    f'git commit -m "$(cat <<\'EOF\'\nx\nEOF\n)"; bash -c "$(cat <<\'EOF\'\n"$G" {P} origin main\nEOF\n)"',
    f'git commit -m "$(cat <<\'EOF\'\n"$G" {P} origin main\nEOF\n)"; bash -c "$(cat <<\'EOF\'\n"$G" {P} origin main\nEOF\n)"',
    f'x="$(cat <<\'EOF\'\n"$G" {P} origin main\nEOF\n)"; $x',
    # A transport program option makes ``git fetch``/``git push`` able to plant a hook.
    f"git fetch --upload-pack='x' origin && git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"git fetch --upl=x origin && git commit -F - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"git {P} --receive-pack=x origin feat && git commit -F - <<'EOF'\n/bin/g?t {P} origin main\nEOF",
    f"git {P} --exec x origin feat && git commit -F - <<'EOF'\n/bin/g?t {P} origin main\nEOF",
    # A bullet is still read where its body may run: beside another program, in
    # a pipeline, to a device, with substitutions bash runs, or before more text.
    f"touch git; cat > /tmp/f <<'EOF'\n* {P} origin main\nEOF",
    f"cat > /tmp/f <<'EOF' | bash\n* {P} origin main\nEOF",
    f"cat > /dev/stdout <<'EOF'\n* {P} origin main\nEOF",
    f"cat > f <<EOF\n$(\n* {P} origin main\n)\nEOF",
    # A file git reads, a device, or a write beside a runner can run the body.
    f"cat > .git/hooks/pre-commit <<'EOF'\n\"$G\" {P} origin main\nEOF\ngit commit -m x",
    # A body written to an executable a sibling then RUNS is command text, even
    # when the sibling's name reads as an inert companion (``sleep``/``true``).
    f"cat > ~/.local/bin/sleep <<'EOF'\n#!/bin/sh\n\"${{1:-git}}\" {P} origin main\nEOF\n"
    f"chmod +x ~/.local/bin/sleep; sleep",
    f"cat > ~/.local/bin/true <<'EOF'\n* {P} origin main\nEOF\nchmod +x ~/.local/bin/true; true",
    f"cat > bin/ls <<'EOF'\n\"$G\" {P} origin main\nEOF\nchmod +x bin/ls; PATH=bin:$PATH ls",
    f"cat >> .git/config <<'EOF'\n[core]\n\"$G\" {P} origin main\nEOF",
    f"tee .gitattributes <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"tee /dev/fd/3 <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"make test && cat > notes.md <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"cat > notes.md <<'EOF'\n* x\n\"$G\" {P} origin main\nEOF\nmake test",
    f"cat > notes.md <<'EOF'\n* {P} origin main\nEOF\nmake -f - test",
    f"cat > notes.md <<'EOF'\n* {P} origin main\nEOF\nmake test FOO=bar",
    f"cat > notes.md <<'EOF' | bash\n* {P} origin main\nEOF\nmake test",
    # A script runner with an option, or beside a git sink, gives no bullet reading.
    f"bash -x scripts/check.sh && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"bash scripts/check.sh && git commit -F - <<'EOF'\n* {P} origin main\nEOF",
    f"bash scripts/check.sh && gh pr create --body-file - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    # An attached value is one word only when quoted, and only for a neutral flag.
    f'"$G" --git-dir=$REPO {P} origin feature',
    f'"$G" -c="$X" {P} origin feature',
    # A standalone ``push`` beside a glued one is still read.
    f'echo "{P}ed`n"; "$G" {P} origin main',
    f'echo "{P} feat/example`n"',
    f"cat > .husky/pre-commit <<'EOF'\n\"$G\" {P} origin main\nEOF\ngit commit -m x",
    f"cat > .husky/pre-push <<'EOF'\n\"$G\" {P} origin main\nEOF\n{GIT_FEATURE}",
    # git runs a file as a hook only by one of its own hook names, so a body
    # written to such a basename and fired by a git sibling is command text:
    # a markdown bullet in it is a glob program word, read like a literal push.
    # The dir does not have to be ``.git/hooks`` (``core.hooksPath`` can point
    # anywhere), so the basename, not the path, names the hook.
    f"cat > .husky/pre-commit <<'EOF'\n* {P} origin main\nEOF\ngit commit -m x",
    f"cat > myhooks/pre-push <<'EOF'\n* {P} origin main\nEOF\ngit commit --allow-empty -m x",
    # A body written to a file ``make`` reads for its recipes by default
    # (``Makefile`` and its spellings) and fired by a default-input ``make``
    # sibling is a recipe ``make`` runs through the shell, so the body is command
    # text: a markdown bullet in it is a glob program word, read like a literal
    # push.  Every spelling ``make`` reads, under one-shot recipes or line by
    # line, with the delimiter quoted or bare, counts.
    f"cat > Makefile <<'EOF'\n.ONESHELL:\npublish:\n\tcd bin\n\t* {P} origin main\nEOF\nmake publish",
    f"cat > Makefile <<'EOF'\npublish:\n\t* {P} origin main\nEOF\nmake publish",
    f"cat > Makefile <<EOF\npublish:\n\t* {P} origin main\nEOF\nmake publish",
    f"cat > makefile <<'EOF'\npublish:\n\t* {P} origin main\nEOF\nmake publish",
    f"cat > GNUmakefile <<'EOF'\npublish:\n\t* {P} origin main\nEOF\nmake publish",
    # A bullet beside an inline-code or assignment-bearing runner is a program
    # word (it is no plain runner); a runtime push in a body counts beside any.
    f"make -f - test && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"make test FOO=bar && git commit -F - <<'EOF'\n* {P} origin main\nEOF",
    f"make --eval=x test && git commit -F - <<'EOF'\n* {P} origin main\nEOF",
    f"make test && git commit -F - <<'EOF'\n* x\n\"$G\" {P} origin main\nEOF",
    # A sink option's value is skipped only when it is one word.
    f"gh pr create --title $T --body-file - <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f'gh pr create --title --body "$X" --body-file - <<\'EOF\'\n"$G" {P} origin main\nEOF',
    f'gh pr create --head "${{B:=x}}" --body-file - <<\'EOF\'\n"$G" {P} origin main\nEOF',
    f"cat <<'EOF' | bash -c 'gh pr create --body-file -'\n\"$G\" {P} origin main\nEOF",
    # Each queued body is read in a quote context of its own.
    f"cat <<'A'; bash <<'EOF'\n'\nA\n\"${{1:-git}}\" {P} origin main\nEOF",
    f"bash <<'A'; cat <<'B'\n\"${{1:-git}}\" {P} origin main\nA\n'\nB",
    # A glob that can expand to an editor option voids the sink exemption.
    f"git commit --allow-empty --[e]dit -F - <<'EOF'\n\"${{1:-git}}\" {P} origin main\nEOF",
    f"git commit --allow-empty --?dit -F - <<'EOF'\n\"${{1:-git}}\" {P} origin main\nEOF",
    f"git commit --allow-empty * -F - <<'EOF'\n\"${{1:-git}}\" {P} origin main\nEOF",
    f"git commit --allow-empty --@(edit) -F - <<'EOF'\n\"${{1:-git}}\" {P} origin main\nEOF",
    f"gh pr create --[e]dit --body-file - <<'EOF'\n\"${{1:-git}}\" {P} origin main\nEOF",
    # A command that is no inert companion or plain runner beside the sink keeps
    # the body command text: it could plant a hook, redefine the sink, or run
    # the body in this shell, so a ``*`` bullet in it is still a program word.
    f"npm exec x && git commit -F - <<'EOF'\n* {P} origin main\nEOF",
    f"npx x && git commit -F - <<'EOF'\n* {P} origin main\nEOF",
    f"pre-commit try-repo x && git commit -F - <<'EOF'\n* {P} origin main\nEOF",
    f"npm run x -e y && git commit -F - <<'EOF'\n* {P} origin main\nEOF",
    f"touch git && git commit -F - <<'EOF'\n* {P} origin main\nEOF",
    f"flake8 . && git commit -F - <<'EOF'\n* {P} origin main\nEOF",
    f"python3 scripts/x.py && git commit -F - <<'EOF'\n* {P} origin main\nEOF",
    f"./deploy.sh && git commit -F - <<'EOF'\n* {P} origin main\nEOF",
    # A function, alias or ``PATH=`` that redefines the sink runs the body, so a
    # ``*`` bullet in it (a glob the command's own ``touch git`` can resolve) is
    # not read as text.
    f"touch git; gh(){{ bash; }}; gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"shopt -s expand_aliases; alias git=bash; touch git; git commit -F - <<'EOF'\n* {P} origin main\nEOF",
    f"touch git; PATH=. git commit -F - <<'EOF'\n* {P} origin main\nEOF",
    f"npm test && git commit -F - <<'EOF'\n\"${{1:-git}}\" {P} origin main\nEOF",
    # A file a mode change makes executable still runs for a command that runs it.
    f"cat > s.sh <<'EOF'\n\"${{1:-git}}\" {P} origin main\nEOF\nchmod +x s.sh; ./s.sh",
    f"cat > s.sh <<'EOF'\n\"${{1:-git}}\" {P} origin main\nEOF\nchmod +x s.sh && git commit -am x",
    f"cat > s.sh <<'EOF'\n\"${{1:-git}}\" {P} origin main\nEOF\nchmod +x s.sh $(sh s.sh)",
    f"cat > .git/hooks/pre-commit <<'EOF'\n\"${{1:-git}}\" {P} origin main\nEOF\n"
    "chmod +x .git/hooks/pre-commit",
    f"cat > hooks/x <<'EOF'\n\"${{1:-git}}\" {P} origin main\nEOF\nchmod +x .git/../hooks/x",
    # A redirection or an assignment before ``git`` beside a file-written hook
    # body does not hide the ``git`` that will run the hook.
    f"cat > .husky/pre-commit <<'EOF'\n\"$G\" {P} origin main\nEOF\n</dev/null git commit -m x",
    f"cat > .husky/pre-commit <<'EOF'\n\"$G\" {P} origin main\nEOF\nGIT_DIR=. git commit -m x",
    # Beside a checkout or a script run by its path a ``gh`` body's bullet is
    # text, but a runtime push in it, a ``git`` sink beside it, or a sibling that
    # can carry code, write a file, make a glob target or redefine ``gh`` keeps
    # it command text.
    f"touch git; git switch -c x && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"git switch -c x && gh pr create --body-file - <<'EOF'\n\"${{1:-git}}\" {P} origin main\nEOF",
    f"python3 -c 'x' && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"python3 - && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"python3 -m x && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"python3 x.py $(id) && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"python3 x.py > out && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"PATH=. python3 x.py && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"git stash show --output=x && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"git stash show --out=x && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"git -c core.x=y switch -c x && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"git switch -c \"$B\" && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"git switch -c x* && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"git rebase -x 'sh' origin/main && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"git rebase --exec=x origin/main && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"git rebase -i origin/main && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"git rebase $(id) && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"git rebase && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"git rebase origin/main && git commit --amend -F - <<'EOF'\n* {P} origin main\nEOF",
    f"gh() {{ bash; }}; git switch -c x && gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"alias gh=bash; git stash pop; gh pr create --body-file - <<'EOF'\n* {P} origin main\nEOF",
    f"git switch -c x && git commit -F - <<'EOF'\n* {P} origin main\nEOF",
    f"git switch -c x && git commit -m \"$(cat <<'EOF'\n* {P} origin main\nEOF\n)\"",
    f"git switch -c x && gh pr create --body \"$(cat <<'EOF'\n* {P} origin main\nEOF\n)\""
    f" && git commit -m \"$(cat <<'EOF'\n* {P} origin main\nEOF\n)\"",
    # ``mkdir`` of a name a bullet's ``*`` can expand to is a glob target, and a
    # ``.github`` path that reaches ``.git`` or a script someone runs is still read.
    f"mkdir git; cat > /tmp/f <<'EOF'\n* {P} origin main\nEOF",
    f"mkdir -p ./git/x; cat > /tmp/f <<'EOF'\n* {P} origin main\nEOF",
    f"mkdir -p git.exe; cat > /tmp/f <<'EOF'\n* {P} origin main\nEOF",
    f"mkdir $D && cat > /tmp/f <<'EOF'\n* {P} origin main\nEOF",
    f"mkdir -p d && touch git && cat > /tmp/f <<'EOF'\n* {P} origin main\nEOF",
    f"mkdir -p .git/hooks && cat > .git/hooks/pre-commit <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"cat > .github/../.git/hooks/pre-commit <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"cat > repo.git/hooks/x <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"mkdir -p d && cat > d/s.sh <<'EOF'\n\"${{1:-git}}\" {P} origin main\nEOF\nsh d/s.sh",
    f"cat > .github/s.sh <<'EOF'\n\"${{1:-git}}\" {P} origin main\nEOF\nsh .github/s.sh",
    f"mkdir -p .github/hooks && cat > .github/hooks/pre-commit <<'EOF'\n"
    f'"${{1:-git}}" {P} origin main\nEOF\ngit commit -m x',
]

#: Heredoc delimiters this reading does not reproduce exactly: past the read
#: window, carrying a substitution or a backtick, or an escaped ANSI-C span.
UNREADABLE_HEREDOC_DELIMITERS = [
    f"cat <<{'E' * 300}\n'\n{'E' * 300}\n\"$G\" {P} origin feature",
    f'cat <<$(echo x)\n\'\n$(echo x)\n"$G" {P} origin feature',
    f'cat <<${{X}}\n\'\n${{X}}\n"$G" {P} origin feature',
    f'cat <<`x`\n\'\n`x`\n"$G" {P} origin feature',
    f"cat <<$'\\x45OF'\n'\nEOF\n\"$G\" {P} origin feature",
    f'cat <<\n"$G" {P} origin feature',
]

#: Precommands that add to or rejoin the argv written after them.
ARGV_CHANGING_PRECOMMANDS = [
    f"G=git; printf '%s\\n' main | xargs \"$G\" {P} origin feature",
    f'xargs "$G" {P} origin feature < list',
    f'watch -n5 "$G" {P} origin feature',
    f'xargs nice "$G" {P} origin feature < list',
]


@pytest.mark.parametrize(("command", "rule"), JUDGED_BY_TARGET)
def test_a_one_word_runtime_program_is_judged_by_its_target(command: str, rule: str) -> None:
    assert _is_git_publish(command.lower()) is True
    reason = is_denied(command)
    assert reason is not None, command
    assert rule in reason, reason


@pytest.mark.parametrize("command", UNVERIFIABLE_PROGRAM_WORDS)
def test_a_program_word_that_can_add_words_is_unverifiable(command: str) -> None:
    assert _is_git_publish(command.lower()) is True
    assert _is_push_to_protected_branch(command.lower()) is True
    reason = is_denied(command)
    assert reason is not None, command
    assert UNVERIFIABLE in reason, reason


@pytest.mark.parametrize("command", PROGRAM_POSITIONS)
def test_every_program_position_is_read(command: str) -> None:
    reason = is_denied(command)
    assert reason is not None, command
    assert "git-publish" in reason or UNVERIFIABLE in reason, reason


def test_a_command_substitution_is_one_segment_so_a_glued_publish_is_not_hidden() -> None:
    # A ``$(...)`` is one shell word, so a ``;`` inside it does not split the
    # command: ``git$(echo ' ';){P} origin main`` stays whole and unverifiable,
    # and a sibling runtime feature push in its own segment does not answer for
    # it. Splitting on that inner ``;`` left the glued publish in an
    # unrecognised fragment, so the protected push slipped through.
    command = f"\"$G\" {P} origin feature; git$(echo ' ';){P} origin main"
    reason = is_denied(command)
    assert reason is not None, command
    assert UNVERIFIABLE in reason, reason
    # The legitimate sibling on its own stays allowed -- the fix is fail-closed
    # on the glued publish, not a blanket refusal of the feature push.
    assert is_denied(f'"$G" {P} origin feature') is None


@pytest.mark.parametrize("command", STILL_ALLOWED)
def test_commands_that_do_not_run_a_protected_publish_stay_allowed(command: str) -> None:
    assert is_denied(command) is None


@pytest.mark.parametrize("command", HEREDOC_BODIES)
def test_a_heredoc_body_is_read_as_command_text(command: str) -> None:
    reason = is_denied(command)
    assert reason is not None, command
    assert "git-publish" in reason or UNVERIFIABLE in reason, reason


@pytest.mark.parametrize("command", UNREADABLE_HEREDOC_DELIMITERS)
def test_an_unreadable_heredoc_delimiter_is_unverifiable(command: str) -> None:
    publishes = publish_program_word._runtime_publishes(command.lower())
    assert [publish.args for publish in publishes] == [None], command
    reason = is_denied(command.replace("origin feature", "origin main"))
    assert reason is not None, command


@pytest.mark.parametrize(
    ("raw", "delimiter"),
    [
        ("EOF", "EOF"),
        ("'EOF'", "EOF"),
        ('"EOF"', "EOF"),
        ("$'EOF'", "EOF"),
        ('$"EOF"', "EOF"),
        ("E\\OF", "EOF"),
        ("E\\\nOF", "EOF"),
        ('"E\\OF"', "E\\OF"),
        ('"E\\$F"', "E$F"),
        ('E""OF', "EOF"),
        ("$X", "$X"),
        ('"$X"', "$X"),
        ("\\$'EOF'", "$EOF"),
        ("$$'EOF'", "$$EOF"),
        ("''", ""),
        ("$'\\x45OF'", None),
    ],
)
def test_a_heredoc_delimiter_is_read_by_quote_removal(raw: str, delimiter: "str | None") -> None:
    assert publish_program_word._heredoc_delimiter(raw) == delimiter


@pytest.mark.parametrize("command", ARGV_CHANGING_PRECOMMANDS)
def test_a_publish_behind_an_argv_changing_precommand_is_unverifiable(command: str) -> None:
    reason = is_denied(command)
    assert reason is not None, command
    assert UNVERIFIABLE in reason, reason


@pytest.mark.parametrize(
    ("token", "reading"),
    [
        ("</dev/null>", (True, True)),
        ("</dev/null>>", (True, True)),
        (">log>|", (True, True)),
        ("2>x&>", (True, True)),
        ("</dev/null", (True, False)),
        ("</dev/null>/dev/null", (True, False)),
        (">", (True, True)),
        ("2>&1", (True, False)),
        (">x\\>", (True, False)),
        (">'x>'", (True, False)),
    ],
)
def test_a_redirection_target_ends_at_the_next_operator(
    token: str, reading: "tuple[bool, bool]"
) -> None:
    assert publish_program_word._push_token_redirection(token) == reading


def _count_calls(monkeypatch: pytest.MonkeyPatch, name: str) -> "list[int]":
    """Wrap ``publish_program_word.<name>`` and return a one-cell call counter."""
    calls = [0]
    original = getattr(publish_program_word, name)

    def counting(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(publish_program_word, name, counting)
    return calls


def test_many_heredocs_on_one_command_find_their_bodies_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _count_calls(monkeypatch, "_heredoc_bodies_end")
    publish_program_word._runtime_publishes("cat" + " <<a" * 2000 + f" {P}\n")
    # Once by the substitution scan and once by the cut.
    assert calls[0] == 2


def test_many_substitutions_scan_in_linear_work(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _count_calls(monkeypatch, "_scan_substitutions")
    publish_program_word._runtime_publishes('echo "$(true)"' * 2000 + f" {P}")
    # One outer scan, one per substitution, then one per body read on its own.
    assert calls[0] <= 1 + 2000 * 2


def test_substitution_nesting_past_the_limit_is_unverifiable() -> None:
    depth = publish_program_word._SUBSTITUTION_DEPTH_LIMIT + 4
    text = "echo " + "$(" * depth + f'"$G" {P} origin feature' + ")" * depth
    publishes = publish_program_word._runtime_publishes(text)
    assert [publish.args for publish in publishes] == [None]


def test_heredoc_nesting_past_the_limit_is_unverifiable(monkeypatch: pytest.MonkeyPatch) -> None:
    depth = publish_program_word._HEREDOC_NESTING_LIMIT + 4
    text = "".join(f"bash <<e{i}\n" for i in range(depth)) + f'"$G" {P} origin feature\n'
    calls = _count_calls(monkeypatch, "_executable_text")
    publishes = publish_program_word._runtime_publishes(text)
    assert any(publish.args is None for publish in publishes)
    assert calls[0] <= publish_program_word._HEREDOC_NESTING_LIMIT + 1


def test_runtime_valued_flags_walk_the_suffix_once(monkeypatch: pytest.MonkeyPatch) -> None:
    words = 2000
    calls = _count_calls(monkeypatch, "_after_redirection")
    publish_program_word._runtime_publishes("env " + "-$1 " * words + f"true {P}")
    # One visit per word from the main walk and at most one from the lookahead.
    assert calls[0] <= 2 * (words + 3)
