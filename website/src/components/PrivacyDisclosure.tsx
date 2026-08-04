import { EyeOff, HardDrive, PackageCheck, Radio } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Toggle } from './ui'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'

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

/** The three per-shell env-var forms, keyed by the same shell ids as
 * `SHELL_LABEL_KEY` so a new shell cannot be added without its label. */
export const SHELL_COMMANDS = [
  { shell: 'macos', command: 'export KIROCREW_TELEMETRY_DISABLED=1' },
  { shell: 'powershell', command: "$env:KIROCREW_TELEMETRY_DISABLED = '1'" },
  { shell: 'cmd', command: 'set KIROCREW_TELEMETRY_DISABLED=1' },
] as const satisfies ReadonlyArray<{
  shell: keyof typeof SHELL_LABEL_KEY
  command: string
}>

export interface BeaconStatus {
  enabled?: boolean
  would_send?: boolean
  reason?: string
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
export function TelemetryToggle() {
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
    },
  })

  // The stored flag and the effective verdict disagree — say so instead of
  // letting the toggle imply the beacon's real state.
  const shadowed = enabled && !statusQ.data?.would_send && !statusQ.isLoading

  return (
    <div>
      <div className="flex items-center justify-between gap-4 py-1.5">
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold text-text">
            {i18nT('privacyDisclosure.toggleLabel')}
          </div>
          <div className="text-[12px] text-muted mt-0.5">
            {i18nT('privacyDisclosure.toggleDescription')}
          </div>
        </div>
        <Toggle
          checked={enabled}
          onChange={v => toggleMut.mutate(v)}
          disabled={statusQ.isLoading || toggleMut.isPending || pinned}
          label={i18nT('privacyDisclosure.toggleLabel')}
        />
      </div>
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
          {i18nT('privacyDisclosure.envOverrideNote', {
            envVar: statusQ.data?.env_var ?? 'KIROCREW_TELEMETRY_DISABLED',
          })}
        </p>
      )}
      {!govOverride && !envOverride && overlayOverride && (
        <p className="text-[12px] text-muted mt-1">
          {i18nT('privacyDisclosure.overlayOverrideNote')}
        </p>
      )}
      {!pinned && shadowed && (
        <p className="text-[12px] text-muted mt-1">
          {i18nT('privacyDisclosure.notSendingNote', { reason: statusQ.data?.reason ?? '' })}
        </p>
      )}
    </div>
  )
}

/** One disclosure section: icon + heading + body. */
function DisclosureSection({
  icon,
  title,
  body,
}: {
  icon: React.ReactNode
  title: string
  body: string
}) {
  return (
    <div>
      <h3 className="text-sm font-semibold tracking-tight text-text-strong mb-1.5 flex items-center gap-2">
        {icon}
        {title}
      </h3>
      <p className="text-sm text-muted leading-relaxed">{body}</p>
    </div>
  )
}

/**
 * The disclosure copy itself — the heartbeat payload, official-app receipt, the
 * never-sent list, and the local-data boundary. Shared verbatim by Settings →
 * Privacy and the onboarding privacy step so the two can never drift; only the
 * surrounding chrome differs.
 *
 * `payloadFields` must stay an exhaustive list of what `beacon.payload()`
 * actually sends. It is the transparency commitment, so a field added to the
 * wire without a line here would make this text a false statement — the
 * `PrivacyNotice.test.tsx` field-count assertion is what keeps them in step.
 */
export function PrivacyDisclosureSections() {
  return (
    <div className="flex flex-col gap-5">
      <div>
        <h3 className="text-sm font-semibold tracking-tight text-text-strong mb-1.5 flex items-center gap-2">
          <Radio className="lucide-inline" aria-hidden="true" />
          {i18nT('privacyDisclosure.anonymousHeartbeatTitle')}
        </h3>
        <p className="text-sm text-muted leading-relaxed">
          {i18nT('privacyDisclosure.anonymousHeartbeatBody')}
        </p>
        {/* The complete payload, kept verbatim as the transparency commitment
            but rendered as one scannable line instead of a paragraph a normal
            user will skip. */}
        <p className="text-[12px] text-muted leading-relaxed mt-1.5">
          {i18nT('privacyDisclosure.payloadFields')}
        </p>
      </div>
      <div>
        <h3 className="text-sm font-semibold tracking-tight text-text-strong mb-1.5 flex items-center gap-2">
          <PackageCheck className="lucide-inline" aria-hidden="true" />
          {i18nT('privacyDisclosure.installReceiptTitle')}
        </h3>
        <p className="text-sm text-muted leading-relaxed">
          {i18nT('privacyDisclosure.installReceiptBody')}
        </p>
        <p className="text-[12px] text-muted leading-relaxed mt-1.5">
          {i18nT('privacyDisclosure.installReceiptFields')}
        </p>
      </div>
      <DisclosureSection
        icon={<EyeOff className="lucide-inline" aria-hidden="true" />}
        title={i18nT('privacyDisclosure.dataNeverSentTitle')}
        body={i18nT('privacyDisclosure.dataNeverSentBody')}
      />
      <DisclosureSection
        icon={<HardDrive className="lucide-inline" aria-hidden="true" />}
        title={i18nT('privacyDisclosure.localDataTitle')}
        body={i18nT('privacyDisclosure.localDataBody')}
      />
    </div>
  )
}

/** The CLI/env-var equivalents, demoted beneath the toggle. */
export function PrivacyCommandList() {
  return (
    <div className="flex flex-col items-start gap-2" aria-label={i18nT('privacyDisclosure.controlsTitle')}>
      {COMMANDS.map(command => (
        <code key={command} className="text-[13px] text-text bg-bg border border-border rounded-md px-2.5 py-1.5 select-all">
          {command}
        </code>
      ))}
      {SHELL_COMMANDS.map(({ shell, command }) => (
        <div key={command} className="flex max-w-full flex-col items-start gap-1">
          <span className="text-[12px] font-medium text-muted">
            {i18nT(SHELL_LABEL_KEY[shell])}
          </span>
          <code className="max-w-full overflow-x-auto text-[13px] text-text bg-bg border border-border rounded-md px-2.5 py-1.5 select-all">
            {command}
          </code>
        </div>
      ))}
    </div>
  )
}
