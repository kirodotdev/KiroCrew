import { i18nT } from '../i18n/t'

const CMD_PREFIX = '(command\\s+|builtin\\s+)?'
const FILE_READERS = `${CMD_PREFIX}(cat|less|head|tail|tac|more)`

/**
 * Catalog KEYS for the human-readable reason shown in the approval dialog
 * (`RunInTerminalConfirm`, next to the "flagged" label).
 *
 * Keys, not strings, and deliberately SEPARATE from the pattern tables below:
 *
 *  - The tables are evaluated at module load, so an `i18nT()` call inside one
 *    would freeze the boot language and never re-resolve on a language switch.
 *    Resolution happens in `checkSensitiveCommand()`, which runs per click.
 *  - The pattern tables now carry only an opaque `reasonId`. Nothing a
 *    translator can reach is adjacent to a regex, so no locale edit can weaken
 *    a detection pattern — translating the reason text and changing what gets
 *    detected are physically different files.
 *
 * Flat, with full literal key strings, indexed inline at the `i18nT()` call:
 * that is the shape `scripts/check-i18n-keys.mjs` resolves statically, so every
 * reason below is verified to exist in the catalog.
 */
const REASON_KEY = {
  raw_tcp: 'utils.sensitiveCommand.opens_raw_tcp_connection',
  network_socket: 'utils.sensitiveCommand.sends_data_to_network_socket',
  credential_files: 'utils.sensitiveCommand.reads_credential_files',
  system_credentials: 'utils.sensitiveCommand.reads_system_credentials',
  env_dump: 'utils.sensitiveCommand.dumps_sensitive_environment_variables',
  env_single: 'utils.sensitiveCommand.reads_sensitive_environment_variable',
  encodes_credentials: 'utils.sensitiveCommand.encodes_credential_files',
  output_to_url: 'utils.sensitiveCommand.sends_command_output_to_external_url',
  pipes_to_url: 'utils.sensitiveCommand.pipes_data_to_external_url',
  uploads_to_url: 'utils.sensitiveCommand.uploads_file_to_external_url',
} as const

/** Which reason a pattern group reports. A union, so a typo fails the type check. */
type ReasonId = keyof typeof REASON_KEY

/**
 * Patterns GROUPED BY the reason they report, rather than a flat list of
 * `{ pattern, reason }` pairs.
 *
 * The grouping is what keeps this file's translatable surface at zero: a reason
 * is a property NAME here, so nothing in these tables is a string literal a
 * translator could reach or a locale file could alter. Detection and wording are
 * now fully separated.
 *
 * Order is still significant and still preserved — `checkSensitiveCommand`
 * reports the FIRST match, string-keyed objects iterate in insertion order, and
 * every reason's patterns were already contiguous in the original flat list, so
 * the pattern-by-pattern trial order is unchanged.
 */
const PER_LINE_PATTERNS: Partial<Record<ReasonId, RegExp[]>> = {
  raw_tcp: [/>\s*\/dev\/tcp\//],
  network_socket: [/export\s+.*>(\/dev\/tcp|nc\s|netcat)/],
  credential_files: [
    new RegExp(`${FILE_READERS}\\s+.*\\.(aws|ssh|gnupg|config\\/gcloud)\\b`),
    new RegExp(`${FILE_READERS}\\s+.*\\/(credentials|id_rsa|id_ed25519|private\\.key)\\b`),
    new RegExp(`${FILE_READERS}\\s+(\\S*\\/)?\\.\\.?env\\b`),
  ],
  system_credentials: [new RegExp(`${FILE_READERS}\\s+\\/etc\\/(shadow|passwd)`)],
  env_dump: [/env\s*\|\s*grep\s+.*(secret|key|token|pass|cred|aws)/i],
  env_single: [/printenv\s+(AWS_SECRET|AWS_SESSION|GITHUB_TOKEN|NPM_TOKEN)/i],
  encodes_credentials: [/base64.*\.(aws|ssh|pem|key)\b/],
}

const FULL_BLOCK_PATTERNS: Partial<Record<ReasonId, RegExp[]>> = {
  output_to_url: [
    /(curl|wget)\s[\s\S]*?\$\(/,
    /(curl|wget)\s[\s\S]*?`[^`]*`/,
  ],
  pipes_to_url: [/(curl|wget)\s[\s\S]*?-d\s+@-/],
  uploads_to_url: [/(curl|wget)\s[\s\S]*?-d\s+@[^\s]/],
}

/**
 * PowerShell and cmd forms of the same risks, checked on words rather than
 * patterns. A block is split into lines (a line ending in PowerShell's backtick
 * continues on the next), each line into words and the separators `;`, `|`,
 * `&`, `(` and `{`, and each command is its first word, lower-cased: both
 * shells match command names case-insensitively, and a word only counts in
 * command position, so `Add-Type` and `-Type` are not readers.
 */
const WIN_READERS = new Set(['get-content', 'gc', 'type'])
const WIN_ENV_LISTERS = new Set(['get-childitem', 'gci', 'dir', 'ls'])
/** `curl` and `wget` are Invoke-WebRequest aliases in Windows PowerShell 5.1. */
const WIN_WEB_SENDERS = new Set(['invoke-webrequest', 'iwr', 'invoke-restmethod', 'irm', 'curl', 'wget'])
const WIN_SEPARATORS = new Set([';', '|', '&', '(', '{'])
const CREDENTIAL_DIRS = new Set(['.aws', '.ssh', '.gnupg', 'gcloud'])
const CREDENTIAL_FILES = ['credentials', 'id_rsa', 'id_ed25519', 'private.key']
const SECRET_WORDS = ['secret', 'key', 'token', 'pass', 'cred', 'aws']
/** A regex, not string literals: the i18n literal gate reads `'$env:…'` as copy. */
const SECRET_ENV_READ = /\$env:(aws_secret|aws_session|github_token|npm_token)/
const ARG_WRAPPERS = new Set(['"', "'", ')', '}'])

/** Lines of `code`, with a backtick-continued line joined to the next. */
function logicalLines(code: string): string[] {
  const out: string[] = []
  let carry = ''
  for (const raw of code.split('\n')) {
    const line = raw.endsWith('\r') ? raw.slice(0, -1) : raw
    const trimmed = line.trimEnd()
    if (trimmed.endsWith('`')) { carry += trimmed.slice(0, -1) + ' '; continue }
    out.push(carry + line)
    carry = ''
  }
  if (carry) out.push(carry)
  return out
}

/** Lower-cased words of one line, each separator a word of its own. */
function lineWords(line: string): string[] {
  const words: string[] = []
  let word = ''
  for (const ch of line.toLowerCase()) {
    if (ch === ' ' || ch === '\t') { if (word) words.push(word); word = ''; continue }
    if (WIN_SEPARATORS.has(ch)) { if (word) words.push(word); words.push(ch); word = ''; continue }
    word += ch
  }
  if (word) words.push(word)
  return words
}

/** A word without the quotes and closing brackets around it. */
function bare(word: string): string {
  let start = 0
  let end = word.length
  while (start < end && ARG_WRAPPERS.has(word[start])) start++
  while (end > start && ARG_WRAPPERS.has(word[end - 1])) end--
  return word.slice(start, end)
}

/** Whether a path argument names a credential store, a key file or a `.env`. */
function namesCredential(arg: string): boolean {
  const parts = bare(arg).split('\\').join('/').split('/').filter(Boolean)
  if (parts.length === 0) return false
  if (parts.some(p => CREDENTIAL_DIRS.has(p))) return true
  const last = parts[parts.length - 1]
  if (last === '.env' || last.startsWith('.env.')) return true
  return parts.length > 1 && CREDENTIAL_FILES.some(f => last === f || last.startsWith(f + '.'))
}

/** The words of a command with a leading `$var =` (or `$var=`) assignment
 *  removed, so `$r = Invoke-RestMethod ...` is judged as Invoke-RestMethod. */
function commandAfterAssignment(words: string[]): string[] {
  if (words.length >= 2 && words[0].startsWith('$') && words[1] === '=') return words.slice(2)
  const first = words[0] ?? ''
  const eq = first.indexOf('=')
  if (first.startsWith('$') && eq !== -1) {
    const called = first.slice(eq + 1)
    return called ? [called, ...words.slice(1)] : words.slice(1)
  }
  return words
}

/** Arguments without each redirection (`>`, `>>`, `2>`) and its target: a
 *  redirect target is written, not read, so `type nul > .env` reads nothing,
 *  while `type >dump.txt .env` still reads `.env`. */
function withoutRedirects(args: string[]): string[] {
  const out: string[] = []
  for (let i = 0; i < args.length; i++) {
    const op = /^[12]?>>?/.exec(args[i])
    if (!op) { out.push(args[i]); continue }
    // `> file`: the target is the next word; `>file`: it is joined.
    if (op[0] === args[i]) i++
  }
  return out
}

/** The value an argument names: itself, the part after `:` of a colon-joined
 *  parameter (`-Path:~\.aws\credentials`), or nothing for a bare parameter. */
function argumentValue(arg: string): string {
  if (!arg.startsWith('-')) return arg
  const colon = arg.indexOf(':')
  return colon === -1 ? '' : arg.slice(colon + 1)
}

/** The PowerShell and cmd reason `code` trips: a credential read first, then a
 *  secret environment dump, then a single secret variable, then a file upload. */
function windowsShellReason(code: string): ReasonId | null {
  let envDump = false
  let envSingle = false
  let upload = false
  for (const line of logicalLines(code)) {
    const words = lineWords(line)
    if (words.some(w => SECRET_ENV_READ.test(w))) envSingle = true
    let start = 0
    for (let i = 0; i <= words.length; i++) {
      if (i < words.length && !WIN_SEPARATORS.has(words[i])) continue
      const command = words.slice(start, i)
      start = i + 1
      const called = commandAfterAssignment(command)
      if (called.length === 0) continue
      const [name, ...rest] = called
      const args = withoutRedirects(rest)
      if (WIN_READERS.has(name) && args.some(a => namesCredential(argumentValue(a)))) return 'credential_files'
      if (WIN_ENV_LISTERS.has(name) && args.some(a => bare(a).startsWith('env:'))) {
        const pipe = words.indexOf('|', i)
        if (pipe !== -1 && words.slice(pipe + 1).some(w => SECRET_WORDS.some(s => w.includes(s)))) envDump = true
      }
      if (WIN_WEB_SENDERS.has(name)) {
        const at = args.indexOf('-infile')
        if (at !== -1 && at + 1 < args.length && !WIN_SEPARATORS.has(args[at + 1])) upload = true
        if (args.some(a => a.startsWith('-infile:') && a.length > '-infile:'.length)) upload = true
      }
    }
  }
  if (envDump) return 'env_dump'
  if (envSingle) return 'env_single'
  if (upload) return 'uploads_to_url'
  return null
}

export interface SensitiveMatch {
  /** Localised, ready to render. Non-empty for every match. */
  reason: string
}

/** First pattern in `groups` that `code` trips, as its reason id. */
function firstMatch(groups: Partial<Record<ReasonId, RegExp[]>>, code: string): ReasonId | null {
  for (const reasonId of Object.keys(groups) as ReasonId[]) {
    for (const pattern of groups[reasonId]!) {
      if (pattern.test(code)) return reasonId
    }
  }
  return null
}

/**
 * First sensitive pattern the command trips, or null.
 *
 * Called from the "Run in terminal" click handler, never at module scope, so the
 * reason resolves in the active language. A language switch remounts the whole
 * tree (`<App>` is keyed on it — see `i18n/t.ts`), which tears down any open
 * dialog, so a resolved reason can never be left stale on screen.
 *
 * Detection is independent of i18n: if a catalog key were ever missing, `i18nT`
 * returns the key itself — still a non-empty string, so the caller's `!!reason`
 * warning state, the amber styling and the focus-the-cancel-button behaviour all
 * still fire. A missing translation degrades the wording, never the check.
 */
export function checkSensitiveCommand(code: string): SensitiveMatch | null {
  const reasonId = firstMatch(PER_LINE_PATTERNS, code) ?? windowsShellReason(code) ?? firstMatch(FULL_BLOCK_PATTERNS, code)
  // `REASON_KEY[reasonId]` indexed INLINE at the call, not resolved inside
  // `firstMatch`: that is the shape `scripts/check-i18n-keys.mjs` follows, so
  // every reason key is verified to exist in the catalog.
  return reasonId ? { reason: i18nT(REASON_KEY[reasonId]) } : null
}
