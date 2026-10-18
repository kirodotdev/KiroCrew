/**
 * Per-shell environment-variable export command builders for <SettingRef kind="env">.
 *
 * NAMED i18n BOUNDARY — this module is excluded by path in
 * `eslint.i18n.config.js`, the same idiom as `*.prompt.ts`: every string in it
 * is CLI syntax handed to a terminal (`export`, `$env:`, `set`, `=1`), never
 * user-visible prose, and translating any fragment would break the command.
 * The module may contain ONLY command builders; the labels users read come from
 * the translated `privacyDisclosure.shell*Label` catalog keys referenced below,
 * and the consumer (SettingRef.tsx) stays fully covered by the gate.
 */

/**
 * Shared validation regex for environment variable key names.
 * Imported by SettingRef.tsx to validate env-mode keys before rendering commands.
 */
export const VALID_ENV_KEY = /^[A-Za-z_][A-Za-z0-9_]{0,99}$/
export type EnvIntent = 'set' | 'unset'

export interface EnvShellEntry {
  labelKey: string
  command: (key: string, valuePlaceholder?: string) => string
  unsetCommand: (key: string) => string
}

export const ENV_SHELLS: readonly EnvShellEntry[] = [
  {
    labelKey: 'privacyDisclosure.shellMacOSLinuxLabel',
    command: (key: string, valuePlaceholder = 'value') => `export ${key}=<${valuePlaceholder}>`,
    unsetCommand: (key: string) => `unset ${key}`,
  },
  {
    labelKey: 'privacyDisclosure.shellPowerShellLabel',
    command: (key: string, valuePlaceholder = 'value') => `$env:${key} = '<${valuePlaceholder}>'`,
    unsetCommand: (key: string) => `Remove-Item Env:${key}`,
  },
  {
    labelKey: 'privacyDisclosure.shellWindowsCmdLabel',
    command: (key: string, valuePlaceholder = 'value') => `set ${key}=<${valuePlaceholder}>`,
    unsetCommand: (key: string) => `set ${key}=`,
  },
] as const

/**
 * Build the `kirocrew config set` CLI command for a file-mode setting.
 * Lives here (not in SettingRef.tsx) because this module is the named i18n
 * boundary for CLI syntax: commands and their placeholders are terminal
 * input, never user-visible prose, and are excluded from string extraction
 * in eslint.i18n.config.js with rationale.
 */
export function configSetCommand(key: string, placeholder: string): string {
  return `kirocrew config set ${key} ${placeholder}`
}

/**
 * The one rule that turns a multi-line string-list editor into list items: split on
 * LF or CRLF, trim each line, drop the empty ones, keep order and duplicates. The
 * Skills editor writes this list and the command row encodes the same list, so the
 * command a user copies sets exactly what the editor would save. Nothing here
 * expands `~`, resolves, deduplicates, sorts or checks a path: the consumer of the
 * list owns what a path means.
 */
export function stringListLines(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
}

/** Standard Base64 of the UTF-8 bytes of `text`, converted in chunks so a long value never builds a huge argument list. */
function utf8Base64(text: string): string {
  const bytes = new TextEncoder().encode(text)
  const chunk = 0x2000
  let binary = ''
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk))
  }
  return btoa(binary)
}

/**
 * Build `kirocrew config set` for a gateway that supplied launcher prefixes.
 *
 * `prefix` is the server-resolved launcher for one shell and `key` a schema key, so
 * the only user text is the value, and that exists solely inside the Base64 token.
 * The token is attached to the flag with `=` as ONE argument: Windows PowerShell 5.1
 * drops a separate empty argument before a `.cmd` shim, and an attached empty token
 * (`--value-base64=`) survives every shell. A schema type of `array` sends the lines
 * of `text` as a JSON list (see {@link stringListLines}); any other type sends `text`
 * verbatim and the CLI parses it exactly as it parses a positional value.
 */
export function configSetCommandV1(
  prefix: string,
  key: string,
  schemaType: string | undefined,
  text: string,
): string {
  return `${prefix} config set ${key} --value-base64=${utf8Base64(configSetDecodedValue(schemaType, text))}`
}

/** The exact text a command built from `text` sets, shown beside it so the user can audit the encoding. */
export function configSetDecodedValue(schemaType: string | undefined, text: string): string {
  return schemaType === 'array' ? JSON.stringify(stringListLines(text)) : text
}

/** The value a command editor starts from when the caller has no meaningful sample: blank for a list, otherwise a literal of the schema type. */
export function configSetSampleValue(schemaType: string | undefined): string {
  if (schemaType === 'array') return ''
  if (schemaType === 'integer' || schemaType === 'number') return '7'
  if (schemaType === 'boolean') return 'true'
  return 'value'
}

/**
 * Value placeholder for a file-mode CLI command: explicit prop override wins,
 * then schema type (`true|false` for booleans), then generic `<value>`.
 */
export function cliValuePlaceholder(schemaType: string | undefined, valuePlaceholderProp?: string): string {
  if (valuePlaceholderProp && valuePlaceholderProp !== 'value') {
    return valuePlaceholderProp
  }
  if (schemaType === 'boolean') return 'true'
  return '<value>'
}
