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
