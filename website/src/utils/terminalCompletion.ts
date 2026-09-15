/**
 * Pure logic for the web terminal's inline completion.
 *
 * Everything here operates on plain strings so it can be unit-tested without an
 * xterm instance: the caller reads the cursor's screen row out of xterm and
 * hands it in. See `TerminalCompletion.tsx` for the DOM/menu side and
 * `POST /api/terminal/complete` for the data.
 *
 * Two tiers, kept disjoint and selected here by `completionMode`:
 *
 *  - **path** — directory listings for a word that could name a file. The
 *    original scope, unchanged.
 *  - **command** — subcommands and flags for an allowlisted CLI, so `gh pr cre⎸`
 *    offers `create` instead of nothing. The data comes from the tool itself
 *    (see `dashboard/terminal_commands.py`); this module only decides WHEN to
 *    ask and how to type the answer back into the shell.
 *
 * Still out of scope: completing the command NAME itself, history search, and
 * fuzzy matching. The trigger rules below are tuned so the menu appears exactly
 * where a completion is plausible and stays out of the way everywhere else.
 */

/** Commands whose arguments are paths — completion triggers for these even on
 *  a bare token (`ls <cursor>` lists the cwd). */
const PATH_COMMANDS: ReadonlySet<string> = new Set([
  'cd', 'pushd', 'ls', 'll', 'la', 'cat', 'bat', 'less', 'more', 'head', 'tail',
  'cp', 'mv', 'rm', 'rmdir', 'mkdir', 'touch', 'ln', 'stat', 'du', 'wc', 'file',
  'open', 'code', 'vim', 'vi', 'nvim', 'nano', 'emacs', 'subl',
  'source', '.', 'chmod', 'chown', 'diff', 'rsync', 'tar', 'zip', 'unzip',
  'python', 'python3', 'node', 'sh', 'bash', 'zsh', 'go', 'cargo', 'make',
])

/** Commands that only ever take a directory — the listing drops plain files. */
const FOLDER_COMMANDS: ReadonlySet<string> = new Set([
  'cd', 'pushd', 'rmdir', 'mkdir',
])

/** Characters that end a shell word — unless the shell escaped them. Quotes are
 *  still NOT interpreted in V1: a quoted path stops producing completions
 *  rather than producing wrong ones, which `isPlainWord` enforces. */
const WORD_BREAK = /[\s]/

/** Prompt terminators, used only when the shell publishes no OSC prompt marker.
 *  Matching the LAST one before the cursor gives us the start of the command,
 *  so `~/dir (branch) » cd ../` yields `cd` and not `dir`. */
const PROMPT_SEP = /(?:❯|➜|»|▶|\$|%|#|>)[ \t]+/g

/** Shell separators that start a fresh command on the same line. */
const CMD_SEP = /\|\||&&|[|;&]/

interface TokenSpan {
  /** The word under/immediately before the cursor (may be empty). */
  token: string
  /** Column at which `token` starts — where the menu is anchored. */
  start: number
}

/** Number of backslashes ending `text`. An odd count means the character that
 *  FOLLOWS them is escaped rather than acting as itself. */
function trailingEscapes(text: string): number {
  let n = 0
  while (n < text.length && text[text.length - 1 - n] === '\\') n += 1
  return n
}

/**
 * The shell word the cursor sits at the end of. `line` is the terminal's screen
 * row (prompt included); `cursorX` is the cursor column within it.
 *
 * The word is returned VERBATIM — `my\ dir` stays escaped. `unescapeWord`
 * decodes it for the filesystem; the escaped form is what `buildInsertion`
 * needs, because that is the text actually on screen.
 */
export function extractToken(line: string, cursorX: number): TokenSpan {
  const end = Math.max(0, Math.min(cursorX, line.length))
  let start = end
  while (start > 0) {
    const prev = line[start - 1]
    if (!WORD_BREAK.test(prev)) { start -= 1; continue }
    // An escaped space belongs to the word: `cd my\ dir/` is ONE argument, and
    // stopping at it would complete `dir/` against the cwd instead of against
    // `my dir/` — the exact shape this feature's own escaping puts on screen.
    if (trailingEscapes(line.slice(0, start - 1)) % 2 === 0) break
    start -= 1
  }
  return { token: line.slice(start, end), start }
}

/**
 * Whether the cursor sits at the END of its word.
 *
 * `extractToken` only looks left of the cursor, so completing in the middle of
 * a word would append the chosen name in front of the surviving suffix and
 * corrupt the command (`cd do⎸cs` → `cd docs/cs`). Rather than attempt a
 * two-sided replacement, the menu stays shut mid-word — the same restriction
 * kiro-cli's own completer applies.
 */
export function atWordEnd(line: string, cursorX: number): boolean {
  const right = line[cursorX]
  return right === undefined || WORD_BREAK.test(right)
}

/**
 * Whether the word at `tokenStart` is one V1 can complete literally.
 *
 * Backslash escapes ARE understood (`extractToken` keeps them inside the word
 * and `unescapeWord` decodes them). Quotes are not: `cd "My D` tokenises to `D`
 * and would be completed against the cwd instead of against names starting
 * `My D`, so a quote anywhere in the word — or open before it — produces no menu
 * at all. A wrong completion here silently rewrites a path, which is worse than
 * no completion.
 *
 * `cmdStart` bounds the scan so a quote inside the PROMPT (`~/it's » `) cannot
 * suppress every completion on the line.
 */
export function isPlainWord(
  line: string, cmdStart: number, tokenStart: number, token: string,
): boolean {
  if (/["']/.test(token)) return false
  const before = line.slice(Math.max(0, cmdStart), tokenStart)
  if (/["']/.test(before)) return false
  // A word ending in a lone backslash is an escape the user has not finished
  // typing: the character it applies to is not on screen yet, so there is no
  // complete word to look up.
  if (trailingEscapes(token) % 2 === 1) return false
  // Backstop: `extractToken` keeps an escaped break inside the word, so an odd
  // run of backslashes before the break would mean the two disagree.
  const escapes = /(\\*)\s*$/.exec(before)
  return !escapes || escapes[1].length % 2 === 0
}

/**
 * A backslash-escaped shell word as the literal text the filesystem sees.
 *
 * The completion request has to carry the name the way the FILESYSTEM spells it:
 * once an accepted entry has put `my\ dir/` on screen, sending that word
 * verbatim would look for a directory whose name really contains a backslash and
 * the walk into the directory would dead-end. Only backslash quoting is decoded
 * — `isPlainWord` refuses words carrying `"` or `'` because V1 cannot tell where
 * the quoted region starts.
 */
export function unescapeWord(word: string): string {
  return word.replace(/\\([\s\S])/g, '$1')
}

/**
 * Where the current command starts on `line`.
 *
 * `markerX` is the column recorded from an OSC 133 `B` / OSC 697 `NewCmd`
 * prompt marker for this row, when the shell emits one (kiro-cli, VS Code and
 * WezTerm shell integrations all do). Without a marker we fall back to the last
 * prompt terminator before the token, which is a heuristic — see the module
 * docstring for why a wrong answer here is benign.
 */
export function commandStart(line: string, tokenStart: number, markerX?: number): number {
  if (markerX != null && markerX >= 0 && markerX <= tokenStart) return markerX
  PROMPT_SEP.lastIndex = 0
  let end = 0
  let m: RegExpExecArray | null
  const prefix = line.slice(0, tokenStart)
  while ((m = PROMPT_SEP.exec(prefix)) !== null) end = m.index + m[0].length
  return end
}

/**
 * A command segment split into shell words, NOT breaking at an escaped space.
 *
 * `extractToken` already keeps `my\ dir` together for the word under the cursor;
 * this applies the same rule to the words BEFORE it, so `gh pr view my\ branch ⎸`
 * yields four words rather than five. Escapes are left in place — the caller
 * decides whether it wants the on-screen form or the decoded one.
 */
function splitWords(segment: string): string[] {
  const words: string[] = []
  let cur = ''
  for (let i = 0; i < segment.length; i += 1) {
    const c = segment[i]
    if (c === '\\' && i + 1 < segment.length) {
      cur += c + segment[i + 1]
      i += 1
      continue
    }
    if (WORD_BREAK.test(c)) {
      if (cur) { words.push(cur); cur = '' }
      continue
    }
    cur += c
  }
  if (cur) words.push(cur)
  return words
}

/**
 * The words of the command governing the token at `tokenStart`, still escaped,
 * with leading wrappers dropped so the real tool is first.
 *
 * `commandWord` and `commandArgv` are both views of this one answer, which is
 * what keeps them from disagreeing about where the command starts — a
 * disagreement would send the backend an argv whose head is not the command the
 * trigger rules were evaluated against.
 */
function commandWords(line: string, tokenStart: number, markerX?: number): string[] {
  const segment = line.slice(commandStart(line, tokenStart, markerX), tokenStart)
  const lastCmd = segment.split(CMD_SEP).pop() ?? ''
  const words = splitWords(lastCmd)
  // `sudo cp x` / `env FOO=1 ls` — step past wrappers so the real command wins.
  let i = 0
  while (i < words.length - 1 && (WRAPPERS.has(words[i]) || words[i].includes('='))) i += 1
  return words.slice(i)
}

/**
 * The command word governing the token at `tokenStart` — `''` when the token IS
 * the command word (nothing to complete a path against yet).
 */
export function commandWord(line: string, tokenStart: number, markerX?: number): string {
  return commandWords(line, tokenStart, markerX)[0] ?? ''
}

const WRAPPERS: ReadonlySet<string> = new Set(['sudo', 'env', 'time', 'nohup', 'command', 'exec'])

/**
 * Whether a completion menu should open for this token.
 *
 * Two independent triggers, both narrow on purpose:
 *  - the token already looks like a path (`/`, `./`, `../`, `~`), whatever the
 *    command is — this is the `cd ../` and `ls ./` case;
 *  - the command is a known path command, so even a bare token lists the cwd.
 *
 * Flags (`-x`, `--long`) and variable/substitution starts never trigger.
 *
 * This governs the PATH tier only; `completionMode` layers the command tier on
 * top for the words this refuses.
 */
export function shouldComplete(token: string, command: string): boolean {
  if (token.startsWith('-') || token.startsWith('$') || token.startsWith('`')) return false
  if (command === '') return false // still typing the command name itself
  if (token.includes('/') || token.startsWith('~') || token.startsWith('.')) return true
  return PATH_COMMANDS.has(command)
}

/** Whether a word could name a file — i.e. whether the PATH tier owns it. */
function looksLikePath(token: string): boolean {
  return token.includes('/') || token.startsWith('~')
}

/** Which tier answers this word, or `none` when the menu stays shut. */
export type CompletionMode = 'path' | 'command' | 'none'

/**
 * The tier that owns the word at the cursor.
 *
 * The two tiers are kept DISJOINT, and the client is what separates them — only
 * the client can see the screen row. Path wins wherever it applied before, so
 * this cannot regress any word that already produced a menu:
 *
 *  - `path`    — `shouldComplete` says so (path-shaped word, or a known path
 *                command). Unchanged behaviour.
 *  - `command` — a word that cannot be a path (no `/`, not `~`-rooted), under a
 *                command that is not a known path command. This is the case that
 *                previously produced nothing at all: `gh pr cre⎸`, `git ⎸`,
 *                `docker --⎸`.
 *  - `none`    — no command word yet (completing the command NAME is out of
 *                scope; that needs a PATH-wide executable scan), or a shell
 *                expansion the tokenizer must not touch.
 *
 * A flag word reaches `command` but never `path`: a path listing for `-` is
 * meaningless, which is why `shouldComplete` rejects it and why that rejection is
 * NOT inherited here.
 */
export function completionMode(token: string, command: string): CompletionMode {
  if (shouldComplete(token, command)) return 'path'
  if (command === '') return 'none'
  if (token.startsWith('$') || token.startsWith('`')) return 'none'
  // A path command's bare word belongs to the path tier even when the tier
  // returns nothing — `python ⎸` must keep listing the cwd, not start probing a
  // tool that has no completion protocol.
  if (PATH_COMMANDS.has(command)) return 'none'
  if (looksLikePath(token)) return 'none'
  return 'command'
}

/**
 * The command line before the cursor's word, as an argv the backend can probe.
 *
 * `["gh", "pr"]` for `gh pr cre⎸`. Words are DECODED (`unescapeWord`) because
 * argv entries are literal — they are handed to `execve`, not to a shell — and a
 * backslash that survived would become part of the value.
 *
 * Wrapper words are stripped so the real tool lands at argv[0] (`sudo docker ps⎸`
 * probes `docker`), matching what `commandWord` already reports. Flag words are
 * KEPT: cobra's position in its own command tree depends on them, and dropping
 * `--namespace kube-system` would change the answer.
 */
export function commandArgv(line: string, tokenStart: number, markerX?: number): string[] {
  return commandWords(line, tokenStart, markerX).map(unescapeWord)
}

/** Whether the listing should be restricted to directories. */
export function foldersOnly(command: string): boolean {
  return FOLDER_COMMANDS.has(command)
}

/** Longest common prefix of `names` (case-sensitive; `''` when empty). */
export function commonPrefix(names: readonly string[]): string {
  if (names.length === 0) return ''
  // Compared as code points, not UTF-16 units: two names differing only in a
  // supplementary character (`𝐀` vs `𝐁`) share a leading surrogate, and cutting
  // between the halves would type a lone surrogate the PTY receives as U+FFFD.
  let out = [...names[0]]
  for (const n of names.slice(1)) {
    const other = [...n]
    let i = 0
    while (i < out.length && i < other.length && out[i] === other[i]) i += 1
    out = out.slice(0, i)
    if (out.length === 0) break
  }
  return out.join('')
}

/**
 * Whether `common` is worth inserting over the already-typed `prefix`.
 *
 * Server-side matching is case-insensitive, so the entries behind a common
 * prefix need not share the typed text's case — or even its length. `doc`
 * matching `Docs` and `DoConfig` yields the common prefix `Do`, which is SHORTER
 * than what the user typed; inserting it would delete a character they meant to
 * keep. So a candidate must be at least as long as the prefix AND agree with it
 * case-insensitively; an equal-length candidate differing only in case is still
 * worth inserting, since it corrects the word to what exists on disk.
 */
export function extendsWord(common: string, prefix: string): boolean {
  if (!common || common.length < prefix.length) return false
  if (!common.toLowerCase().startsWith(prefix.toLowerCase())) return false
  return common !== prefix
}

interface Insertion {
  /** Number of DEL bytes to send before `text` (rewrites a case mismatch). */
  erase: number
  /** Literal text to send to the PTY. */
  text: string
}

/**
 * Characters no completion may ever type.
 *
 * `\p{Cc}` — control characters. Everything the menu offers comes off the
 * filesystem, and whatever is accepted is written straight into the PTY as if
 * the user had typed it. A directory entry may legally contain a newline:
 * `touch $'evil\nrm -rf x'` creates one, and typing it would submit the second
 * line to the shell as a command. There is no escape that makes a CR/LF safe to
 * type, so such names are refused outright rather than sanitised.
 *
 * `\p{Cs}` — lone surrogates, which reach us when the server decodes an
 * undecodable filename byte with surrogateescape. JSON carries them, but
 * `TextEncoder` replaces each with U+FFFD, so accepting one would type a path
 * that does not exist on disk. (A well-formed pair is a supplementary code
 * point, not `Cs`, so ordinary emoji and CJK names are unaffected.)
 */
const UNSAFE_NAME = /[\p{Cc}\p{Cs}]/u

/** Whether a filesystem name may be typed into the shell at all. */
export function isSafeName(name: string): boolean {
  return !UNSAFE_NAME.test(name)
}

/**
 * A subcommand name (`pr`, `dry-run`, `run:build`, `v2.0`).
 *
 * `:` is allowed: a subcommand is argv[1], never a path, so the `host:path`
 * ambiguity that the path guard exists for cannot arise — and script-runner
 * subcommands really are spelled that way.
 */
const SUBCOMMAND_NAME = /^[\p{L}\p{N}][\p{L}\p{N}._+:@-]*$/u

/** A flag (`-v`, `--repo`, `--dry-run`, `--message=`). */
const FLAG_NAME = /^--?[\p{L}\p{N}][\p{L}\p{N}._-]*=?$/u

/**
 * Whether a command-tier value is one this module will type into the shell.
 *
 * The path tier ESCAPES what it types, because a filename is arbitrary bytes the
 * user chose and escaping is the only way to make it a single shell word. A
 * subcommand or flag is the opposite: it is a token the TOOL defined, drawn from
 * a closed vocabulary, and it is already a plain shell word or it is not a real
 * flag at all. So the command tier validates instead of escaping — which is both
 * safer (an unexpected value is refused outright rather than smuggled through as
 * an escaped literal) and correct on screen: escaping would put `--message\=` on
 * the line where the tool asked for `--message=`.
 */
export function isCommandToken(name: string, isFlag: boolean): boolean {
  if (!isSafeName(name)) return false
  return isFlag ? FLAG_NAME.test(name) : SUBCOMMAND_NAME.test(name)
}

/**
 * Characters that must be backslash-escaped to survive as literal text.
 *
 * An allowlist, not a denylist (the same set `shlex.quote` treats as safe, minus
 * `=` for zsh's equals-expansion): anything unlisted gets a backslash, so a
 * metacharacter added by a future shell is escaped by default rather than
 * forgotten. Letters and digits are matched by Unicode property so CJK and
 * accented names stay readable instead of being escaped byte by byte.
 */
const NEEDS_ESCAPE = /[^\p{L}\p{N}_@%+:,./-]/gu

/**
 * A filesystem name as literal shell text.
 *
 * Backslashes rather than quotes on purpose: the escaped name stays ONE shell
 * word with no opening quote to close, so the echoed text still tokenises as a
 * single word and accepting a directory can immediately re-trigger on its
 * contents. Wrapping in quotes would instead put the cursor inside a quoted
 * region, which `isPlainWord` (correctly) refuses to complete against, dead-
 * ending the very walk-into-the-directory flow the trailing `/` exists for.
 */
export function shellEscape(text: string): string {
  return text.replace(NEEDS_ESCAPE, (c) => '\\' + c)
}

/**
 * Index of the last `/` in `word`, which is always a path separator.
 *
 * Backslash parity is deliberately NOT consulted. A shell dequotes `\/` to `/`,
 * so an escaped slash still separates path components — and the completion
 * request carries the DECODED word, meaning the server splits `sub\/do` at that
 * slash and answers with prefix `do`. Skipping it here would make the two sides
 * disagree: acceptance would treat the whole `sub\/do` as the name to rewrite
 * and erase the directory part along with it.
 */
function lastSeparator(word: string): number {
  return word.lastIndexOf('/')
}

/**
 * Keystrokes that turn the word on screen into `replacement`.
 *
 * `word` is the shell word EXACTLY as the terminal shows it (escapes intact) and
 * `replacement` is a filesystem name for its last path segment. Escaping happens
 * here, at the single choke point every insertion path (Enter, Tab's common
 * prefix) goes through — a caller cannot forget to escape.
 *
 * When the escaped replacement extends what is on screen, only the missing
 * suffix is appended, so the shell's own line (and any autosuggestion) is left
 * untouched. Otherwise the last segment is erased with DEL and retyped whole —
 * the case for a case-insensitive hit (`Docs` for `docs`), for a substring hit
 * where the typed text sits in the MIDDLE of the name (`termi` →
 * `KiroCrew-terminal-completion`), for a name whose escaping alters characters
 * the user already typed, and for the `./` guard below.
 */
/**
 * Whether a bare filename would be read as something OTHER than a local path.
 *
 * Shell metacharacters are handled by `shellEscape`, but escaping is the wrong
 * tool here: these shapes are interpreted by the PROGRAM after the shell has
 * already split the word, so `\\-c` is still the option `-c` and `host\\:path`
 * is still a remote target. Only making the path explicit with `./` removes the
 * ambiguity.
 *
 * Two shapes qualify:
 *
 * * a leading `-` or `+` — an option to whatever runs next (`vim -c:!sh evil`
 *   runs a command; `+{cmd}` is vim's own startup-command prefix, so
 *   `vim +:!id` runs `id`);
 * * a `:` anywhere in a name with no directory part — `rsync`/`scp` read
 *   `host:path` as a REMOTE target, so completing a local file called
 *   `attacker.example:drop` would upload the other argument off the machine.
 *
 * Handled as a class: each is a case of the program, not the shell,
 * reinterpreting the word, and the guard costs a two-character prefix while the
 * failure costs command execution or exfiltration.
 */
const NOT_A_PLAIN_PATH = /^[-+]|:/

export function buildInsertion(
  word: string, replacement: string, suffix = '', isPath = true,
): Insertion {
  const sep = lastSeparator(word)
  // Only the last segment is rewritten; any directory part stays on screen.
  const onScreen = word.slice(sep + 1)
  // Escaped only in path mode. A command token has been through
  // `isCommandToken`, so it is already a plain shell word — escaping it would
  // only corrupt it (`--message\=` for the tool's `--message=`), and a token that
  // WOULD need escaping was refused rather than offered.
  let text = isPath ? shellEscape(replacement) : replacement
  // A name starting with `-` or `+` is an OPTION to whatever runs next, not a
  // path, and no escaping changes that: `\-c` is still `-c`, so `vim -c:!sh evil`
  // would run a command, and `+` is vim's own command prefix, so `vim +:!id` runs
  // `id`. `./` makes either unambiguously a path. Needed only when the word has
  // no directory part — with one (`sub/-x`) the argument already cannot parse as
  // an option.
  //
  // `isPath` is what keeps the guard from firing on the completion that is
  // SUPPOSED to be an option. A filesystem entry called `--force` must be typed
  // as `./--force` or it silently becomes a flag; the flag `--force` offered by
  // the command tier must be typed verbatim, and `./--force` would be a path that
  // does not exist. Same characters, opposite correct answers — so the guard is a
  // property of the entry's KIND, not of its spelling.
  if (isPath && NOT_A_PLAIN_PATH.test(replacement) && sep < 0) text = './' + text
  // Compared against the ESCAPED on-screen text, not the decoded name: after an
  // accepted `my dir/` the screen reads `my\ dir/`, so a decoded comparison
  // would mis-measure what is already typed.
  if (text.startsWith(onScreen)) return { erase: 0, text: text.slice(onScreen.length) + suffix }
  // One DEL erases one CHARACTER from the shell's line editor, so the count is in
  // code points: `onScreen.length` would send two DELs for one supplementary
  // character and eat the character before it. (`readWord` refuses rows holding
  // such cells outright, so this is a backstop rather than the only guard.)
  return { erase: [...onScreen].length, text: text + suffix }
}

/** Trailing character appended when a completion is accepted outright. */
export function acceptSuffix(isDir: boolean): string {
  return isDir ? '/' : ' '
}

/**
 * Trailing character for an accepted SUBCOMMAND or FLAG.
 *
 * A space, so the next word starts and the menu can re-trigger one level deeper
 * (`gh` → `gh pr ` → `gh pr create `) — except when the protocol said the value
 * is incomplete (cobra's `NoSpace` directive, git's `--message=` form), where a
 * separator would strand the cursor after a flag that still needs its value.
 */
export function commandSuffix(nospace: boolean): string {
  return nospace ? '' : ' '
}
