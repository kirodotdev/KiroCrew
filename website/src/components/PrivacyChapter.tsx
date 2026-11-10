import { useContext, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Btn, SendBtn } from './ui'
import ErrorNotice from './ErrorNotice'
import { api } from '../api/client'
import { apiErrorCode } from '../api/apiError'
import { useDocumentImeLatch } from '../hooks/useImeGuard'
import { useLocalCrewHidden } from './overlayRoot'
import OnboardingChapterShell, { OnboardingShellContext } from './OnboardingChapterShell'
import { PrivacyCommandList, PrivacyDisclosureSections } from './PrivacyDisclosure'
import { useUsageDataState, type UsageOffReason } from './privacyNotice'
import type { BeaconStatus } from './PrivacyDisclosure'

import { i18nT } from '../i18n/t'

// Indexed `as const` maps of full literal keys, so check-i18n-keys.mjs can verify
// every key these lists render exists in the catalog.
const COLLECTED_KEYS = [
  'components.privacyChapter.collectedTimings',
  'components.privacyChapter.collectedFeatures',
  'components.privacyChapter.collectedVersions',
  'components.privacyChapter.collectedInstallId',
] as const
const NEVER_COLLECTED_KEYS = [
  'components.privacyChapter.neverPrompts',
  'components.privacyChapter.neverFiles',
  'components.privacyChapter.neverCredentials',
  'components.privacyChapter.neverIdentity',
] as const
const OFF_REASON_KEY = {
  governance: 'components.privacyChapter.offReasonGovernance',
  do_not_track: 'components.privacyChapter.offReasonDoNotTrack',
  env: 'components.privacyChapter.offReasonEnv',
  config: 'components.privacyChapter.offReasonConfig',
} as const satisfies Record<UsageOffReason, string>
// "Where it goes" while local recording is on: whether metrics also leave the
// machine, and whether the heartbeat is sent, are separate facts.
const WHERE_BODY_KEY = {
  local: {
    heartbeat: 'components.privacyChapter.whereBody',
    none: 'components.privacyChapter.whereBodyNoHeartbeat',
  },
  export: {
    heartbeat: 'components.privacyChapter.whereBodyExport',
    none: 'components.privacyChapter.whereBodyExportNoHeartbeat',
  },
} as const

/**
 * Privacy — the second first-run chapter, between Import setup and Customize.
 *
 * A SINGLE-SCREEN chapter, so its eyebrow is the chapter name alone: there is no
 * "x of y" counter to show when x and y are both 1, and a counter that never
 * moves reads like a stalled wizard.
 *
 * MANDATORY, which here means exactly one thing — there is no way past it but
 * forward:
 *   - no "Skip all" (the shell renders none when no handler is passed),
 *   - no Escape-to-dismiss (the Tab trap below deliberately omits it),
 *   - and the paths that abandon the flows on EITHER side still route through
 *     it: skipping Import setup lands here, and "Skip all" from Import setup
 *     shows this screen before the user reaches the product (see
 *     shell/boot/firstRun.tsx).
 *
 * The screen says usage data is on, what is and is never collected, and where it
 * goes, then asks for one of two answers:
 *   - "Keep on" records only that the user was told (`onContinue`, which
 *     persists `dashboard.privacy_acked`, plus `dashboard.privacy_ack_version`
 *     when the text shown was the one that says local recording is on).
 *   - "Turn off" first writes `telemetry.enabled = false` and
 *     `telemetry.beacon_enabled = false` through the config route, which stops
 *     the recorder at once, then re-reads both effective states, and only when
 *     both are really off records the ack. An env var or a `config.local.json`
 *     entry that keeps either on leaves the chapter open and says so.
 * A failed write leaves the chapter open with an error and marks nothing as seen.
 *
 * The text follows what is actually on. With local recording on it lists what is
 * recorded. With only the heartbeat on (recording is off by default today) it
 * says so, leaves the recording list out, and does NOT record the notice
 * version: that version means "this user was told recording is on".
 *
 * When usage data is already off for a reason this screen cannot change (an env
 * var, `DO_NOT_TRACK`, an administrator's policy) or was turned off before, the
 * same text runs under "Usage data is off on this machine" with the reason and
 * one "Continue".
 *
 * The detail sections below are the SAME ones Settings > Privacy renders, so the
 * first-run explanation and the durable panel cannot drift.
 */
export default function PrivacyChapter({
  open,
  onContinue,
  onAskAgent,
}: {
  open: boolean
  /**
   * Hides the chapter WITHOUT recording a choice when the user hands an error to
   * the agent: the hand-off opens a chat this mandatory overlay would cover. The
   * chapter returns at the next dashboard load. Unset, no hand-off is offered.
   */
  onAskAgent?: () => void
  /**
   * Persists the ack; `noticeVersion` says whether the current notice version is
   * recorded too. Rejects when the write fails; the chapter then stays open.
   */
  onContinue: (ack: { noticeVersion: boolean }) => Promise<void> | void
}) {
  const qc = useQueryClient()
  const usage = useUsageDataState(open)
  // An unreadable status still shows the "on" text: Turn off works without it,
  // and saying "off" when that cannot be proven would be the worse claim.
  const showOff = usage.status === 'ready' && !usage.on
  // Heartbeat on, local recording off: say only what is true.
  const heartbeatOnly = usage.status === 'ready' && usage.on && !usage.recordingOn
  // The status could not be read: say nothing about what is on.
  const statusUnknown = usage.status === 'error'
  // The buttons wait for the status: a choice made before the screen can say
  // what is on would be a choice about a statement the screen never made.
  const [busy, setBusy] = useState<'keep' | 'off' | null>(null)
  // `pinned` names what still holds usage data on, read back from the gateway.
  const [failure, setFailure] = useState<
    | { kind: 'save' }
    | { kind: 'unconfirmed' }
    | { kind: 'pinned'; where: 'env'; envVar: string }
    | { kind: 'pinned'; where: 'overlay' | 'unknown' }
    | null
  >(null)

  const choose = async (choice: 'keep' | 'off') => {
    setBusy(choice)
    setFailure(null)
    try {
      if (choice === 'off') {
        // Off keys FIRST: a user who said no must not be left on because the
        // ack write landed and the off write did not.
        // A key set in config.local.json is refused with 409
        // `config_overlay_owned`. Send the other write anyway, then let the
        // read-back below name the overlay instead of a generic save error.
        const writeOff = (path: string) =>
          api.patchConfig(path, false).catch((err: unknown) => {
            if (apiErrorCode(err) !== 'config_overlay_owned') throw err
          })
        await writeOff('telemetry.enabled')
        await writeOff('telemetry.beacon_enabled')
        // The writes land in config.json; an env var or config.local.json can
        // still hold either switch on. Ask the gateway what is in force before
        // telling the user it is off.
        const [beacon, collection] = await Promise.all([
          api.beaconStatus() as Promise<BeaconStatus>,
          api.collectionStatus() as Promise<{
            enabled?: boolean
            env_pinned?: boolean
            env_var?: string
            overlay_override?: boolean
          }>,
        ])
        if (collection?.enabled || beacon?.enabled) {
          // Name the setting, so the user knows exactly what to change.
          if (collection?.enabled && collection.env_pinned && collection.env_var) {
            setFailure({ kind: 'pinned', where: 'env', envVar: collection.env_var })
          } else if ((collection?.enabled && collection.overlay_override) || (beacon?.enabled && beacon.overlay_override)) {
            setFailure({ kind: 'pinned', where: 'overlay' })
          } else {
            setFailure({ kind: 'pinned', where: 'unknown' })
          }
          return
        }
      }
      // One rule for both buttons: the version means "told that local recording
      // is on", so it is recorded only when the text shown said so.
      await onContinue({ noticeVersion: usage.status === 'ready' && usage.recordingOn })
    } catch {
      // Keep writes only the ack, so its failure changed nothing. A Turn off
      // failure can land after one write, both, or an overlay refusal, so it
      // claims neither "off" nor "nothing changed".
      setFailure(choice === 'off' ? { kind: 'unconfirmed' } : { kind: 'save' })
    } finally {
      setBusy(null)
      if (choice === 'off') {
        qc.invalidateQueries({ queryKey: ['beaconStatus'] })
        qc.invalidateQueries({ queryKey: ['telemetryCollection'] })
        qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
      }
    }
  }
  // The focus trap queries the dialog element. Inside a persistent shell host
  // the dialog is host-owned, so use its ref; standalone we own it locally.
  const shellHost = useContext(OnboardingShellContext)
  const localDialogRef = useRef<HTMLDivElement>(null)
  const dialogRef = shellHost?.dialogRef ?? localDialogRef
  const headingRef = useRef<HTMLHeadingElement>(null)
  // Shared IME latch for the Tab trap below: a Tab that lands during an IME
  // composition (or its post-`compositionend` window) is choosing a candidate,
  // not leaving the field, so the trap must decline it instead of yanking
  // focus and aborting the composition (`useDialogFocusTrap` is the reference
  // consumer of the same seam).
  const imeLatch = useDocumentImeLatch(open)
  // Hidden with the local Crew, this chapter still hears keys on the page
  // (components/overlayRoot), and its Tab trap would swallow Tab on the Crew in
  // front. Read through a ref so a switch does not re-run the effect, which
  // would also move focus back to the heading.
  const crewHidden = useLocalCrewHidden()
  const crewHiddenRef = useRef(crewHidden)
  crewHiddenRef.current = crewHidden

  // Move focus to the heading on open, then trap Tab inside the dialog
  // (website/AGENTS.md modal a11y). Escape is NOT wired: this chapter cannot be
  // dismissed, and a key that silently does nothing is better than one that
  // looks like it should close the screen.
  useEffect(() => {
    if (!open) return
    headingRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (crewHiddenRef.current) return
      if (event.key !== 'Tab') return
      const focusable = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      ).filter(element => element.getAttribute('aria-hidden') !== 'true')
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const activeIndex = focusable.indexOf(document.activeElement as HTMLElement)
      const wrapsBackward = event.shiftKey && activeIndex <= 0
      const wrapsForward = !event.shiftKey && (activeIndex < 0 || activeIndex === focusable.length - 1)
      // A mid-dialog Tab is the browser's to move, so it is also not the
      // trap's to claim — claiming it would consume legitimate navigation
      // inside the post-composition latch window.
      if (!wrapsBackward && !wrapsForward) return
      // A Tab the IME owns must not cycle focus — the user is choosing a
      // candidate, not leaving the field. `claimKey` owns the whole decline;
      // it must run before the preventDefault() and focus move so the IME
      // keeps the key (see its contract in useImeGuard.ts).
      if (!imeLatch.claimKey(event)) return
      event.preventDefault()
      ;(wrapsBackward ? last : first).focus()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
    // shellHost?.sectionSlot: in host mode the content is portaled a pass after
    // open, so re-run once the slot exists to install the trap + initial focus.
  }, [open, dialogRef, shellHost?.sectionSlot, imeLatch])

  if (!open) return null

  const reasonKey = usage.offReason ? OFF_REASON_KEY[usage.offReason] : null
  const handoff = onAskAgent ? ({ askAgent: true, onHandoff: onAskAgent } as const) : {}

  return (
    <OnboardingChapterShell
      ariaLabel={i18nT('components.privacyChapter.title')}
      panelHeadline={i18nT('components.privacyChapter.panelHeadline')}
      panelBody={i18nT('components.privacyChapter.panelBody')}
      panelFootnote={i18nT('components.privacyChapter.panelFootnote')}
      eyebrow={i18nT('components.privacyChapter.eyebrow')}
      dialogRef={dialogRef}
      header={
        <div className="mt-6">
          <h1
            ref={headingRef}
            tabIndex={-1}
            className="text-2xl font-semibold text-text-strong outline-hidden"
          >
            {/* An unread status gets a neutral heading: "on" would be a claim the
                screen cannot back up. */}
            {usage.status === 'error'
              ? i18nT('components.privacyChapter.titleUnknown')
              : showOff
                ? i18nT('components.privacyChapter.titleOff')
                : heartbeatOnly
                  ? i18nT('components.privacyChapter.titleHeartbeatOnly')
                  : i18nT('components.privacyChapter.titleOn')}
          </h1>
          {showOff && reasonKey ? (
            <p className="mt-2 text-sm leading-relaxed text-muted" data-testid="privacy-off-reason">
              {i18nT(reasonKey, { envVar: usage.envVar })}
            </p>
          ) : (
            <p className="mt-2 text-sm leading-relaxed text-muted">
              {statusUnknown
                ? i18nT('components.privacyChapter.introUnknown')
                : heartbeatOnly
                  ? i18nT('components.privacyChapter.introHeartbeatOnly')
                  : i18nT('components.privacyChapter.intro')}
            </p>
          )}
        </div>
      }
      footer={
        <div className="flex flex-col items-end gap-2">
          {/* Nothing to lose: no draft on this screen, the stored choice is
              unchanged or already off, and the buttons stay enabled for a retry.
              The hand-off hides the chapter (no choice recorded), since this
              overlay would otherwise cover the chat it opens. */}
          <ErrorNotice
            variant="inline"
            {...handoff}
            message={
              failure?.kind === 'pinned'
                ? failure.where === 'env'
                  ? i18nT('components.privacyChapter.turnOffPinnedEnv', { envVar: failure.envVar })
                  : failure.where === 'overlay'
                    ? i18nT('components.privacyChapter.turnOffPinnedOverlay')
                    : i18nT('components.privacyChapter.turnOffPinned')
                : failure?.kind === 'save'
                  ? i18nT('components.privacyChapter.saveFailed')
                  : failure?.kind === 'unconfirmed'
                    ? i18nT('components.privacyChapter.turnOffUnconfirmed')
                    : null
            }
          />
          {!showOff && (
            <p className="text-[12px] text-muted text-right" data-testid="privacy-turn-off-scope">
              {heartbeatOnly
                ? i18nT('components.privacyChapter.turnOffScopeHeartbeatOnly')
                : i18nT('components.privacyChapter.turnOffScope')}
            </p>
          )}
          <div className="flex flex-wrap items-center justify-end gap-2">
            {showOff ? (
              <SendBtn type="button" className="focus-ring-accent" onClick={() => void choose('keep')} disabled={busy !== null || usage.status === 'loading'}>
                {i18nT('components.privacyChapter.continue')}
              </SendBtn>
            ) : (
              <>
                <SendBtn type="button" className="focus-ring-accent" onClick={() => void choose('keep')} disabled={busy !== null || usage.status === 'loading'}>
                  {/* "Keep on" would refer to a state the screen just said it
                      could not read. This button records only that the screen
                      was shown, and its label says it changes nothing. */}
                  {statusUnknown
                    ? i18nT('components.privacyChapter.continueUnchanged')
                    : i18nT('components.privacyChapter.keepOn')}
                </SendBtn>
                <Btn
                  type="button"
                  className="min-h-9 px-4 text-sm"
                  onClick={() => void choose('off')}
                  disabled={busy !== null || usage.status === 'loading'}
                >
                  {i18nT('components.privacyChapter.turnOff')}
                </Btn>
              </>
            )}
          </div>
        </div>
      }
    >
      <div className="flex flex-col gap-5">
        {usage.status === 'error' && (
          // The status could not be read, so this screen cannot say which parts
          // are on. The text below is the full disclosure, which over-states
          // rather than under-states; Turn off works either way.
          <ErrorNotice
            variant="inline"
            {...handoff}
            message={i18nT('components.privacyChapter.statusUnavailable')}
          />
        )}
        {!heartbeatOnly && !showOff && (
          <section>
            <h2 className="text-sm font-semibold tracking-tight text-text-strong mb-1.5">
              {i18nT('components.privacyChapter.collectedTitle')}
            </h2>
            <ul className="list-disc pl-5 text-sm leading-relaxed text-muted">
              {COLLECTED_KEYS.map(key => <li key={key}>{i18nT(key)}</li>)}
            </ul>
          </section>
        )}
        <section>
          <h2 className="text-sm font-semibold tracking-tight text-text-strong mb-1.5">
            {i18nT('components.privacyChapter.neverTitle')}
          </h2>
          <ul className="list-disc pl-5 text-sm leading-relaxed text-muted">
            {NEVER_COLLECTED_KEYS.map(key => <li key={key}>{i18nT(key)}</li>)}
          </ul>
        </section>
        <section>
          <h2 className="text-sm font-semibold tracking-tight text-text-strong mb-1.5">
            {i18nT('components.privacyChapter.whereTitle')}
          </h2>
          <p className="text-sm leading-relaxed text-muted">
            {statusUnknown
              ? i18nT('components.privacyChapter.whereBodyUnknown')
              : showOff
                ? i18nT('components.privacyChapter.whereBodyOff')
                : heartbeatOnly
                  ? i18nT('components.privacyChapter.whereBodyHeartbeatOnly')
                  : i18nT(WHERE_BODY_KEY[usage.exportOn ? 'export' : 'local'][usage.heartbeatOn ? 'heartbeat' : 'none'])}
          </p>
        </section>
        {/* A pin this screen cannot lift (an env var, DO_NOT_TRACK, a policy)
            also keeps Settings from changing it, so do not point there. */}
        {(!showOff || usage.offReason === 'config') && (
          <p className="text-sm leading-relaxed text-muted" data-testid="privacy-change-later">
            {i18nT('components.privacyChapter.changeLater')}
          </p>
        )}
      </div>
      <div className="mt-6 border-t border-border pt-5">
        {showOff && (
          // The sections below describe what WOULD be sent; say plainly what
          // stopped, and that the local usage and context records continue.
          <p className="mb-4 text-sm font-medium text-text" data-testid="privacy-details-off">
            {i18nT('components.privacyChapter.detailsOffNote')}
          </p>
        )}
        <PrivacyDisclosureSections />
        <p className="mt-5 mb-3 text-sm leading-relaxed text-muted">
          {i18nT('privacyDisclosure.controlsBody')}
        </p>
        <PrivacyCommandList />
      </div>
    </OnboardingChapterShell>
  )
}
