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
    (f'"$G" {P} origin main eval {GIT_FEATURE}', PROTECTED),
    (f"\"$G\" {P} origin main eval '{GIT_FEATURE}'", PROTECTED),
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
    # A ``case`` inside a command substitution is read, so a real push in its
    # body is found rather than invented or missed.
    f'$(case x in x) "$G" {P} origin main;; esac)',
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
]

STILL_ALLOWED = [
    GIT_FEATURE,
    f'"$G" {P} origin feature',
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
    f'"$GIT" -C "$REPO" {P} origin feat/example',
    f'"${{HELM}}" {P} chart-0.1.0.tgz oci://ghcr.io/example-org/charts',
    f'"${{G}}" {P} origin feature; "${{G}}" {P} origin topic',
    f'"$G" {P}>log.txt origin feature',
    f'"$G"</dev/null {P} origin feature',
    f'echo "$x">out.txt {P}',
    f"ls !(build) {P}",
    f'duration=5; G=git; timeout "$duration" echo "$G" {P} origin main',
    f'"$G" -C "${{REPO}}" {P} origin feature',
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
    f"git log --oneline -5; gh pr create --body-file - <<'EOF'\n* {P} notes for the release\nEOF",
    f'echo "$(case x in x) echo {P};; esac)"',
    f'echo "$(case x in (x) echo {P};; esac)"',
    f'git commit -m "$(echo case) {P}"',
    f'set -- git; "$@" "$G" {P} origin feature',
    f'"${{GIT:-git}}" {P} origin feature',
    f'"$HOME\\bin\\git.exe" {P} origin feat/example',
    f'& "$env:ProgramFiles\\Git\\cmd\\git.exe" {P} origin feat/example',
    f'"$env:ANDROID_HOME\\platform-tools\\adb.exe" {P} app-debug.apk /data/local/tmp/',
]

#: Heredoc bodies are read as command text: what consumes one may run it, and
#: no reading of the redirections can prove the body reaches only a file.
HEREDOC_BODIES = [
    f"cat > notes.md <<'EOF'\n\"$G\" {P} origin main\nEOF",
    f"cat >>notes.md <<-'EOF'\n\t\"$G\" {P} origin main\n\tEOF",
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
