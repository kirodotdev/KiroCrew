/**
 * Shell-command classifier: a port of Codex's `parse_command.rs`
 * (`codex-rs/shell-command`) that turns a script into a `ToolAction`.
 *
 * The script is accepted only when it consists of plain words, quoted strings
 * and the connectors `&&` `||` `;` `|`; each segment is classified as Read /
 * List files / Search / (our additions) Git / package-manager / print / GitHub
 * CLI / curl; `cd` only moves the cwd used to resolve later relative paths;
 * small formatting helpers (`wc`, `head -n 40`, …) are dropped from pipelines.
 * **If any segment stays Unknown the whole result is null** and the caller shows
 * the raw command — a half-templated title would hide the unparsed action at
 * approval time (claude-agent-acp#1068 is the user backlash that rule prevents).
 *
 * One deliberate extension past Codex's strict grammar, display-only: the
 * stderr-silencing redirects `2>/dev/null` / `2>&1` are stripped before
 * parsing. Any other redirect, expansion, glob or subshell still rejects the
 * script. Two deliberate departures the other way, both safety: a `VAR=value`
 * env prefix (which Codex strips) keeps the whole command raw, because the
 * prefix can change what the command does; and a segment that writes, uploads,
 * deletes or rewrites history through a flag the template would not show
 * (`curl -T`, `find -delete`, `xargs rm`, `git push --force`, `gh api -X`)
 * stays raw too. The title may say less than the command only in detail, never
 * in effect. The invariant behind every such rule: **no templated segment may
 * carry code or an exec-capable option.** An interpreter (`sed`, `awk`, `perl`,
 * `ruby`, `python`, `node`) is never an `xargs` target, and is a formatting
 * tail or a read only when its script matches a positive read-only grammar
 * (`parseSedPure`, `awkProgramIsPure`); a tool with an option that runs a
 * program (`find -exec`, `fd -x`, `rg --pre`, `git -c`, `xargs <anything not
 * on the read-only list>`) is raw whenever that option is present.
 *
 * Rule-change policy: every change to a rule here lands in the same commit
 * with fixture cases in `test/fixtures/tool_call_titles.json` that exercise it,
 * and with the matching change in `src/kiro_crew/tool_call_title.py`; the
 * fixture is what makes the two implementations one rule set.
 *
 * EVERY string literal in this module is CLI syntax — command names, option
 * flags, marker substrings the parser matches on — never user-visible copy;
 * translating one would break the parser. The module is therefore a named
 * boundary in `eslint.i18n.config.js` (same idiom as `envShellCommands.ts`),
 * and the copy it feeds lives in `toolCallTitle.ts` under `i18nT`. Keep it that
 * way: anything a person reads belongs there.
 *
 * Mirrored by `src/kiro_crew/tool_call_title.py`; both are pinned to
 * `test/fixtures/tool_call_titles.json`.
 */

import type { ToolAction } from './toolAction'

// ---------------------------------------------------------------------------
// Tokenizer — Codex's "word-only commands sequence" grammar
// ---------------------------------------------------------------------------

type Tok = { op: string } | { word: string }

/** Characters that make an unquoted word non-literal (expansion, glob, escape,
 *  brace, tilde, comment, history) — Codex `is_literal_word_or_number`. */
const BARE_REJECT = new Set(['{', '}', '*', '?', '[', ']', '\\', '~', '^', '#', '$', '`'])
/** stderr-silencing redirects that change nothing about what a command DOES;
 *  stripped before parsing (display-only extension past Codex). */
const NOISE_REDIRECT_RE = /(?:^|\s)(?:2>&1|[12]?>>?\s*\/dev\/null|&>\s*\/dev\/null)(?=\s|$)/g
const ENV_ASSIGN_RE = /^[A-Za-z_]\w*=/

/** Tokenize a shell script into words and connectors, or null when the script
 *  uses anything outside the plain-words grammar (redirects, `$VAR`, `$(…)`,
 *  globs, subshells, heredocs, backgrounding, comments). */
export function tokenizeShell(script: string): Tok[] | null {
  const src = script.replace(NOISE_REDIRECT_RE, ' ')
  const out: Tok[] = []
  let word = ''
  let hasWord = false
  let i = 0
  const flush = () => {
    if (hasWord) out.push({ word })
    word = ''
    hasWord = false
  }
  while (i < src.length) {
    const ch = src[i]
    if (ch === ' ' || ch === '\t' || ch === '\r') { flush(); i++; continue }
    if (ch === '\n') { flush(); out.push({ op: ';' }); i++; continue }
    if (ch === '&' || ch === '|' || ch === ';') {
      flush()
      const two = src.slice(i, i + 2)
      if (two === '&&' || two === '||') { out.push({ op: two }); i += 2; continue }
      if (ch === '&') return null // backgrounding / redirect fragment
      out.push({ op: ch })
      i++
      continue
    }
    if (ch === '<' || ch === '>' || ch === '(' || ch === ')') return null
    if (ch === "'") {
      const end = src.indexOf("'", i + 1)
      if (end < 0) return null
      word += src.slice(i + 1, end)
      hasWord = true
      i = end + 1
      continue
    }
    if (ch === '"') {
      let j = i + 1
      let buf = ''
      let closed = false
      while (j < src.length) {
        const c = src[j]
        if (c === '"') { closed = true; break }
        if (c === '$' || c === '`') return null // expansion inside quotes
        if (c === '\\' && j + 1 < src.length && '"\\'.includes(src[j + 1])) { buf += src[j + 1]; j += 2; continue }
        if (c === '\\') return null
        buf += c
        j++
      }
      if (!closed) return null
      word += buf
      hasWord = true
      i = j + 1
      continue
    }
    if (BARE_REJECT.has(ch)) return null
    word += ch
    hasWord = true
    i++
  }
  flush()
  return out
}

function splitSegments(tokens: Tok[]): string[][] {
  const segs: string[][] = []
  let cur: string[] = []
  for (const t of tokens) {
    if ('op' in t) {
      if (cur.length) segs.push(cur)
      cur = []
    } else {
      cur.push(t.word)
    }
  }
  if (cur.length) segs.push(cur)
  return segs
}

// ---------------------------------------------------------------------------
// Shell helpers — ports of the Codex helper set
// ---------------------------------------------------------------------------

const SHORT_PATH_SKIP = new Set(['build', 'dist', 'node_modules', 'src'])

/** Last path component, skipping `build`/`dist`/`node_modules`/`src` (Codex
 *  `short_display_path`): `webview/src` -> `webview`, `packages/app/node_modules/` -> `app`. */
export function shortDisplayPath(path: string): string {
  const trimmed = path.replace(/\\/g, '/').replace(/\/+$/, '')
  const parts = trimmed.split('/').filter(p => p && !SHORT_PATH_SKIP.has(p))
  return parts.length ? parts[parts.length - 1] : trimmed
}

function isDigits(s: string): boolean {
  return s.length > 0 && /^[0-9]+$/.test(s)
}

function isPathish(s: string): boolean {
  return s === '.' || s === '..' || s.startsWith('./') || s.startsWith('../') || s.includes('/') || s.includes('\\')
}

function isAbsLike(p: string): boolean {
  return p.startsWith('/') || /^[A-Za-z]:\\/.test(p) || p.startsWith('\\\\')
}

function joinPaths(base: string, rel: string): string {
  if (isAbsLike(rel) || !base) return rel
  return base.replace(/\/+$/, '') + '/' + rel
}

/** Skip values consumed by `flagsWithVals` and `--flag=value` forms; `--`
 *  passes everything after it through (Codex `skip_flag_values`). */
function skipFlagValues(args: string[], flagsWithVals: string[]): string[] {
  const out: string[] = []
  let skipNext = false
  for (let i = 0; i < args.length; i++) {
    const a = args[i]
    if (skipNext) { skipNext = false; continue }
    if (a === '--') { out.push(...args.slice(i + 1)); break }
    if (a.startsWith('--') && a.includes('=')) continue
    if (flagsWithVals.includes(a)) { if (i + 1 < args.length) skipNext = true; continue }
    out.push(a)
  }
  return out
}

/** Non-flag operands after flag-value skipping (Codex `positional_operands`). */
function positionalOperands(args: string[], flagsWithVals: string[]): string[] {
  const out: string[] = []
  let afterDD = false
  let skipNext = false
  for (let i = 0; i < args.length; i++) {
    const a = args[i]
    if (skipNext) { skipNext = false; continue }
    if (afterDD) { out.push(a); continue }
    if (a === '--') { afterDD = true; continue }
    if (a.startsWith('--') && a.includes('=')) continue
    if (flagsWithVals.includes(a)) { if (i + 1 < args.length) skipNext = true; continue }
    if (a.startsWith('-')) continue
    out.push(a)
  }
  return out
}

function firstNonFlagOperand(args: string[], flagsWithVals: string[]): string | undefined {
  return positionalOperands(args, flagsWithVals)[0]
}

function singleNonFlagOperand(args: string[], flagsWithVals: string[]): string | undefined {
  const ops = positionalOperands(args, flagsWithVals)
  return ops.length === 1 ? ops[0] : undefined
}

/** sed flags a read-only invocation may carry. Anything else (`-i`, `-f`, `-s`,
 *  `--debug`, an unknown flag) leaves the command raw: the allowlist is the rule. */
const SED_PURE_FLAGS = new Set(['-n', '--quiet', '--silent', '-E', '-r', '--regexp-extended', '-z', '--null-data', '-u', '--unbuffered'])
/** A sed script that only selects or prints lines: `12p`, `10,20p`, `$p`, `3d`, `1,5d`. */
const SED_RANGE_SCRIPT_RE = /^(\d+|\$)(,(\d+|\$))?[pd]$/
/** True for a substitute `s<d>pattern<d>replacement<d>flags` with a
 *  non-alphanumeric delimiter and only g/i/I/p/number flags — never `w file` or
 *  `e`, the two flags through which `s` leaves the stream. A linear walk (a
 *  backslash escapes the next character) rather than a regex: an alternation
 *  of `\\.` and `[^d]` backtracks exponentially on runs of `\a`. */
function isSedSubstituteScript(script: string): boolean {
  if (script.length < 4 || script[0] !== 's') return false
  const d = script[1]
  if (/[A-Za-z0-9\\\s]/.test(d)) return false
  let i = 2
  let delims = 0
  while (i < script.length && delims < 2) {
    const c = script[i]
    if (c === '\\') { i += 2; continue }
    if (c === d) delims++
    i++
  }
  if (delims !== 2) return false
  return /^[gIip0-9]*$/.test(script.slice(i))
}

/** Parsed sed invocation: every script and every file operand, or undefined when
 *  a flag outside the allowlist, a script file, or a non-pure script is present. */
function parseSedPure(args: string[]): { scripts: string[]; files: string[]; quiet: boolean } | undefined {
  const scripts: string[] = []
  const operands: string[] = []
  let quiet = false
  let afterDD = false
  for (let i = 0; i < args.length; i++) {
    const a = args[i]
    if (afterDD) { operands.push(a); continue }
    if (a === '--') { afterDD = true; continue }
    if (a === '-e' || a === '--expression') { if (i + 1 >= args.length) return undefined; scripts.push(args[++i]); continue }
    if (a.startsWith('--expression=')) { scripts.push(a.slice('--expression='.length)); continue }
    if (a.startsWith('-')) {
      if (a === '-n' || a === '--quiet' || a === '--silent') quiet = true
      if (SED_PURE_FLAGS.has(a)) continue
      // A short cluster of allowed letters (`-nE`, `-rn`); `e` must be last and takes the next token.
      if (/^-[nErzu]+$/.test(a)) { if (a.includes('n')) quiet = true; continue }
      if (/^-[nErzu]*e$/.test(a)) { if (a.includes('n')) quiet = true; if (i + 1 >= args.length) return undefined; scripts.push(args[++i]); continue }
      return undefined
    }
    operands.push(a)
  }
  if (scripts.length === 0) {
    if (operands.length === 0) return undefined
    scripts.push(operands.shift() as string)
  }
  for (const sc of scripts) {
    if (!SED_RANGE_SCRIPT_RE.test(sc) && !isSedSubstituteScript(sc)) return undefined
  }
  return { scripts, files: operands, quiet }
}

/** `sed -n <range>p file`: a read of `file`. Requires `-n` and exactly one range-print script. */
function sedReadPath(args: string[]): string | undefined {
  const parsed = parseSedPure(args)
  if (!parsed || !parsed.quiet || parsed.files.length !== 1) return undefined
  if (parsed.scripts.length !== 1 || !/p$/.test(parsed.scripts[0]) || !SED_RANGE_SCRIPT_RE.test(parsed.scripts[0])) return undefined
  return parsed.files[0]
}

/** sed as a pipeline filter: pure scripts and no file operand. */
function sedIsPureFilter(args: string[]): boolean {
  const parsed = parseSedPure(args)
  return parsed !== undefined && parsed.files.length === 0
}

/** awk's whole surface beyond its input: `system()`, `getline` (from a file or
 *  a command), output redirection and pipes (`>`, `>>`, `|`), `close`/`fflush`.
 *  A program using any of them is code, not a filter, and the command stays raw. */
const AWK_SIDE_EFFECT_RE = /system\s*\(|getline|[>|@]|close\s*\(|fflush\s*\(/
/** Every function an awk filter program may CALL. A call to anything else — a
 *  user function, a gawk extension — makes the program code, not a filter. */
const AWK_PURE_BUILTINS = new Set(['print', 'printf', 'length', 'substr', 'split', 'index', 'match', 'sub', 'gsub', 'sprintf', 'toupper', 'tolower', 'int', 'sqrt', 'exp', 'log', 'sin', 'cos', 'atan2', 'rand', 'strtonum', 'if', 'while', 'for'])

function awkProgramIsPure(program: string): boolean {
  if (AWK_SIDE_EFFECT_RE.test(program)) return false
  const calls = program.matchAll(/([A-Za-z_][A-Za-z0-9_]*)\s*\(/g)
  for (const m of calls) if (!AWK_PURE_BUILTINS.has(m[1])) return false
  return true
}

function awkDataFileOperand(args: string[]): string | undefined {
  if (args.length === 0) return undefined
  // A program read from a file cannot be inspected; it is not a read of the data file.
  if (args.some(a => a === '-f' || a === '--file' || a.startsWith('--file='))) return undefined
  const nonFlags = skipFlagValues(args, ['-F', '-v', '--field-separator', '--assign']).filter(a => !a.startsWith('-'))
  if (nonFlags.length < 2) return undefined
  if (!awkProgramIsPure(nonFlags[0])) return undefined
  return nonFlags[1]
}

/** True when awk is a pure filter: an inline program with no side-effect construct. */
function awkIsPureFilter(args: string[]): boolean {
  if (args.some(a => a === '-f' || a === '--file' || a.startsWith('--file='))) return false
  const nonFlags = skipFlagValues(args, ['-F', '-v', '--field-separator', '--assign']).filter(a => !a.startsWith('-'))
  return nonFlags.length === 1 && awkProgramIsPure(nonFlags[0])
}

function isPythonCommand(cmd: string): boolean {
  return cmd === 'python' || cmd === 'python2' || cmd === 'python3' || cmd.startsWith('python2.') || cmd.startsWith('python3.')
}

function cdTarget(args: string[]): string | undefined {
  let target: string | undefined
  for (let i = 0; i < args.length; i++) {
    const a = args[i]
    if (a === '--') return args[i + 1]
    if (a === '-L' || a === '-P' || a.startsWith('-')) continue
    target = a
  }
  return target
}

function parseGrepLike(args: string[]): ToolAction {
  const operands: string[] = []
  let pattern: string | undefined
  let afterDD = false
  for (let i = 0; i < args.length; i++) {
    const a = args[i]
    if (afterDD) { operands.push(a); continue }
    if (a === '--') { afterDD = true; continue }
    if (a === '-e' || a === '--regexp' || a === '-f' || a === '--file') {
      if (i + 1 < args.length && pattern === undefined) pattern = args[i + 1]
      i++
      continue
    }
    if (['-m', '--max-count', '-C', '--context', '-A', '--after-context', '-B', '--before-context'].includes(a)) { i++; continue }
    if (a.startsWith('-')) continue
    operands.push(a)
  }
  const hasPattern = pattern !== undefined
  const query = hasPattern ? pattern : operands[0]
  const pathIdx = hasPattern ? 0 : 1
  const path = operands[pathIdx]
  if (query === undefined) return { type: 'unknown', cmd: '' }
  return { type: 'search', query, ...(path ? { path: shortDisplayPath(path) } : {}) }
}

function parseFdQueryAndPath(tail: string[]): { query?: string; path?: string } {
  const nonFlags = skipFlagValues(tail, ['-t', '--type', '-e', '--extension', '-E', '--exclude', '--search-path']).filter(a => !a.startsWith('-'))
  if (nonFlags.length === 1) {
    return isPathish(nonFlags[0]) ? { path: shortDisplayPath(nonFlags[0]) } : { query: nonFlags[0] }
  }
  if (nonFlags.length >= 2) return { query: nonFlags[0], path: shortDisplayPath(nonFlags[1]) }
  return {}
}

/** `find` actions that mutate or run arbitrary commands; such a `find` is never a search. */
const FIND_MUTATING_ACTIONS = new Set(['-delete', '-exec', '-execdir', '-ok', '-okdir', '-fprint', '-fprint0', '-fprintf', '-fls'])

function findMutates(tail: string[]): boolean {
  return tail.some(a => FIND_MUTATING_ACTIONS.has(a))
}

function parseFindQueryAndPath(tail: string[]): { query?: string; path?: string } {
  let path: string | undefined
  for (const a of tail) {
    if (!a.startsWith('-') && a !== '!' && a !== '(' && a !== ')') { path = shortDisplayPath(a); break }
  }
  let query: string | undefined
  for (let i = 0; i < tail.length; i++) {
    const a = tail[i]
    if (a === '-name' || a === '-iname' || a === '-path' || a === '-regex') {
      if (i + 1 < tail.length) query = tail[i + 1]
      break
    }
  }
  return { query, path }
}

// ---------------------------------------------------------------------------
// Shell helpers — formatting-helper detection (Codex `is_small_formatting_command`)
// ---------------------------------------------------------------------------

// `tee` is deliberately absent: it writes its operands, so a pipeline ending in
// `tee out.txt` is not a formatting tail and must fall back to the raw command.
const ALWAYS_FORMATTING = new Set(['wc', 'tr', 'cut', 'uniq', 'column', 'yes', 'printf'])

/**
 * Commands `xargs` may fan out to and still count as a formatting tail. Every
 * entry only reads its operands. Anything else (`rm`, `mv`, `chmod`, `git`,
 * `kubectl`, an unknown binary) keeps the `xargs` segment in the pipeline,
 * where it classifies as Unknown and drops the whole command to the raw
 * fallback — the person approving `find . -name '*.log' | xargs rm -f` must
 * see `rm`, not "Search '*.log' in .".
 */
const XARGS_READ_ONLY_TARGETS = new Set([
  'cat', 'grep', 'egrep', 'fgrep', 'rg', 'ag', 'wc', 'head', 'tail', 'ls', 'stat', 'file', 'echo',
  'sort', 'uniq', 'cut', 'tr', 'column', 'md5sum', 'sha256sum', 'du',
])
// No interpreter (sed, awk, perl, ruby, python, node) is ever an xargs target: the
// program it runs is code, and "xargs <interpreter>" is where a title would hide it.

/** ripgrep's exec-capable options: a preprocessor command run per file. */
function rgHasPreprocessor(args: string[]): boolean {
  return args.some(a => a === '--pre' || a.startsWith('--pre=') || a === '--pre-glob' || a.startsWith('--pre-glob='))
}

function isPlusDigits(s: string): boolean {
  return s.startsWith('+') ? isDigits(s.slice(1)) : isDigits(s)
}

function sortWritesFile(args: string[]): boolean {
  return args.some(a => a === '-o' || a.startsWith('--output') || (a.startsWith('-') && !a.startsWith('--') && a.length > 1 && a.slice(1).includes('o')))
}

function xargsSubcommand(tokens: string[]): string[] | undefined {
  if (tokens[0] !== 'xargs') return undefined
  let i = 1
  while (i < tokens.length) {
    const t = tokens[i]
    if (t === '--') return tokens.length > i + 1 ? tokens.slice(i + 1) : undefined
    if (!t.startsWith('-')) return tokens.slice(i)
    const takesValue = ['-E', '-e', '-I', '-L', '-n', '-P', '-s'].includes(t)
    i += takesValue && t.length === 2 ? 2 : 1
  }
  return undefined
}

/**
 * True unless the `xargs` target is a read-only command used read-only. A bare
 * `xargs` (implicit `echo`) is not mutating; a target outside
 * `XARGS_READ_ONLY_TARGETS` is, whatever its flags — the allowlist is the rule,
 * not a denylist of known-destructive names.
 */
function isMutatingXargs(tokens: string[]): boolean {
  const sub = xargsSubcommand(tokens)
  if (!sub || sub.length === 0) return false
  const [head, ...tail] = sub
  if (!XARGS_READ_ONLY_TARGETS.has(head)) return true
  if (head === 'rg') return tail.includes('--replace') || tail.includes('-r') || rgHasPreprocessor(tail)
  if (head === 'sort') return sortWritesFile(tail)
  return false
}

function isSmallFormattingCommand(tokens: string[]): boolean {
  if (tokens.length === 0) return false
  const cmd = tokens[0]
  if (ALWAYS_FORMATTING.has(cmd)) return true
  if (cmd === 'sort') return !sortWritesFile(tokens.slice(1))
  if (cmd === 'xargs') return !isMutatingXargs(tokens)
  if (cmd === 'awk') return awkIsPureFilter(tokens.slice(1))
  if (cmd === 'head') {
    if (tokens.length === 1) return true
    if (tokens.length === 2) return tokens[1].startsWith('-')
    if (tokens.length === 3 && (tokens[1] === '-n' || tokens[1] === '-c') && isDigits(tokens[2])) return true
    return false
  }
  if (cmd === 'tail') {
    if (tokens.length === 1) return true
    if (tokens.length === 2) return tokens[1].startsWith('-')
    if (tokens.length === 3 && (tokens[1] === '-n' || tokens[1] === '-c') && isPlusDigits(tokens[2])) return true
    return false
  }
  if (cmd === 'sed') return sedIsPureFilter(tokens.slice(1))
  return false
}

// ---------------------------------------------------------------------------
// Shell — per-segment classification (Codex `summarize_main_tokens` + our additions)
// ---------------------------------------------------------------------------

/** Git flags that rewrite history, delete refs or discard work. A title of
 *  `Git push` for `git push --force` says less than the command in effect, so
 *  any of these leaves the command raw. `-d`/`-D`/`--delete` count only on the
 *  subcommands where they delete a ref. */
const GIT_DESTRUCTIVE_FLAGS = new Set(['--force', '-f', '--force-if-includes', '--hard', '--mirror', '--prune', '--delete', '-D'])
const GIT_REF_DELETING_SUBS = new Set(['push', 'branch', 'tag', 'remote'])
const GIT_SUBCOMMANDS = new Set([
  'status', 'diff', 'log', 'show', 'add', 'commit', 'push', 'pull', 'fetch', 'checkout', 'switch', 'branch',
  'rebase', 'merge', 'stash', 'worktree', 'clone', 'rev-parse', 'ls-remote', 'blame', 'restore', 'reset', 'tag', 'remote', 'rm', 'mv',
])
/** Sub-commands whose positional operands are pathspecs, not refs. */
const GIT_PATHSPEC_SUBS = new Set(['status', 'diff', 'log', 'show', 'add', 'blame', 'restore', 'rm', 'mv'])
/** Sub-commands whose operands are refs unless separated by `--`. */
const GIT_PATHSPEC_AFTER_DD_SUBS = new Set(['checkout', 'reset'])
/** Global git options a templated command may carry. `-c key=value` and
 *  `--config-env` can point `diff.external`, `core.pager`, `core.sshCommand` and
 *  friends at arbitrary programs, `--exec-path` swaps the git binaries: any of
 *  those, or an unknown global option, leaves the command raw. */
const GIT_GLOBAL_FLAGS_WITH_VALS = ['-C', '--git-dir', '--work-tree']
const GIT_GLOBAL_FLAGS_BARE = new Set(['--no-pager', '-P', '--no-replace-objects', '--literal-pathspecs', '--glob-pathspecs', '--noglob-pathspecs', '--icase-pathspecs'])
const GIT_SUB_FLAGS_WITH_VALS = ['-m', '--message', '-C', '-c', '--author', '--date', '-b', '-B', '-u', '--set-upstream', '-M', '-S', '-G', '--since', '--until', '--grep', '-L', '--format', '--pretty', '-n', '--max-count', '--depth', '-o', '--origin']
const NODE_PMS = new Set(['npm', 'pnpm', 'yarn', 'bun'])
const LINTERS = new Set(['eslint', 'flake8', 'ruff', 'mypy', 'black', 'prettier', 'isort', 'pylint', 'stylelint'])
const TEST_RUNNERS = new Set(['pytest', 'vitest', 'jest', 'mocha'])
const NPX_PASSTHROUGH = new Set(['vitest', 'jest', 'mocha', 'tsc', 'eslint', 'prettier', 'stylelint'])
const CURL_FLAGS_WITH_VALS = ['-X', '--request', '-H', '--header', '-d', '--data', '--data-raw', '--data-binary', '--data-urlencode', '-F', '--form', '-o', '--output', '-u', '--user', '-A', '--user-agent', '-e', '--referer', '-b', '--cookie', '-c', '--cookie-jar', '-m', '--max-time', '--connect-timeout', '-w', '--write-out', '--retry', '--retry-delay', '-T', '--upload-file', '-x', '--proxy', '--url', '-K', '--config', '--cacert', '--cert', '--key', '--resolve', '--limit-rate', '--json', '--form-string', '--data-ascii']
/** Methods that ARE a fetch; any other explicit method is shown on the title. */
const CURL_FETCH_METHODS = new Set(['GET', 'HEAD'])
/** curl flags that make the call more than a fetch of a URL — they upload a
 *  local file, write a local file, or read further options from a file. A title
 *  cannot say what those do, so the command is left raw (`unknown`). */
const CURL_LOCAL_IO_FLAGS = new Set(['-T', '--upload-file', '-o', '--output', '-O', '--remote-name', '--remote-name-all', '-J', '--remote-header-name', '--output-dir', '--create-dirs', '-c', '--cookie-jar', '-D', '--dump-header', '-K', '--config'])
/** Short letters of the same flags, so a cluster like `-sSO` or `-fsSLo` is caught. */
const CURL_LOCAL_IO_LETTERS = /[ToOJcDK]/
/** A single-dash token that is not a pure letter cluster carries an ATTACHED
 *  value (`-Tsecrets.txt`, `-o/tmp/x`, `-d@file`, `-XPOST`). Which letter owns
 *  the value cannot be told apart from the letters before it without curl's own
 *  option table, so any such token naming a local-I/O, body or method letter
 *  leaves the command raw. */
const CURL_ATTACHED_VALUE = /^-[A-Za-z]*[^A-Za-z-]/
const CURL_ATTACHED_GUARD_LETTERS = /[ToOJcDKdFX]/
/** Flags that send a request body; with no explicit `-X` curl sends a POST. */
const CURL_BODY_FLAGS = new Set(['-d', '--data', '--data-raw', '--data-binary', '--data-urlencode', '--data-ascii', '-F', '--form', '--form-string', '--json'])

function unknown(tokens: string[]): ToolAction {
  return { type: 'unknown', cmd: tokens.join(' ') }
}

function readAction(path: string): ToolAction {
  return { type: 'read', path }
}

function listAction(path: string | undefined): ToolAction {
  return path ? { type: 'list_files', path } : { type: 'list_files' }
}

function searchAction(query: string | undefined, path: string | undefined, tokens: string[]): ToolAction {
  if (query === undefined) return unknown(tokens)
  return { type: 'search', query, ...(path ? { path } : {}) }
}

/** `head -n 50 file` / `head -n50 file` / `tail -n +10 file` (Codex head/tail branches). */
function headTailRead(tail: string[], allowPlus: boolean): string | undefined {
  const valid = (n: string) => (allowPlus ? isPlusDigits(n) : isDigits(n))
  let hasValidN = false
  if (tail[0] === '-n') hasValidN = tail.length > 1 && valid(tail[1])
  else if (tail[0]?.startsWith('-n')) hasValidN = valid(tail[0].slice(2))
  if (hasValidN) {
    const candidates: string[] = []
    let i = 0
    while (i < tail.length) {
      if (i === 0 && tail[i] === '-n' && i + 1 < tail.length && valid(tail[i + 1])) { i += 2; continue }
      candidates.push(tail[i])
      i++
    }
    const p = candidates.find(c => !c.startsWith('-'))
    if (p) return p
  }
  if (tail.length === 1 && !tail[0].startsWith('-')) return tail[0]
  return undefined
}

export function hostOf(url: string): string | undefined {
  const m = url.match(/^(?:[a-z][a-z0-9+.-]*:\/\/)?(?:[^@/\s]+@)?([A-Za-z0-9.-]+)(?::\d+)?(?:[/?#]|$)/)
  if (!m) return undefined
  const host = m[1]
  return host.includes('.') || host === 'localhost' ? host : undefined
}

function classifyGit(tokens: string[]): ToolAction {
  // Skip global options (`git -C dir status`) by hand so a later `--` survives.
  let idx = 1
  while (idx < tokens.length && tokens[idx].startsWith('-')) {
    const t = tokens[idx]
    if (GIT_GLOBAL_FLAGS_WITH_VALS.includes(t)) { idx += 2; continue }
    if (GIT_GLOBAL_FLAGS_BARE.has(t) || /^--(git-dir|work-tree)=/.test(t)) { idx += 1; continue }
    return unknown(tokens)
  }
  if (idx >= tokens.length) return unknown(tokens)
  const sub = tokens[idx]
  const subTail = tokens.slice(idx + 1)
  if (sub === 'grep') {
    const a = parseGrepLike(subTail)
    return a.type === 'unknown' ? unknown(tokens) : a
  }
  if (sub === 'ls-files') {
    const p = firstNonFlagOperand(subTail, ['--exclude', '--exclude-from', '--pathspec-from-file'])
    return listAction(p ? shortDisplayPath(p) : undefined)
  }
  if (!GIT_SUBCOMMANDS.has(sub)) return unknown(tokens)
  for (const t of subTail) {
    if (t === '--') break
    const flag = t.startsWith('--') && t.includes('=') ? t.slice(0, t.indexOf('=')) : t
    if (GIT_DESTRUCTIVE_FLAGS.has(flag) || flag.startsWith('--force-with-lease')) return unknown(tokens)
    if (flag === '-d' && GIT_REF_DELETING_SUBS.has(sub)) return unknown(tokens)
    if (/^-[a-zA-Z]*[fD][a-zA-Z]*$/.test(t)) return unknown(tokens) // `-fd`, `-Df`
  }
  if (sub === 'stash' && (subTail[0] === 'drop' || subTail[0] === 'clear')) return unknown(tokens)
  const dd = subTail.indexOf('--')
  let candidates: string[] = []
  if (GIT_PATHSPEC_SUBS.has(sub)) {
    candidates = dd >= 0 ? subTail.slice(dd + 1) : positionalOperands(subTail, GIT_SUB_FLAGS_WITH_VALS)
  } else if (GIT_PATHSPEC_AFTER_DD_SUBS.has(sub) && dd >= 0) {
    candidates = subTail.slice(dd + 1)
  }
  // Refs and ranges (`origin/main`, `a..b`, `HEAD~2`) are not paths.
  const path = candidates.find(c => isPathish(c) && !c.includes('..') && !/^(origin|upstream|refs)\//.test(c))
  return { type: 'git', sub, ...(path ? { path: shortDisplayPath(path) } : {}) }
}

function classifyNodePm(tokens: string[]): ToolAction {
  const [head, ...tail] = tokens
  const ops = positionalOperands(tail, [])
  const sub = ops[0]
  if (sub === undefined) return head === 'yarn' ? { type: 'install' } : unknown(tokens)
  if (['install', 'i', 'add', 'ci'].includes(sub)) return { type: 'install' }
  if (sub === 'test' || sub === 't') return { type: 'test' }
  if (sub === 'run' || sub === 'run-script') return ops[1] ? { type: 'script', name: ops[1] } : unknown(tokens)
  if (sub === 'build') return { type: 'build' }
  if (sub === 'lint') return { type: 'lint' }
  return unknown(tokens)
}

function classifyTool(tokens: string[]): ToolAction {
  const [head, ...tail] = tokens
  const ops = positionalOperands(tail, [])
  switch (head) {
    case 'pip':
    case 'pip3':
      return ops[0] === 'install' ? { type: 'install' } : unknown(tokens)
    case 'uv':
      if (ops[0] === 'sync' || ops[0] === 'add' || (ops[0] === 'pip' && ops[1] === 'install')) return { type: 'install' }
      if (ops[0] === 'run' && ops[1]) return TEST_RUNNERS.has(ops[1]) ? { type: 'test' } : { type: 'script', name: ops[1] }
      return unknown(tokens)
    case 'poetry':
      if (ops[0] === 'install') return { type: 'install' }
      if (ops[0] === 'run' && ops[1]) return TEST_RUNNERS.has(ops[1]) ? { type: 'test' } : { type: 'script', name: ops[1] }
      return unknown(tokens)
    case 'cargo':
      if (ops[0] === 'add' || ops[0] === 'fetch') return { type: 'install' }
      if (ops[0] === 'test') return { type: 'test' }
      if (ops[0] === 'build') return { type: 'build' }
      if (ops[0] === 'clippy' || ops[0] === 'fmt') return { type: 'lint' }
      return unknown(tokens)
    case 'go':
      if (ops[0] === 'test') return { type: 'test' }
      if (ops[0] === 'build') return { type: 'build' }
      if (ops[0] === 'vet') return { type: 'lint' }
      if (ops[0] === 'mod' && (ops[1] === 'download' || ops[1] === 'tidy')) return { type: 'install' }
      return unknown(tokens)
    case 'make':
      return ops[0] ? { type: 'script', name: ops[0] } : { type: 'build' }
    case 'tsc':
      return { type: 'build' }
    default:
      return unknown(tokens)
  }
}

/** `gh api` flags that turn the call into a mutation: an explicit method, or a
 *  body (`-f`/`-F` fields switch gh to POST; `--input` sends a file). */
const GH_API_MUTATING_FLAGS = new Set(['-X', '--method', '-f', '--raw-field', '-F', '--field', '--input'])

function classifyGh(tokens: string[]): ToolAction {
  if (tokens[1] === 'api' && tokens.slice(2).some(t => GH_API_MUTATING_FLAGS.has(t.startsWith('--') && t.includes('=') ? t.slice(0, t.indexOf('=')) : t) || /^-[XfF]./.test(t))) {
    // The title would read `GitHub API repos` for a DELETE or a POST with a
    // body; the method and the payload are what an approver needs to see.
    return unknown(tokens)
  }
  const ops = positionalOperands(tokens.slice(1), ['-R', '--repo', '-F', '--field', '-f', '--raw-field', '-H', '--header', '-q', '--jq', '-t', '--template', '-L', '--limit', '-s', '--state', '-l', '--label', '-a', '--assignee', '-A', '--author', '-S', '--search', '-b', '--body', '-B', '--body-file', '--title', '-X', '--method', '--json'])
  if (ops.length === 0) return unknown(tokens)
  if (ops[0] === 'api') {
    if (!ops[1]) return unknown(tokens)
    const seg = ops[1].replace(/^\/+/, '').split('/')[0]
    return seg ? { type: 'github_api', path: seg } : unknown(tokens)
  }
  if (!ops[1]) return unknown(tokens)
  return { type: 'github', noun: ops[0], verb: ops[1], ...(ops[2] ? { target: ops[2] } : {}) }
}

function classifyCurl(tokens: string[]): ToolAction {
  const tail = tokens.slice(1)
  let method: string | undefined
  let sendsBody = false
  for (let i = 0; i < tail.length; i++) {
    const t = tail[i]
    const flag = t.startsWith('--') && t.includes('=') ? t.slice(0, t.indexOf('=')) : t
    // Uploads, local writes and option files: the title would hide the half
    // that touches the filesystem, so the command stays raw.
    if (CURL_LOCAL_IO_FLAGS.has(flag)) return unknown(tokens)
    if (/^-[A-Za-z]{2,}$/.test(t) && CURL_LOCAL_IO_LETTERS.test(t.slice(1))) return unknown(tokens)
    if (CURL_ATTACHED_VALUE.test(t) && CURL_ATTACHED_GUARD_LETTERS.test(t.slice(1).replace(/[^A-Za-z].*$/, ''))) return unknown(tokens)
    if (CURL_BODY_FLAGS.has(flag)) {
      // `-d @file` / `-F name=@file` read a local file into the body.
      const val = t.includes('=') && t.startsWith('--') ? t.slice(t.indexOf('=') + 1) : tail[i + 1] ?? ''
      if (val.startsWith('@') || (flag === '-F' || flag === '--form') && /(^|=)[@<]/.test(val)) return unknown(tokens)
      sendsBody = true
    }
    if (method === undefined) {
      if (flag === '-X' || flag === '--request') method = (t.includes('=') ? t.slice(t.indexOf('=') + 1) : tail[i + 1])?.toUpperCase()
    }
  }
  if (method === undefined && sendsBody) method = 'POST'
  const ops = positionalOperands(tail, CURL_FLAGS_WITH_VALS)
  for (const op of ops) {
    const host = hostOf(op)
    if (host) return { type: 'fetch', host, ...(method && !CURL_FETCH_METHODS.has(method) ? { method } : {}) }
  }
  return unknown(tokens)
}

/** Classify one pipeline segment. `single` is true when the whole script is
 *  this one segment (gates the Print rule: a chained `echo` is noise, a lone
 *  `echo` is the action). */
export function summarizeSegment(segment: string[], single: boolean): ToolAction {
  if (segment.length === 0) return unknown(segment)
  // `npx vitest …` classifies as `vitest …` for the tools we know.
  const tokens = segment[0] === 'npx' && segment[1] && NPX_PASSTHROUGH.has(segment[1]) ? segment.slice(1) : segment
  const [head, ...tail] = tokens
  switch (head) {
    case 'ls':
    case 'eza':
    case 'exa': {
      const flags = head === 'ls'
        ? ['-I', '-w', '--block-size', '--format', '--time-style', '--color', '--quoting-style']
        : ['-I', '--ignore-glob', '--color', '--sort', '--time-style', '--time']
      const p = firstNonFlagOperand(tail, flags)
      return listAction(p ? shortDisplayPath(p) : undefined)
    }
    case 'tree': {
      const p = firstNonFlagOperand(tail, ['-L', '-P', '-I', '--charset', '--filelimit', '--sort'])
      return listAction(p ? shortDisplayPath(p) : undefined)
    }
    case 'du': {
      const p = firstNonFlagOperand(tail, ['-d', '--max-depth', '-B', '--block-size', '--exclude', '--time-style'])
      return listAction(p ? shortDisplayPath(p) : undefined)
    }
    case 'rg':
    case 'rga':
    case 'ripgrep-all': {
      // `--pre <cmd>` runs a command over every file; the search title would hide it.
      if (rgHasPreprocessor(tail)) return unknown(tokens)
      const hasFiles = tail.includes('--files')
      const nonFlags = skipFlagValues(tail, ['-g', '--glob', '--iglob', '-t', '--type', '--type-add', '--type-not', '-m', '--max-count', '-A', '-B', '-C', '--context', '--max-depth', '-e', '--regexp']).filter(a => !a.startsWith('-'))
      if (hasFiles) return listAction(nonFlags[0] ? shortDisplayPath(nonFlags[0]) : undefined)
      // `-e PATTERN` is the pattern; otherwise the first operand is.
      const eIdx = tail.findIndex(a => a === '-e' || a === '--regexp')
      const query = eIdx >= 0 ? tail[eIdx + 1] : nonFlags[0]
      const path = eIdx >= 0 ? nonFlags[0] : nonFlags[1]
      return searchAction(query, path ? shortDisplayPath(path) : undefined, tokens)
    }
    case 'git':
      return classifyGit(tokens)
    case 'fd': {
      // `-x`/`--exec`/`-X`/`--exec-batch` run a command per match; the same rule as `find -exec`.
      if (tail.some(t => t === '-x' || t === '-X' || t === '--exec' || t === '--exec-batch' || t.startsWith('--exec=') || t.startsWith('--exec-batch='))) return unknown(tokens)
      const { query, path } = parseFdQueryAndPath(tail)
      return query !== undefined ? { type: 'search', query, ...(path ? { path } : {}) } : listAction(path)
    }
    case 'find': {
      if (findMutates(tail)) return unknown(tokens)
      const { query, path } = parseFindQueryAndPath(tail)
      return query !== undefined ? { type: 'search', query, ...(path ? { path } : {}) } : listAction(path)
    }
    case 'grep':
    case 'egrep':
    case 'fgrep': {
      const a = parseGrepLike(tail)
      return a.type === 'unknown' ? unknown(tokens) : a
    }
    case 'ag':
    case 'ack':
    case 'pt': {
      const nonFlags = skipFlagValues(tail, ['-G', '-g', '--file-search-regex', '--ignore-dir', '--ignore-file', '--path-to-ignore']).filter(a => !a.startsWith('-'))
      return searchAction(nonFlags[0], nonFlags[1] ? shortDisplayPath(nonFlags[1]) : undefined, tokens)
    }
    case 'cat': {
      const p = singleNonFlagOperand(tail, [])
      return p ? readAction(p) : unknown(tokens)
    }
    case 'bat':
    case 'batcat': {
      const p = singleNonFlagOperand(tail, ['--theme', '--language', '--style', '--terminal-width', '--tabs', '--line-range', '--map-syntax'])
      return p ? readAction(p) : unknown(tokens)
    }
    case 'less': {
      const p = singleNonFlagOperand(tail, ['-p', '-P', '-x', '-y', '-z', '-j', '--pattern', '--prompt', '--tabs', '--shift', '--jump-target'])
      return p ? readAction(p) : unknown(tokens)
    }
    case 'more': {
      const p = singleNonFlagOperand(tail, [])
      return p ? readAction(p) : unknown(tokens)
    }
    case 'head': {
      const p = headTailRead(tail, false)
      return p ? readAction(p) : unknown(tokens)
    }
    case 'tail': {
      const p = headTailRead(tail, true)
      return p ? readAction(p) : unknown(tokens)
    }
    case 'awk': {
      const p = awkDataFileOperand(tail)
      return p ? readAction(p) : unknown(tokens)
    }
    case 'nl': {
      const p = skipFlagValues(tail, ['-s', '-w', '-v', '-i', '-b']).find(a => !a.startsWith('-'))
      return p ? readAction(p) : unknown(tokens)
    }
    case 'sed': {
      const p = sedReadPath(tail)
      return p ? readAction(p) : unknown(tokens)
    }
    case 'npm':
    case 'pnpm':
    case 'yarn':
    case 'bun':
      return classifyNodePm(tokens)
    case 'pytest':
    case 'vitest':
    case 'jest':
    case 'mocha':
      return { type: 'test' }
    case 'gh':
      return classifyGh(tokens)
    case 'curl':
      return classifyCurl(tokens)
    case 'echo':
    case 'printf':
      return single ? { type: 'print' } : unknown(tokens)
    default:
      if (isPythonCommand(head)) {
        if (tail[0] === '-m' && tail[1] === 'pytest') return { type: 'test' }
        // `python -c <script>` is arbitrary code; a marker match on the script
        // text (`os.listdir`) says nothing about what else it does. Raw.
        return unknown(tokens)
      }
      if (LINTERS.has(head)) return { type: 'lint' }
      if (NODE_PMS.has(head)) return classifyNodePm(tokens)
      return classifyTool(tokens)
  }
}

// ---------------------------------------------------------------------------
// Shell — whole-script classification (Codex `parse_shell_script` + `simplify_once`)
// ---------------------------------------------------------------------------

function stripWrapper(segments: string[][]): string[][] | null {
  if (segments.length === 1) {
    const seg = segments[0]
    if (seg.length === 3 && (seg[0] === 'bash' || seg[0] === 'zsh' || seg[0] === 'sh') && (seg[1] === '-c' || seg[1] === '-lc')) {
      const inner = tokenizeShell(seg[2])
      return inner ? stripWrapper(splitSegments(inner)) : null
    }
  }
  if (segments.length >= 2 && segments[0].length === 1 && ['yes', 'y', 'no', 'n'].includes(segments[0][0])) {
    return segments.slice(1)
  }
  return segments
}

/** True when the segment opens with `VAR=value` assignments. */
function hasEnvPrefix(seg: string[]): boolean {
  return seg.length > 0 && ENV_ASSIGN_RE.test(seg[0])
}

function sameAction(a: ToolAction, b: ToolAction): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

/** Codex `simplify_once`: drop a leading `echo`, a `cd` that is followed by
 *  something, `|| true`, and a bare `nl -flags`. */
function simplifyOnce(actions: ToolAction[]): ToolAction[] | null {
  if (actions.length <= 1) return null
  const first = actions[0]
  if (first.type === 'unknown' && /^echo(\s|$)/.test(first.cmd)) return actions.slice(1)
  const cdIdx = actions.findIndex(a => a.type === 'unknown' && /^cd(\s|$)/.test(a.cmd))
  if (cdIdx >= 0 && actions.length > cdIdx + 1) return [...actions.slice(0, cdIdx), ...actions.slice(cdIdx + 1)]
  const trueIdx = actions.findIndex(a => a.type === 'unknown' && a.cmd === 'true')
  if (trueIdx >= 0) return [...actions.slice(0, trueIdx), ...actions.slice(trueIdx + 1)]
  const nlIdx = actions.findIndex(a => a.type === 'unknown' && /^nl(\s+-\S+)*$/.test(a.cmd))
  if (nlIdx >= 0) return [...actions.slice(0, nlIdx), ...actions.slice(nlIdx + 1)]
  return null
}

/**
 * Classify a shell script into its main action plus a count of further parsed
 * actions, or null when the script is not fully parseable (the caller then
 * shows the raw command).
 */
export function classifyShellCommand(script: string): { action: ToolAction; more: number } | null {
  const tokens = tokenizeShell(script)
  if (!tokens) return null
  let segments = stripWrapper(splitSegments(tokens))
  if (!segments || segments.length === 0) return null
  // An environment prefix can change what the command does (`LD_PRELOAD=…`,
  // `PATH=…`, `GIT_DIR=…`); a title that drops it would say `List files` for
  // a command that also loads a library. Any prefix leaves the command raw.
  if (segments.some(hasEnvPrefix)) return null
  const multi = segments.length > 1
  if (multi) segments = segments.filter(s => !isSmallFormattingCommand(s))
  if (segments.length === 0) return null

  let actions: ToolAction[] = []
  let cwd: string | undefined
  for (const seg of segments) {
    if (seg[0] === 'cd') {
      const dir = cdTarget(seg.slice(1))
      if (dir) cwd = cwd ? joinPaths(cwd, dir) : dir
      continue
    }
    let action = summarizeSegment(seg, !multi)
    if (action.type === 'read' && cwd) action = { ...action, path: joinPaths(cwd, action.path) }
    actions.push(action)
  }
  if (actions.length > 1) {
    actions = actions.filter(a => !(a.type === 'unknown' && a.cmd === 'true'))
    for (let next = simplifyOnce(actions); next; next = simplifyOnce(actions)) actions = next
  }
  // Collapse consecutive duplicates (Codex `parse_command`).
  const deduped: ToolAction[] = []
  for (const a of actions) if (!deduped.length || !sameAction(deduped[deduped.length - 1], a)) deduped.push(a)
  if (deduped.length === 0 || deduped.some(a => a.type === 'unknown')) return null
  // Shell reads show the short display name, like Codex's `Read file` title.
  const main = deduped[0].type === 'read' ? { ...deduped[0], path: shortDisplayPath(deduped[0].path) } : deduped[0]
  return { action: main, more: deduped.length - 1 }
}
