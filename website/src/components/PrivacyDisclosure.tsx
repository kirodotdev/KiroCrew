import { Activity, EyeOff, HardDrive, PackageCheck, Radio, Terminal } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Trans } from 'react-i18next'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { SettingRef } from './settingRef/SettingRef'
import { DO_NOT_TRACK_COMMANDS } from './settingRef/envShellCommands'

/** Read-only CLI twins of the toggle, kept for headless hosts and for the one
 * case the toggle cannot win: a `config.local.json` overlay or the env var. */
export const COMMANDS = [
  'kirocrew telemetry status',
  'kirocrew telemetry disable',
] as const

// Keys held in an indexed `as const` map of full literals rather than inline on each
// SHELL_COMMANDS entry: check-i18n-keys.mjs resolves a map access to the map's value
// set, but cannot follow a key destructured out of an array of objects, which would
// exempt the call site from key-existence verification.
const SHELL_LABEL_KEY = {
  macos: 'privacyDisclosure.shellMacOSLinuxLabel',
  powershell: 'privacyDisclosure.shellPowerShellLabel',
  cmd: 'privacyDisclosure.shellWindowsCmdLabel',
} as const

/** The three per-shell env-var forms, one per `SHELL_LABEL_KEY` shell, so a
 * shell cannot render without its label. The command text lives in
 * `envShellCommands.ts`, the module for CLI syntax that is never translated.
 *
 * `DO_NOT_TRACK`, not `KIROCREW_TELEMETRY_DISABLED`: with local recording on by
 * default, the env var shown here must turn off everything the command above it
 * does. `KIROCREW_TELEMETRY_DISABLED` stops only the heartbeat, so a user who
 * copied it would believe metrics were off while recording went on. */
const DO_NOT_TRACK_BY_SHELL: Record<keyof typeof SHELL_LABEL_KEY, string> = DO_NOT_TRACK_COMMANDS
export const SHELL_COMMANDS = (Object.keys(SHELL_LABEL_KEY) as Array<keyof typeof SHELL_LABEL_KEY>).map(
  shell => ({ shell, command: DO_NOT_TRACK_BY_SHELL[shell] }),
)

/** The backend's stable suppression discriminants, mapped to catalog keys.
 *
 * The panel renders THIS, never the sibling `reason` string: that field is
 * untranslated operator prose ("already sent today (2026-08-04)"), and
 * interpolating it into a UI that ships in 10 languages leaks a developer
 * diagnostic onto the user's screen. `reason` stays in the payload for bug
 * reports and logs.
 */
const NOT_SENDING_KEY = {
  already_sent_today: 'privacyDisclosure.notSendingAlreadySentToday',
  ci: 'privacyDisclosure.notSendingCi',
  non_default_home: 'privacyDisclosure.notSendingNonDefaultHome',
  no_endpoint: 'privacyDisclosure.notSendingNoEndpoint',
  unreadable_home: 'privacyDisclosure.notSendingUnreadableHome',
} as const

export interface BeaconStatus {
  enabled?: boolean
  would_send?: boolean
  reason?: string
  /** Stable discriminant for why nothing is being sent; see `NOT_SENDING_KEY`. */
  reason_code?: string
  endpoint_configured?: boolean
  env_override?: boolean
  env_var?: string
  overlay_override?: boolean
  /** An enterprise ceiling pins `capabilities.telemetry` off. Unlike the other
   *  two overrides this one the user cannot lift, and the config PATCH route
   *  returns 403 for a re-enable — so the toggle must be disabled, not just
   *  annotated. */
  governance_override?: boolean
}

/**
 * The heartbeat opt-out control.
 *
 * The toggle writes `telemetry.beacon_enabled` (the same key
 * `kirocrew telemetry disable` persists), so the choice survives restarts and
 * upgrades. It reports the EFFECTIVE state, not just the stored flag: an
 * enterprise governance ceiling, the env var, a CI host, a non-default data
 * home, or a `config.local.json` overlay can all suppress sending
 * independently, and a privacy control that claims "on" while something else
 * silences the beacon — or claims "off" while it still sends — is worse than no
 * control. When any of those pins the state the toggle is disabled rather than
 * offering a write that cannot take effect.
 */
interface TelemetryToggleProps {
  /** The caller draws the row: the Settings panel passes its registry-aware
   * `SettingsToggle`, so the settings extractor finds the control in a
   * `pages/settings/*` file. This component owns the state and the notes. */
  renderControl: (props: {
    checked: boolean
    onChange: (value: boolean) => void
    disabled: boolean
  }) => React.ReactNode
}

export function TelemetryToggle({ renderControl }: TelemetryToggleProps) {
  const qc = useQueryClient()
  const statusQ = useQuery<BeaconStatus>({
    queryKey: ['beaconStatus'],
    queryFn: () => api.beaconStatus(),
  })

  const enabled = statusQ.data?.enabled ?? false
  const envOverride = statusQ.data?.env_override ?? false
  // A config.local.json entry deep-merges over the file this toggle writes, so
  // the switch would snap back after a successful save. Disable it and say why
  // rather than offering a write the overlay silently undoes.
  const overlayOverride = statusQ.data?.overlay_override ?? false
  // An enterprise ceiling. Listed FIRST in the note precedence below because it
  // is the only one the user has no way to lift — telling them to unset an env
  // var when their administrator pinned the policy would be a dead end.
  const govOverride = statusQ.data?.governance_override ?? false
  const pinned = govOverride || envOverride || overlayOverride

  const toggleMut = useMutation({
    mutationFn: (value: boolean) => api.patchConfig('telemetry.beacon_enabled', value),
    onMutate: async (value: boolean) => {
      await qc.cancelQueries({ queryKey: ['beaconStatus'] })
      const prev = qc.getQueryData<BeaconStatus>(['beaconStatus'])
      qc.setQueryData<BeaconStatus>(['beaconStatus'], old => ({ ...(old ?? {}), enabled: value }))
      return { prev }
    },
    onError: (_err, _value, ctx) => {
      if (ctx?.prev) qc.setQueryData(['beaconStatus'], ctx.prev)
    },
    // Refetch rather than trusting the optimistic value: the server decides the
    // effective verdict, and only it knows whether an overlay shadowed the write.
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['beaconStatus'] })
      qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
      // The heartbeat-off load rule ties unwritten recording to this switch, so
      // a heartbeat change can move recording too.
      qc.invalidateQueries({ queryKey: ['telemetryCollection'] })
    },
  })

  // The stored flag and the effective verdict disagree — say so instead of
  // letting the toggle imply the beacon's real state.
  const shadowed = enabled && !statusQ.data?.would_send && !statusQ.isLoading

  return (
    <div>
      {renderControl({
        checked: enabled,
        onChange: value => toggleMut.mutate(value),
        disabled: statusQ.isLoading || toggleMut.isPending || pinned,
      })}
      {toggleMut.isError && (
        <p role="alert" className="text-[12px] text-danger mt-1">
          {i18nT('privacyDisclosure.toggleSaveFailed')}
        </p>
      )}
      {/* One note only, strongest-first: an admin pin outranks an env var, which
          outranks an overlay. Stacking all three would offer remedies that the
          outer pin makes pointless. */}
      {govOverride && (
        <p className="text-[12px] text-muted mt-1">
          {i18nT('privacyDisclosure.governanceOverrideNote')}
        </p>
      )}
      {!govOverride && envOverride && (
        <p className="text-[12px] text-muted mt-1">
          <Trans
            i18nKey="privacyDisclosure.envOverrideWithSettingRef"
            components={{
              settingRef: <SettingRef kind="env" configKey={statusQ.data?.env_var ?? 'KIROCREW_TELEMETRY_DISABLED'} envIntent="unset" />,
            }}
          />
        </p>
      )}
      {!govOverride && !envOverride && overlayOverride && (
        <p className="text-[12px] text-muted mt-1">
          {i18nT('privacyDisclosure.overlayOverrideNote')}
        </p>
      )}
      {!pinned && shadowed && (
        <p className="text-[12px] text-muted mt-1">
          {i18nT(
            NOT_SENDING_KEY[statusQ.data?.reason_code as keyof typeof NOT_SENDING_KEY]
            ?? 'privacyDisclosure.notSendingGeneric',
          )}
        </p>
      )}
    </div>
  )
}

/** One Details row: icon + heading, a body line, and an optional field list. */
function DisclosureRow({
  icon,
  title,
  body,
  fields,
}: {
  icon: React.ReactNode
  title: string
  body: string
  fields?: string
}) {
  return (
    <div>
      <h3 className="text-[13px] font-semibold text-text-strong mb-1 flex items-center gap-2">
        {icon}
        {title}
      </h3>
      <p className="text-[13px] text-muted leading-relaxed">{body}</p>
      {fields && <p className="text-[12px] text-muted leading-relaxed mt-1">{fields}</p>}
    </div>
  )
}

/**
 * The Details rows of Settings > Privacy: what each switch sends, what is never
 * sent, and what stays local. Each row is a direct child of the caller's
 * `SettingsCard`, so the card draws the hairlines between them.
 *
 * `payloadFields` must stay an exhaustive list of what `beacon.payload()`
 * actually sends. It is the transparency commitment, so a field added to the
 * wire without a line here would make this text a false statement; the
 * `PrivacyPanel.test.tsx` field-count assertion is what keeps them in step.
 *
 * `shared`: the reader is a teammate on someone else's Crew, so the records stay
 * on the Crew's computer, not on their device. `kiroTelemetry`: this build lists
 * the Kiro telemetry destination, so its row is shown; a build without it (an
 * LTS build, a CI host) shows no row for a destination it does not have.
 */
export function PrivacyDisclosureSections({ shared = false, kiroTelemetry = false }: { shared?: boolean; kiroTelemetry?: boolean } = {}) {
  return (
    <>
      <DisclosureRow
        icon={<Radio className="lucide-inline" aria-hidden="true" />}
        title={i18nT('privacyDisclosure.anonymousHeartbeatTitle')}
        body={i18nT('privacyDisclosure.anonymousHeartbeatBody')}
        fields={i18nT('privacyDisclosure.payloadFields')}
      />
      <DisclosureRow
        icon={<PackageCheck className="lucide-inline" aria-hidden="true" />}
        title={i18nT('privacyDisclosure.installReceiptTitle')}
        body={i18nT('privacyDisclosure.installReceiptBody')}
        fields={i18nT('privacyDisclosure.installReceiptFields')}
      />
      {kiroTelemetry && (
        <DisclosureRow
          icon={<Activity className="lucide-inline" aria-hidden="true" />}
          title={i18nT('privacyDisclosure.kiroTelemetryTitle')}
          body={i18nT('privacyDisclosure.kiroTelemetryBody')}
        />
      )}
      <DisclosureRow
        icon={<EyeOff className="lucide-inline" aria-hidden="true" />}
        title={i18nT('privacyDisclosure.dataNeverSentTitle')}
        body={i18nT('privacyDisclosure.dataNeverSentBody')}
      />
      <DisclosureRow
        icon={<HardDrive className="lucide-inline" aria-hidden="true" />}
        title={shared ? i18nT('privacyDisclosure.localDataTitleShared') : i18nT('privacyDisclosure.localDataTitle')}
        body={shared ? i18nT('privacyDisclosure.localDataBodyShared') : i18nT('privacyDisclosure.localDataBody')}
      />
    </>
  )
}

/** The command-line and env-var equivalents of the switches, as one Details row. */
export function PrivacyCommandList() {
  return (
    <div>
      <h3 className="text-[13px] font-semibold text-text-strong mb-2 flex items-center gap-2">
        <Terminal className="lucide-inline" aria-hidden="true" />
        {i18nT('privacyDisclosure.controlsTitle')}
      </h3>
      <div className="flex flex-wrap gap-2">
        {COMMANDS.map(command => (
          <code key={command} className="text-[12px] text-text bg-bg border border-border rounded-md px-2 py-1 select-all">
            {command}
          </code>
        ))}
      </div>
      {/* Say what the env var stops: the status and disable commands above it
          are self-describing, this line is not. */}
      <p className="mt-3 mb-1.5 text-[12px] text-muted">{i18nT('privacyDisclosure.doNotTrackCaption')}</p>
      <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-3 gap-y-1.5">
        {SHELL_COMMANDS.map(({ shell, command }) => (
          <div key={command} className="contents">
            <span className="text-[12px] font-medium text-muted">
              {i18nT(SHELL_LABEL_KEY[shell])}
            </span>
            <code className="justify-self-start max-w-full overflow-x-auto text-[12px] text-text bg-bg border border-border rounded-md px-2 py-1 select-all">
              {command}
            </code>
          </div>
        ))}
      </div>
    </div>
  )
}
