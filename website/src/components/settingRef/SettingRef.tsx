/**
 * <SettingRef> — clickable setting reference chip.
 *
 * Renders differently depending on where the setting lives:
 * - UI-editable: accent link chip navigating to /settings/<tab>?highlight=key:<configKey>
 * - File-only: <code> with popover showing CLI command
 * - Env var (kind='env'): <code> with popover showing per-shell export lines
 * - Unknown/malicious key: plain <code>, NO link
 */
import { Link } from 'react-router-dom'
import { Settings, Terminal } from 'lucide-react'
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover'
import { resolveSettingRef } from './resolveSettingRef'
import type { SchemaEntry } from './resolveSettingRef'
import { useConfigSchema } from './useConfigSchema'
import { i18nT } from '../../i18n/t'
import { toPathSegment } from '../subNavParams'
import { settingsPath } from '../settingsPath'
import { CopyCommandButton } from './CopyCommandButton'

export interface SettingRefProps {
  /** The dotted config key (e.g. 'telemetry.beacon_enabled') or env var name. */
  configKey: string
  /** 'config' (default) for config.json keys; 'env' for environment variables. */
  kind?: 'config' | 'env'
  /** Backend schema index — pass from useConfigSchema() hook. Overrides internal fetch. */
  schemaIndex?: Map<string, SchemaEntry>
  /** Whether the env var popover shows set or unset commands. Default 'set'. */
  envIntent?: EnvIntent
  /** Placeholder text for the value in set-mode commands. Default 'value'. */
  valuePlaceholder?: string
}

import { ENV_SHELLS, cliValuePlaceholder, configSetCommand, VALID_ENV_KEY } from './envShellCommands'
import type { EnvIntent } from './envShellCommands'

/**
 * Build a safe internal route from a registry entry.
 * Route is constructed ONLY from registry data fields, never from the raw input string.
 * Delegates assembly to the shared settingsPath builder (segment codec + query
 * encoding in one place); keeps the null contract for an invalid tab so the
 * caller renders a plain <code> instead of a link to the settings root.
 * Uses highlight=key:<configKey> format so the consumer (useSettingHighlight) can
 * resolve directly via data-setting-key attribute, avoiding the label round-trip.
 */
function buildSettingsRoute(tab: string, configKey: string): string | null {
  // toPathSegment rejects the dot-only values ('.', '..') whose percent-forms
  // the WHATWG URL parser still resolves as dot-segments — a crafted registry
  // tab must not mint a route that normalizes outside /settings.
  if (!toPathSegment(tab)) return null
  return settingsPath({ tab, highlight: `key:${configKey}` })
}

/**
 * Get a translated tab label via the same i18n keys the Settings page nav uses.
 * Falls back to capitalizing the raw tab id if no translation key exists.
 */
function translatedTabLabel(tab: string): string {
  const key = `settings.tabs.${tab}.label`
  const translated = i18nT(key)
  // If i18next returns the raw key (no translation), fall back to capitalized id
  if (translated === key) {
    return tab.charAt(0).toUpperCase() + tab.slice(1)
  }
  return translated
}

export function SettingRef({ configKey, kind = 'config', schemaIndex: schemaIndexProp, envIntent = 'set', valuePlaceholder = 'value' }: SettingRefProps) {
  // Use internal hook when no explicit schemaIndex prop is provided.
  // Hook is always called (React rules) but its result is only used when prop is absent.
  const hookSchema = useConfigSchema()
  // When schemaIndexProp is passed (tests), treat it as the loaded schema.
  // When absent, use hook result (Map | undefined).
  const schemaIndex = schemaIndexProp !== undefined ? schemaIndexProp : hookSchema

  // Env var mode: validate key at component boundary, then render popover
  if (kind === 'env') {
    // Security: reject any configKey that doesn't match a valid env var pattern
    if (!VALID_ENV_KEY.test(configKey)) {
      return <code className="text-xs font-mono px-1 py-0.5 rounded bg-bg-accent border border-border">{configKey}</code>
    }

    const descriptionKey = envIntent === 'unset'
      ? 'components.settingRef.envVarUnsetDescription'
      : 'components.settingRef.envVarDescription'

    return (
      <Popover>
        <PopoverTrigger asChild>
          <button
            type="button"
            className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs font-mono max-w-full [overflow-wrap:anywhere] border border-accent/30 bg-accent/5 text-text-strong hover:border-accent hover:bg-accent/10 cursor-pointer transition-colors"
            aria-label={i18nT('components.settingRef.envVarAriaLabel', { key: configKey })}
          >
            <Terminal size={12} />
            <code>{configKey}</code>
          </button>
        </PopoverTrigger>
        <PopoverContent className="w-80 p-3">
          <p className="text-xs text-muted mb-2">{i18nT(descriptionKey)}</p>
          {/* Shell commands below are CLI literals — not user-visible prose. */}
          <div className="flex flex-col gap-1.5">
            {ENV_SHELLS.map(({ labelKey, command, unsetCommand }) => {
              const cmdText = envIntent === 'unset' ? unsetCommand(configKey) : command(configKey, valuePlaceholder)
              return (
                <div key={labelKey} className="flex flex-col gap-0.5">
                  <span className="text-[11px] text-muted">{i18nT(labelKey)}</span>
                  <div className="flex items-center gap-1">
                    <code className="text-xs bg-bg px-1.5 py-1 rounded border border-border block font-mono break-all flex-1">
                      {cmdText}
                    </code>
                    <CopyCommandButton text={cmdText} />
                  </div>
                </div>
              )
            })}
          </div>
        </PopoverContent>
      </Popover>
    )
  }

  // Config key mode: resolve against registry + schema
  const resolution = resolveSettingRef(configKey, schemaIndex)

  if (resolution.mode === 'ui') {
    const route = buildSettingsRoute(resolution.entry.tab, configKey)
    if (!route) {
      // Safety fallback: cannot build a valid route
      return <code className="text-xs font-mono px-1 py-0.5 rounded bg-bg-accent border border-border">{configKey}</code>
    }
    return (
      <Link
        to={route}
        className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs font-mono max-w-full [overflow-wrap:anywhere] bg-accent/10 border border-accent/30 text-accent hover:bg-accent/20 transition-colors no-underline"
        aria-label={i18nT('components.settingRef.openSettingsAriaLabel', { tab: translatedTabLabel(resolution.entry.tab), key: configKey })}
      >
        <Settings size={12} />
        <span>{configKey}</span>
      </Link>
    )
  }

  if (resolution.mode === 'file') {
    const placeholder = cliValuePlaceholder(resolution.schemaEntry.type, valuePlaceholder)
    const cliCommand = configSetCommand(configKey, placeholder)
    return (
      <Popover>
        <PopoverTrigger asChild>
          <button
            type="button"
            className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs font-mono max-w-full [overflow-wrap:anywhere] border border-accent/30 bg-accent/5 text-text-strong hover:border-accent hover:bg-accent/10 cursor-pointer transition-colors"
            aria-label={i18nT('components.settingRef.fileSettingAriaLabel', { key: configKey })}
          >
            <Terminal size={12} />
            <code>{configKey}</code>
          </button>
        </PopoverTrigger>
        <PopoverContent className="w-72 p-3">
          <p className="text-xs text-muted mb-2">{i18nT('components.settingRef.fileOnlyDescription')}</p>
          <div className="flex items-center gap-1 mb-1.5">
            <code className="text-xs bg-bg px-1.5 py-1 rounded border border-border block font-mono break-all flex-1">
              {cliCommand}
            </code>
            <CopyCommandButton text={cliCommand} />
          </div>
          <p className="text-[11px] text-muted">{i18nT('components.settingRef.orEditConfigJson')}</p>
        </PopoverContent>
      </Popover>
    )
  }

  // mode === 'unknown': plain code, no link, no navigation, no icon
  return <code className="text-xs font-mono px-1 py-0.5 rounded bg-bg-accent border border-border">{configKey}</code>
}
