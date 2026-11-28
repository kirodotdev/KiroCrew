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
 * patterns. The block is read once, left to right, the way the shells read it
 * (`winStatements`): quoted text is part of one word, with its quotes removed,
 * and a separator, a redirection or a line end counts only outside quotes. Each
 * command is its first word, lower-cased: both shells match command names
 * case-insensitively, and a word only counts in command position, so `Add-Type`
 * and `-Type` are not readers.
 */
const WIN_READERS = new Set(['get-content', 'gc', 'type'])
const WIN_ENV_LISTERS = new Set(['get-childitem', 'gci', 'dir', 'ls'])
/** `curl` and `wget` are Invoke-WebRequest aliases in Windows PowerShell 5.1. */
const WIN_WEB_SENDERS = new Set(['invoke-webrequest', 'iwr', 'invoke-restmethod', 'irm', 'curl', 'wget'])
/** Unquoted characters that end one command inside a statement. */
const WIN_COMMAND_ENDS = new Set(['|', '&', '(', ')', '{', '}'])
/** Streams a redirection can name before its `>`, as in `2>` or `*>`. */
const WIN_REDIRECT_STREAMS = new Set(['1', '2', '3', '4', '5', '6', '*'])
const DIGITS = '0123456789'
const CREDENTIAL_DIRS = new Set(['.aws', '.ssh', '.gnupg', 'gcloud'])
const CREDENTIAL_FILES = ['credentials', 'id_rsa', 'id_ed25519', 'private.key']
const SECRET_WORDS = ['secret', 'key', 'token', 'pass', 'cred', 'aws']
/** A regex, not string literals: the i18n literal gate reads `'$env:…'` as copy. */
const SECRET_ENV_READ = /\$env:(aws_secret|aws_session|github_token|npm_token)/

interface WinToken {
  kind: 'word' | 'separator' | 'redirect'
  /** A word lower-cased and unquoted, a separator's character, or a redirection operator. */
  text: string
  /** A redirection that writes to the word after it (`> file`), unlike `2>&1`. */
  takesTarget?: boolean
}

/** Index of the `)` closing the `$(` whose `(` is at `open`, or the end of `s`.
 *  Quoted text inside is skipped, so a `)` in a string does not close it. */
function subexpressionEnd(s: string, open: number): number {
  let depth = 0
  for (let i = open; i < s.length; i++) {
    const ch = s[i]
    if (ch === "'" || ch === '"') {
      const close = s.indexOf(ch, i + 1)
      if (close === -1) return s.length
      i = close
      continue
    }
    if (ch === '(') depth++
    if (ch === ')' && --depth === 0) return i
  }
  return s.length
}

/**
 * Statements of a PowerShell or cmd block, each as its tokens.
 *
 * A statement ends at an unquoted `;` or line end, except a line end escaped
 * by a backtick or following a `|`, where PowerShell reads on. Single quotes
 * are literal, with `''` for a quote; inside double quotes a backtick escapes
 * the next character and `""` is a quote. A here-string (`@"` or `@'` ending a
 * line) runs to the line that starts with `"@` or `'@`. A `$(` inside double
 * quotes or a `@"` here-string runs a command, so its text is read as
 * statements of its own.
 */
function winStatements(code: string): WinToken[][] {
  const s = code.toLowerCase()
  const statements: WinToken[][] = []
  let tokens: WinToken[] = []
  let word = ''
  let inWord = false
  const endWord = () => {
    if (inWord) tokens.push({ kind: 'word', text: word })
    word = ''
    inWord = false
  }
  const endStatement = () => {
    endWord()
    if (tokens.length) statements.push(tokens)
    tokens = []
  }
  /** Adds the text of a double-quoted string or here-string to the word, and
   *  reads each `$( … )` in it as statements. */
  const expandable = (text: string) => {
    for (let k = 0; k < text.length; k++) {
      if (text[k] === '$' && text[k + 1] === '(') {
        const close = subexpressionEnd(text, k + 1)
        statements.push(...winStatements(text.slice(k + 2, close)))
        k = close
        continue
      }
      word += text[k]
    }
  }

  for (let i = 0; i < s.length; i++) {
    const ch = s[i]
    if (ch === '@' && (s[i + 1] === '"' || s[i + 1] === "'") && (s[i + 2] === '\n' || (s[i + 2] === '\r' && s[i + 3] === '\n'))) {
      const quote = s[i + 1]
      const bodyStart = s.indexOf('\n', i) + 1
      let close = s.indexOf('\n' + quote + '@', bodyStart - 1)
      if (close === -1) close = s.length
      const body = s.slice(bodyStart, close)
      inWord = true
      if (quote === '"') expandable(body)
      else word += body
      i = close + 2
      continue
    }
    if (ch === "'") {
      inWord = true
      for (i++; i < s.length; i++) {
        if (s[i] === "'") {
          if (s[i + 1] !== "'") break
          i++
        }
        word += s[i]
      }
      continue
    }
    if (ch === '"') {
      inWord = true
      let text = ''
      for (i++; i < s.length; i++) {
        if (s[i] === '`' && i + 1 < s.length) { text += s[++i]; continue }
        if (s[i] === '"') {
          if (s[i + 1] !== '"') break
          i++
        }
        if (s[i] === '$' && s[i + 1] === '(') {
          const close = subexpressionEnd(s, i + 1)
          text += s.slice(i, close + 1)
          i = close
          continue
        }
        text += s[i]
      }
      expandable(text)
      continue
    }
    if (ch === '`') {
      const next = s[i + 1]
      if (next === '\n') { endWord(); i++; continue }
      if (next === '\r' && s[i + 2] === '\n') { endWord(); i += 2; continue }
      if (next !== undefined) { inWord = true; word += next; i++ }
      continue
    }
    if (ch === '\n') {
      endWord()
      const last = tokens[tokens.length - 1]
      if (last?.kind === 'separator' && last.text === '|') continue
      endStatement()
      continue
    }
    if (ch === ' ' || ch === '\t' || ch === '\r') { endWord(); continue }
    if (ch === ';') { endStatement(); continue }
    if (WIN_COMMAND_ENDS.has(ch)) {
      endWord()
      tokens.push({ kind: 'separator', text: ch })
      continue
    }
    if (ch === '>') {
      let op = '>'
      if (inWord && WIN_REDIRECT_STREAMS.has(word)) {
        op = word + op
        word = ''
        inWord = false
      } else {
        endWord()
      }
      if (s[i + 1] === '>') { op += '>'; i++ }
      if (s[i + 1] === '&' && i + 2 < s.length && DIGITS.includes(s[i + 2])) {
        tokens.push({ kind: 'redirect', text: op + s.slice(i + 1, i + 3), takesTarget: false })
        i += 2
        continue
      }
      tokens.push({ kind: 'redirect', text: op, takesTarget: true })
      continue
    }
    inWord = true
    word += ch
  }
  endStatement()
  return statements
}

/** Whether a path argument names a credential store, a key file or a `.env`. */
function namesCredential(arg: string): boolean {
  const parts = arg.split('\\').join('/').split('/').filter(Boolean)
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

/** The words of one command, without each redirection and the word it writes
 *  to: a redirect target is written, not read, so `type nul > .env` reads
 *  nothing, while `type >dump.txt .env` and `type .env>dump.txt` read `.env`. */
function commandWords(tokens: WinToken[]): string[] {
  const words: string[] = []
  for (let k = 0; k < tokens.length; k++) {
    const token = tokens[k]
    if (token.kind === 'redirect') {
      if (token.takesTarget && tokens[k + 1]?.kind === 'word') k++
      continue
    }
    words.push(token.text)
  }
  return words
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
  for (const statement of winStatements(code)) {
    if (statement.some(t => t.kind === 'word' && SECRET_ENV_READ.test(t.text))) envSingle = true
    let start = 0
    for (let i = 0; i <= statement.length; i++) {
      if (i < statement.length && statement[i].kind !== 'separator') continue
      const called = commandAfterAssignment(commandWords(statement.slice(start, i)))
      start = i + 1
      if (called.length === 0) continue
      const [name, ...args] = called
      if (WIN_READERS.has(name) && args.some(a => namesCredential(argumentValue(a)))) return 'credential_files'
      if (WIN_ENV_LISTERS.has(name) && args.some(a => argumentValue(a).startsWith('env:'))) {
        const pipe = statement.findIndex((t, k) => k >= i && t.kind === 'separator' && t.text === '|')
        if (pipe !== -1 && statement.slice(pipe + 1).some(t => t.kind === 'word' && SECRET_WORDS.some(w => t.text.includes(w)))) envDump = true
      }
      if (WIN_WEB_SENDERS.has(name)) {
        const at = args.indexOf('-infile')
        if (at !== -1 && at + 1 < args.length) upload = true
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
