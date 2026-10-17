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
  const reasonId = firstMatch(PER_LINE_PATTERNS, code) ?? firstMatch(FULL_BLOCK_PATTERNS, code)
  // `REASON_KEY[reasonId]` indexed INLINE at the call, not resolved inside
  // `firstMatch`: that is the shape `scripts/check-i18n-keys.mjs` follows, so
  // every reason key is verified to exist in the catalog.
  return reasonId ? { reason: i18nT(REASON_KEY[reasonId]) } : null
}
