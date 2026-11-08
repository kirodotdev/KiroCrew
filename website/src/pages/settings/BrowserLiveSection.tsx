/**
 * Settings -> Browser, the setting rows: those under the main card ("Which
 * browser", "Show the live view in chat", and "Use <provider> sign-in" when an
 * edition supplies a sign-in provider) and the collapsed More group (Quality,
 * Max frame rate, the Preview tab's local addresses, Engine, Run with no window).
 *
 * Every control carries its `configKey` (except Run with no window, the second
 * view of `browser.choice`, which anchors by label), so a `<SettingRef>` chip or
 * a command palette hit can land on it; More is a collapsible `SettingsSection`, which opens
 * itself when a deep link targets a row inside it. Every explanation is a `hint`
 * (an info tip), never a description line.
 *
 * `browser.choice` is split across two controls; `browserStatus.ts` defines the
 * mapping (`pickerValueOf`, `choiceFromPicker`, `choiceFromHeadless`).
 * `browser.live.mask_passwords` has no control: it stays on, and the CLI can still
 * change it.
 */
import type { BrowserCredentialsStatus, BrowserStatusData } from '../../api/client'
import { SettingsCard, SettingsSection, SettingsSelect, SettingsToggle } from '../../components/settings'
import { i18nT } from '../../i18n/t'
import { BrowserCredentialDomains } from './BrowserCredentialDomains'
import { BrowserEngineRow } from './BrowserEngineSection'
import { BrowserLocalOrigins } from './BrowserLocalOrigins'
import {
  CREDENTIAL_LINE_KEY,
  PICKER_CHOICES,
  choiceFromHeadless,
  choiceFromPicker,
  isHeadlessChoice,
  pickerValueOf,
  type PickerChoice,
} from './browserStatus'
import { LIVE_MAX_FPS, LIVE_QUALITIES, type BrowserSettingsView } from './useBrowserSettings'

/** Catalog KEY per quality, flat literals for the key-reference gate. */
const QUALITY_LABEL_KEY: Record<(typeof LIVE_QUALITIES)[number], string> = {
  auto: 'pages.settings.browserPanel.quality_auto',
  high: 'pages.settings.browserPanel.quality_high',
  medium: 'pages.settings.browserPanel.quality_medium',
  low: 'pages.settings.browserPanel.quality_low',
}

const CHOICE_LABEL_KEY: Record<PickerChoice, string> = {
  auto: 'pages.settings.browserPanel.choice_auto',
  user_chrome: 'pages.settings.browserPanel.choice_user_chrome',
  kiro_chromium: 'pages.settings.browserPanel.choice_kiro_chromium',
}

type SetConfig = (path: string, value: unknown) => void

/**
 * The sign-in row's status line: how many cookies the last apply set, the
 * provider's own renew hint verbatim when it gave one, or a fixed catalog line.
 */
function credentialLine(cred: BrowserCredentialsStatus): string | undefined {
  if (cred.state === 'applied') {
    return i18nT('pages.settings.browserPanel.credentials_applied', { count: cred.applied ?? 0 })
  }
  if (cred.state === 'needs_refresh') {
    const hint = cred.provider_status?.message
    return hint || i18nT('pages.settings.browserPanel.credentials_needs_refresh', { provider: cred.provider ?? '' })
  }
  const key = CREDENTIAL_LINE_KEY[cred.state]
  return key ? i18nT(key) : undefined
}

export function BrowserMainControls({ view, credentials, disabled, setConfig }: {
  view: BrowserSettingsView
  /** The status read's `credentials` block; the sign-in row shows only with a provider. */
  credentials?: BrowserCredentialsStatus | null
  disabled: boolean
  setConfig: SetConfig
}) {
  const provider = credentials?.provider ?? null
  return (
    <SettingsCard>
      <SettingsSelect
        label={i18nT('pages.settings.browserPanel.choice_label')}
        hint={i18nT('pages.settings.browserPanel.choice_desc')}
        configKey="browser.choice"
        value={pickerValueOf(view.choice)}
        options={[...PICKER_CHOICES]}
        optionLabels={PICKER_CHOICES.map(c => i18nT(CHOICE_LABEL_KEY[c]))}
        onChange={v => {
          const next = choiceFromPicker(view.choice, v)
          if (next !== null) setConfig('browser.choice', next)
        }}
        disabled={disabled}
      />
      <SettingsToggle
        label={i18nT('pages.settings.browserPanel.live_enabled_label')}
        hint={i18nT('pages.settings.browserPanel.live_enabled_desc')}
        configKey="browser.live.enabled"
        checked={view.live.enabled}
        onChange={v => setConfig('browser.live.enabled', v)}
        disabled={disabled}
      />
      {credentials && provider && (
        <SettingsToggle
          label={i18nT('pages.settings.browserPanel.credentials_label', { provider })}
          hint={i18nT('pages.settings.browserPanel.credentials_hint', { provider })}
          description={credentialLine(credentials)}
          configKey="browser.credentials.enabled"
          checked={credentials.enabled}
          onChange={v => setConfig('browser.credentials.enabled', v)}
          disabled={disabled}
        />
      )}
    </SettingsCard>
  )
}

export function BrowserMoreSection({ view, status, disabled, setConfig, installing, installError, onInstall, onRedownload }: {
  view: BrowserSettingsView
  status: BrowserStatusData
  disabled: boolean
  setConfig: SetConfig
  installing: boolean
  installError: string | null
  onInstall: () => void
  onRedownload: () => void
}) {
  const fpsOptions = LIVE_MAX_FPS.map(String)
  return (
    <SettingsSection title={i18nT('pages.settings.browserPanel.more_title')} collapsible>
      <SettingsCard>
        <SettingsSelect
          label={i18nT('pages.settings.browserPanel.quality_label')}
          hint={i18nT('pages.settings.browserPanel.quality_desc')}
          configKey="browser.live.quality"
          value={view.live.quality}
          options={[...LIVE_QUALITIES]}
          optionLabels={LIVE_QUALITIES.map(q => i18nT(QUALITY_LABEL_KEY[q]))}
          onChange={v => setConfig('browser.live.quality', v)}
          disabled={disabled}
        />
        <SettingsSelect
          label={i18nT('pages.settings.browserPanel.max_fps_label')}
          hint={i18nT('pages.settings.browserPanel.max_fps_desc')}
          configKey="browser.live.max_fps"
          value={String(view.live.maxFps)}
          options={fpsOptions}
          optionLabels={LIVE_MAX_FPS.map(n => i18nT('pages.settings.browserPanel.fps_value', { fps: n }))}
          onChange={v => setConfig('browser.live.max_fps', Number(v))}
          disabled={disabled}
        />
        <BrowserLocalOrigins
          origins={view.localOrigins}
          disabled={disabled}
          onChange={next => setConfig('dashboard.browser_local_origins', next)}
        />
        {status.credentials?.provider && (
          <BrowserCredentialDomains
            domains={view.credentialDomains}
            provider={status.credentials.provider}
            disabled={disabled}
            onChange={next => setConfig('browser.credentials.domains', next)}
          />
        )}
        <BrowserEngineRow
          status={status}
          installing={installing}
          installError={installError}
          onInstall={onInstall}
          onRedownload={onRedownload}
        />
        <SettingsToggle
          label={i18nT('pages.settings.browserPanel.headless_label')}
          hint={i18nT('pages.settings.browserPanel.headless_hint')}
          // No configKey: this switch is a second view of `browser.choice`, and
          // the key anchors on the Which browser picker alone, so a key link or a
          // guide step for the key never lands here. The row anchors by label.
          checked={isHeadlessChoice(view.choice)}
          onChange={v => setConfig('browser.choice', choiceFromHeadless(v))}
          disabled={disabled}
        />
      </SettingsCard>
    </SettingsSection>
  )
}
