import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SlidersHorizontal } from 'lucide-react'
import { Trans } from 'react-i18next'
import { api, ApiError } from '../../api/client'
import { Card, CardTitle } from '../../components/ui'
import { SettingsToggle } from '../../components/settings'
import { SettingRef } from '../../components/settingRef/SettingRef'
import {
  NOT_SENDING_KEY,
  PrivacyCommandList,
  PrivacyDisclosureSections,
  type BeaconStatus,
} from '../../components/PrivacyDisclosure'
import { i18nT } from '../../i18n/t'

/** GET /api/telemetry/collection */
interface CollectionStatus {
  /** EFFECTIVE state — `KIROCREW_TELEMETRY` can override the stored flag. */
  enabled: boolean
  env_pinned?: boolean
  env_var?: string
  /** A `config.local.json` entry would make a successful write snap back. */
  overlay_override?: boolean
  /** `telemetry.otlp_endpoint` is set, so enabling here would also export. */
  otlp_configured?: boolean
  metrics_dir?: string
}

/**
 * The local metric-collection switch.
 *
 * Lives here rather than on the Telemetry panel it feeds for two reasons: this is
 * where the user already comes to decide what gets collected, and only settings
 * rendered in a `pages/settings/*` panel are picked up by the settings-registry
 * extractor — which is what lets `<SettingRef configKey="telemetry.enabled" />`
 * resolve to a deep link into this control instead of a copyable CLI command.
 *
 * The write takes effect immediately: the PATCH route drops the process's
 * memoized metrics recorder, so the next metric call site rebuilds one from the
 * value just written. When the env var or a config overlay is what decides, the
 * switch disables itself and says so rather than offering a write that cannot
 * hold.
 */
/** A 409 from the config route is the egress refusal, not a transient failure. */
function isEgressRefusal(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409
}

/**
 * The heartbeat opt-out control, registry-visible (#2689).
 *
 * `PrivacyDisclosure.tsx`'s `TelemetryToggle` renders the identical control —
 * same query, same mutation, same override precedence — but as a bare
 * `<Toggle>`, and it stays that way: it is shared with the onboarding privacy
 * step (`PrivacyChapter.tsx`), which is not a `pages/settings/*` panel and
 * has no config-key concept of its own. The settings-registry extractor
 * (`settingsExtract.ts`) only finds a `<SettingsToggle configKey=…>` element
 * whose SOURCE TEXT is literally inside a `PANEL_TAB_MAP`-mapped file — it
 * does not follow into an imported component's own JSX — so `<SettingRef
 * configKey="telemetry.beacon_enabled" />` could never resolve to a deep link
 * while the only rendering of this control lived in `PrivacyDisclosure.tsx`.
 * This duplicates the toggle's state/logic (accepted cost, per the issue) so
 * this file's own JSX carries a `SettingsToggle` the extractor can see.
 */
function BeaconToggle() {
  const qc = useQueryClient()
  const statusQ = useQuery<BeaconStatus>({
    queryKey: ['beaconStatus'],
    queryFn: () => api.beaconStatus(),
  })

  const enabled = statusQ.data?.enabled ?? false
  const envOverride = statusQ.data?.env_override ?? false
  const overlayOverride = statusQ.data?.overlay_override ?? false
  const govOverride = statusQ.data?.governance_override ?? false
  const pinned = govOverride || envOverride || overlayOverride
  const shadowed = enabled && !statusQ.data?.would_send && !statusQ.isLoading

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
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['beaconStatus'] })
      qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
  })

  const NOTE_ID = 'beacon-toggle-note'
  const showNote = pinned || (!pinned && shadowed)

  return (
    <div>
      <SettingsToggle
        label={i18nT('privacyDisclosure.toggleLabel')}
        description={i18nT('privacyDisclosure.toggleDescription')}
        checked={enabled}
        onChange={v => toggleMut.mutate(v)}
        disabled={statusQ.isLoading || toggleMut.isPending || pinned}
        configKey="telemetry.beacon_enabled"
        describedBy={showNote ? NOTE_ID : undefined}
      />
      {toggleMut.isError && (
        <p role="alert" className="text-[12px] text-danger mt-1">
          {i18nT('privacyDisclosure.toggleSaveFailed')}
        </p>
      )}
      {/* Precedence mirrors TelemetryToggle: an admin pin outranks the env
          var, which outranks the overlay; only shown at all when nothing
          pins it does the "not sending" note apply. */}
      {govOverride && (
        <p id={NOTE_ID} className="text-[12px] text-muted mt-1">
          {i18nT('privacyDisclosure.governanceOverrideNote')}
        </p>
      )}
      {!govOverride && envOverride && (
        <p id={NOTE_ID} className="text-[12px] text-muted mt-1">
          <Trans
            i18nKey="privacyDisclosure.envOverrideWithSettingRef"
            components={{
              settingRef: <SettingRef kind="env" configKey={statusQ.data?.env_var ?? 'KIROCREW_TELEMETRY_DISABLED'} envIntent="unset" />,
            }}
          />
        </p>
      )}
      {!govOverride && !envOverride && overlayOverride && (
        <p id={NOTE_ID} className="text-[12px] text-muted mt-1">
          {i18nT('privacyDisclosure.overlayOverrideNote')}
        </p>
      )}
      {!pinned && shadowed && (
        <p id={NOTE_ID} className="text-[12px] text-muted mt-1">
          {i18nT(
            NOT_SENDING_KEY[statusQ.data?.reason_code as keyof typeof NOT_SENDING_KEY]
            ?? 'privacyDisclosure.notSendingGeneric',
          )}
        </p>
      )}
    </div>
  )
}

function MetricRecordingToggle() {
  const qc = useQueryClient()
  const statusQ = useQuery<CollectionStatus>({
    queryKey: ['telemetryCollection'],
    queryFn: () => api.collectionStatus(),
  })
  const enabled = statusQ.data?.enabled ?? false
  // Three independent reasons the config file is not what decides. Each disables
  // the switch, because offering a write that cannot hold is worse than no switch.
  const envPinned = !!statusQ.data?.env_pinned
  const overlayPinned = !!statusQ.data?.overlay_override
  const otlpConfigured = !!statusQ.data?.otlp_configured
  // A configured egress endpoint pins the ENABLE direction only. Turning recording
  // OFF is exactly what a user on such a host most needs — the config route allows
  // it for that reason — so disabling the whole control here would strand them with
  // export running and no way to stop it.
  const pinned = envPinned || overlayPinned || (otlpConfigured && !enabled)
  // Two independent notes, not one priority chain: a pin reason and the egress
  // fact answer different questions ("why can't I change this" vs "where do my
  // metrics go"), and collapsing them let an env pin hide the fact that metrics
  // leave the machine. The switch points at both — a disabled control whose
  // explanation is not associated with it announces no reason at all.
  const PIN_NOTE_ID = 'record-metrics-pin-note'
  const EGRESS_NOTE_ID = 'record-metrics-egress-note'
  const showPinNote = envPinned || overlayPinned
  const describedBy =
    [showPinNote ? PIN_NOTE_ID : '', otlpConfigured ? EGRESS_NOTE_ID : '']
      .filter(Boolean)
      .join(' ') || undefined

  const mut = useMutation({
    mutationFn: (value: boolean) => api.patchConfig('telemetry.enabled', value),
    onMutate: async (value: boolean) => {
      await qc.cancelQueries({ queryKey: ['telemetryCollection'] })
      const prev = qc.getQueryData<CollectionStatus>(['telemetryCollection'])
      qc.setQueryData<CollectionStatus>(['telemetryCollection'], old => ({ ...(old ?? {}), enabled: value }))
      return { prev }
    },
    onError: (_err, _value, ctx) => {
      if (ctx?.prev) qc.setQueryData(['telemetryCollection'], ctx.prev)
    },
    // Refetch rather than trusting the optimistic value: the server owns the
    // effective verdict, and only it knows whether an overlay shadowed the write.
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['telemetryCollection'] })
      qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
  })

  return (
    <div>
      <SettingsToggle
        label={i18nT('pages.settings.privacyPanel.recordMetricsLabel')}
        description={
          // Never claim locality on a host that exports: the description is the
          // full-weight line a skimming user reads before flipping the switch.
          otlpConfigured
            ? i18nT('pages.settings.privacyPanel.recordMetricsDescriptionExporting')
            : i18nT('pages.settings.privacyPanel.recordMetricsDescription')
        }
        checked={enabled}
        onChange={v => mut.mutate(v)}
        disabled={statusQ.isLoading || mut.isPending || pinned}
        configKey="telemetry.enabled"
        describedBy={describedBy}
      />
      {mut.isError && (
        <p role="alert" className="text-[12px] text-danger mt-1">
          {/* A 409 is the egress refusal, and "try again" can never succeed against
              it — name the actual reason instead. */}
          {isEgressRefusal(mut.error)
            ? i18nT('pages.settings.privacyPanel.recordMetricsSaveRefusedEgress')
            : i18nT('pages.settings.privacyPanel.recordMetricsSaveFailed')}
        </p>
      )}
      {/* The egress fact carries body weight, not muted fine print: it contradicts
          the "on this machine" mental model and is a privacy decision. */}
      {otlpConfigured && (
        <p id={EGRESS_NOTE_ID} className="text-[13px] text-text mt-1">
          {/* Literal keys, one per state: a key assembled from a variable is what
              the dynamic-keys ratchet exists to stop, and it would make the string
              invisible to the key-reference gate. */}
          {enabled
            ? i18nT('pages.settings.privacyPanel.recordMetricsEgressOn')
            : i18nT('pages.settings.privacyPanel.recordMetricsEgressOff')}
        </p>
      )}
      {/* The pin reason, strongest-first: the env var is resolved inside the
          collector, an overlay is merged before the collector ever reads it. */}
      {showPinNote && (
        <p id={PIN_NOTE_ID} className="text-[12px] text-muted mt-1">
          {envPinned
            ? i18nT('pages.settings.privacyPanel.recordMetricsEnvPinned', {
                envVar: statusQ.data?.env_var ?? 'KIROCREW_TELEMETRY',
              })
            : i18nT('pages.settings.privacyPanel.recordMetricsOverlayPinned')}
        </p>
      )}
    </div>
  )
}

/** Durable disclosure surface. This page explains controls and offers the
 * in-product opt-out, but does not ask for consent or gate use of the
 * application.
 *
 * The disclosure copy, the toggle, and the command list live in
 * `components/PrivacyDisclosure.tsx` because the onboarding privacy step renders
 * the same three pieces — single-sourcing them is what keeps the first-run
 * explanation and this durable panel from drifting apart. */
export function PrivacyPanel() {
  return (
    <div aria-label={i18nT('privacyDisclosure.settingsLabel')}>
      <Card>
        <PrivacyDisclosureSections />
      </Card>

      <Card>
        <CardTitle>
          <SlidersHorizontal className="lucide-inline" aria-hidden="true" />
          {i18nT('privacyDisclosure.controlsTitle')}
        </CardTitle>
        <BeaconToggle />
        {/* Recording is a separate decision from the heartbeat: this one never
            leaves the machine, so it sits below the egress control rather than
            being folded into it. */}
        <MetricRecordingToggle />
        <p className="text-sm text-muted leading-relaxed mt-4 mb-3">
          {i18nT('privacyDisclosure.controlsBody')}
        </p>
        <PrivacyCommandList />
      </Card>
    </div>
  )
}
