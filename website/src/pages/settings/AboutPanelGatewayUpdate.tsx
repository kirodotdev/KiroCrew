/**
 * The About panel's Updates card for a gateway install (no Electron shell) and the
 * state behind it: the check, apply, restart and channel-switch state
 * (`useAboutPanelGatewayUpdate`), the card body that offers the update or the check
 * (`AboutPanelGatewayUpdates`), and the apply confirm (`AboutPanelUpdateConfirm`). The
 * controls it places come from `AboutPanelUpdateFlow.tsx`; `AboutPanel.tsx` derives the
 * verdicts it shares with the hero card and passes them in.
 */
import { useState } from 'react'
import { useMutation, type QueryClient } from '@tanstack/react-query'
import { RefreshCw, CheckCircle2, GitBranch, ArrowUp, Package, X, Copy } from 'lucide-react'
import { Btn } from '../../components/ui'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { api, ApiError } from '../../api/client'
import { adoptCheckedAutoUpdate, configReads } from '../../hooks/useGatewayAutoUpdate'
import ErrorNotice from '../../components/ErrorNotice'
import { dirtyWorktreeChanged } from '../../utils/dirtyWorktree'
import { CopyCommand } from '../../components/agentHarness/CopyCommand'
import { i18nT } from '../../i18n/t'
import { fmtRelative } from '../../i18n/format'
import { InAppUpdateFlow, RestartGatewayButton, copyCommand } from './AboutPanelUpdateFlow'

/**
 * Why the GATEWAY update check produced no verdict.
 *
 * Distinct from `updateErrorText` in AboutPanel.tsx, which speaks for the Electron updater's
 * download/install lifecycle. These codes come from `/api/update/check` and mean
 * "the comparison did not happen" — never "you are up to date".
 *
 * An unrecognised code deliberately falls back to the generic reason instead of
 * being dropped: a newer gateway paired with an older bundle must still say the
 * check failed rather than silently render the success line.
 */
const GATEWAY_CHECK_ERROR_KEYS: Record<string, string> = {
  feed_unreachable: 'pages.settings.aboutPanel.update_check_error_feed_unreachable',
  feed_malformed: 'pages.settings.aboutPanel.update_check_error_feed_malformed',
  git_fetch_failed: 'pages.settings.aboutPanel.update_check_error_git_fetch_failed',
  git_read_failed: 'pages.settings.aboutPanel.update_check_error_git_read_failed',
  version_unparseable: 'pages.settings.aboutPanel.update_check_error_version_unparseable',
  // Not failures: this gateway is not the update surface for the install it is
  // running inside. A desktop bundle embeds this same backend, so it reaches this
  // code and must defer to the Electron updater; a container is replaced by
  // pulling a new image.
  managed_by_app: 'pages.settings.aboutPanel.update_check_managed_by_app',
  managed_by_image: 'pages.settings.aboutPanel.update_check_managed_by_image',
}

function gwCheckErrorText(code: string): string {
  const key = GATEWAY_CHECK_ERROR_KEYS[code]
  return i18nT(key || 'pages.settings.aboutPanel.update_check_error_unknown')
}

/**
 * The gateway update flow's state: the last check's verdict, the apply and restart
 * requests, the channel switch and the manual-update command box's copy flags.
 * `AboutPanel` calls it once per render, ahead of the verdicts it derives from this
 * state for the hero card and the Updates card.
 */
export function useAboutPanelGatewayUpdate(queryClient: QueryClient) {
  const [gwChanges, setGwChanges] = useState('')
  const [gwTarget, setGwTarget] = useState('')
  // Display-only sibling of gwTarget, folded to the clean release version on
  // stable. Never fed to InAppUpdateFlow or /api/update/arm.
  const [gwTargetDisplay, setGwTargetDisplay] = useState('')
  const [gwFound, setGwFound] = useState(false)
  // Commit distance from the tracked upstream, straight from the check payload.
  // Only a git checkout ever reports non-zero values; both stay 0 elsewhere.
  const [gwAhead, setGwAhead] = useState(0)
  const [gwBehind, setGwBehind] = useState(0)
  // The honesty trio, straight from /api/update/check.
  //
  // `gwChecked` is what licenses the "you're on the latest version" line. It used
  // to be enough that the request returned 200 — but for a wheel install the
  // backend's check never actually ran, so a check that did nothing reported an
  // out-of-date install as up to date. A 200 is now only a transport success;
  // `checked` is the verdict, and `gwError` names why there is none.
  const [gwChecked, setGwChecked] = useState(false)
  const [gwError, setGwError] = useState('')
  // Why there is no verdict when nothing FAILED: this gateway is not the update
  // surface for the install it runs inside. Separate from `gwError` because
  // rendering a deferral under "Couldn't check for updates" would be its own
  // lie — nothing broke, the update arrives through a different surface.
  const [gwUnavailableReason, setGwUnavailableReason] = useState('')
  // Null = not yet known from a check; the redux status flag AboutPanel.tsx reads carries the
  // same fact for the pre-check case.
  const [gwSelfUpdatable, setGwSelfUpdatable] = useState<boolean | null>(null)
  const [gwChannel, setGwChannel] = useState('')
  // Server-supplied reason for a refused switch; '' means 'no detail, use the generic line'.
  const [gwChannelError, setGwChannelError] = useState('')
  const [gwCommand, setGwCommand] = useState('')
  const [gwCommandCopied, setGwCommandCopied] = useState(false)
  const [gwCommandCopyFailed, setGwCommandCopyFailed] = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)
  const [applyError, setApplyError] = useState('')
  // The apply was refused because the checkout has uncommitted changes: the
  // error then carries guided copy, and a stash command renders beneath it.
  const [applyDirty, setApplyDirty] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const gwCheck = useMutation({
    mutationFn: () => api.checkUpdate(),
    // Where the saved switch's entry stood, so a late answer cannot overwrite a newer read.
    onMutate: () => configReads(queryClient),
    onSuccess: (d, _vars, readsWhenSent) => {
      setGwChanges(d?.changes || '')
      // `latest_version` is the field the gateway actually emits; `version` is
      // read as a fallback only because it is what some older payloads carried.
      const target = d?.latest_version || d?.version
      if (target) setGwTarget(String(target))
      // Same contract as the channel-switch handler below: adopt the
      // display-only folded sibling (empty when the gateway predates it).
      setGwTargetDisplay(typeof d?.latest_version_display === 'string' ? d.latest_version_display : '')
      // Derive availability from the check response itself, not only the redux
      // status flag (which refreshes on a slower WS status push). Otherwise a
      // check that finds an update could still show "You're on the latest
      // version" until the flag catches up.
      setGwFound(d?.update_available === true)
      setGwChecked(d?.check_status === 'succeeded')
      // Adopted unconditionally (0 when absent) so one check's divergence can
      // never survive into the next check's verdict.
      setGwAhead(typeof d?.commits_ahead === 'number' ? d.commits_ahead : 0)
      setGwBehind(typeof d?.commits_behind === 'number' ? d.commits_behind : 0)
      // A DEFERRAL is not a failure: a desktop bundle reporting "the app
      // updates itself" has not malfunctioned, and its reason has its own slot.
      // Only `error_code` may render as an error.
      setGwError(typeof d?.error_code === 'string' ? d.error_code : '')
      setGwUnavailableReason(
        typeof d?.unavailable_reason === 'string' ? d.unavailable_reason : ''
      )
      setGwChannel(typeof d?.channel === 'string' ? d.channel : '')
      setGwCommand(
        typeof d?.remediation?.command === 'string' ? d.remediation.command : ''
      )
      setGwCommandCopied(false)
      setGwCommandCopyFailed(false)
      if (typeof d?.can_apply === 'boolean') setGwSelfUpdatable(d.can_apply)
      // The check read the saved switch too, which another tab or the CLI may
      // have changed: adopt it, unless a save or a read has moved it since.
      adoptCheckedAutoUpdate(queryClient, d, readsWhenSent)
    },
  })
  const gwApply = useMutation({
    mutationFn: () => api.applyUpdate(),
    onSuccess: () => setRestarting(true),
    onError: (e: unknown) => {
      // A real server rejection (e.g. 409 dirty tree, 400) arrives as ApiError
      // with a status code — surface it. A bare network failure means the POST's
      // connection was reset by the gateway restart the update itself triggers;
      // that is the expected success path, not a failure.
      const changed = dirtyWorktreeChanged(e)
      setApplyDirty(changed !== null)
      if (changed !== null) setApplyError(i18nT('pages.settings.aboutPanel.update_refused_dirty_worktree', { changed }))
      else if (e instanceof ApiError) setApplyError(e.message || i18nT('pages.settings.aboutPanel.update_failed'))
      else setRestarting(true)
    },
  })
  // Restart WITHOUT updating: the missing half of the non-self-updatable flow.
  // After the user runs the copied installer command in a terminal, the running
  // gateway is still executing the old code and had no in-app way to reload.
  //
  // Same error shape as gwApply: os.execv kills the connection, so a bare
  // network failure after the POST is the expected path, and only an ApiError
  // (a real server rejection) is a failure worth showing.
  const gwRestart = useMutation({
    mutationFn: () => api.restartGateway(),
    onSuccess: () => setRestarting(true),
    onError: (e: unknown) => {
      setApplyDirty(false)
      if (e instanceof ApiError) setApplyError(e.message || i18nT('pages.settings.aboutPanel.restart_failed'))
      else setRestarting(true)
    },
  })
  // Gateway channel switch (stable | insider | nightly). Unlike the desktop
  // switcher this is a THREE-way control: cli.sh publishes and installs all
  // three lanes, so every one of them is a real destination for a wheel install.
  // Refuses on a git checkout or an externally managed layout (409), which is
  // why the control is only rendered when the backend reported a channel.
  const gwChannelMutation = useMutation({
    mutationFn: (next: string) => api.setUpdateChannel(next),
    onMutate: () => { setGwChannelError(''); return configReads(queryClient) },
    onError: (e: unknown) => {
      // The backend's 409s carry the only actionable detail there is ("a git
      // checkout follows its git remote", or the externally-managed guidance
      // naming the real update surface). A bare "Couldn't switch channel" leaves
      // the user with no next step, so surface the server's reason when it gave
      // one and fall back to the generic line when it did not.
      setGwChannelError(e instanceof ApiError ? (e.message || '') : '')
    },
    onSuccess: (d, _vars, readsWhenSent) => {
      // The response is the re-run check against the new channel, so adopt it
      // wholesale rather than leaving the previous lane's verdict on screen.
      //
      // These MUST stay the same field names `gwCheck.onSuccess` reads: both
      // handlers consume the same update-check contract, and when this one was
      // left on the old names a successful switch that FOUND an update wrote
      // `gwFound=false` / `gwChecked=false` and blanked the target — silently
      // discarding the very verdict the switch was made to get.
      if (typeof d?.channel === 'string') setGwChannel(d.channel)
      setGwFound(d?.update_available === true)
      setGwChecked(d?.check_status === 'succeeded')
      setGwAhead(typeof d?.commits_ahead === 'number' ? d.commits_ahead : 0)
      setGwBehind(typeof d?.commits_behind === 'number' ? d.commits_behind : 0)
      // Only `error_code` may render as an error; a deferral is not a failure.
      setGwError(typeof d?.error_code === 'string' ? d.error_code : '')
      setGwCommand(typeof d?.update_command === 'string' ? d.update_command : '')
      setGwCommandCopied(false)
      setGwCommandCopyFailed(false)
      setGwTarget(typeof d?.latest_version === 'string' ? d.latest_version : '')
      setGwTargetDisplay(typeof d?.latest_version_display === 'string' ? d.latest_version_display : '')
      if (typeof d?.can_apply === 'boolean') setGwSelfUpdatable(d.can_apply)
      adoptCheckedAutoUpdate(queryClient, d, readsWhenSent)
    },
  })
  return {
    gwChanges, gwTarget, gwTargetDisplay, gwFound, gwAhead, gwBehind, gwChecked, gwError,
    gwUnavailableReason, gwSelfUpdatable, gwChannel, gwChannelError, gwCommand, gwCommandCopied,
    setGwCommandCopied, gwCommandCopyFailed, setGwCommandCopyFailed, showConfirm, setShowConfirm,
    applyError, setApplyError, applyDirty, restarting, setRestarting, gwCheck, gwApply, gwRestart,
    gwChannelMutation,
  }
}

export type AboutPanelGatewayUpdateState = ReturnType<typeof useAboutPanelGatewayUpdate>

/**
 * The Updates card body for a gateway install (no Electron shell): the update offer or
 * the check, the auto-update switch the panel passes in, and the standing Restart row.
 */
export function AboutPanelGatewayUpdates({
  gwUpdate,
  showUpdate,
  channelMovePending,
  showManualUpdate,
  effectiveGwCanArm,
  effectiveCommand,
  effectiveGwChannel,
  gwChannelMovePending,
  gwManagedByCommand,
  gwStatusLatest,
  gwStatusLatestDisplay,
  gwDiverged,
  gwDivergedText,
  lastCheckedAt,
  checkIntervalSecs,
  botName,
  autoUpdateSwitch,
}: {
  gwUpdate: AboutPanelGatewayUpdateState
  showUpdate: boolean
  channelMovePending: boolean
  showManualUpdate: boolean
  effectiveGwCanArm: boolean
  effectiveCommand: string
  effectiveGwChannel: string
  gwChannelMovePending: boolean
  gwManagedByCommand: boolean
  gwStatusLatest: string
  gwStatusLatestDisplay: string
  gwDiverged: boolean
  gwDivergedText: string
  lastCheckedAt: number | null
  checkIntervalSecs: number
  botName: string
  autoUpdateSwitch: React.ReactNode
}) {
  const {
    gwChanges, gwTarget, gwTargetDisplay, gwChecked, gwError, gwUnavailableReason, gwCommandCopied,
    setGwCommandCopied, gwCommandCopyFailed, setGwCommandCopyFailed, setShowConfirm, applyError,
    setApplyError, restarting, setRestarting, gwCheck, gwRestart,
  } = gwUpdate
  return (
    <div className="flex flex-col gap-2.5">
      {(showUpdate || channelMovePending) ? (
        <>
          {showUpdate && (
            <p className="text-sm text-muted flex items-center gap-1.5">
              <ArrowUp size={13} className="lucide-inline text-accent" /> {i18nT('pages.settings.aboutPanel.a_new_version')}{(gwTargetDisplay || gwTarget) ? ` (v${gwTargetDisplay || gwTarget})` : ''} {i18nT('pages.settings.aboutPanel.is_available')}
            </p>
          )}
          {showManualUpdate ? (
            effectiveGwCanArm ? (
              <InAppUpdateFlow
                version={gwTargetDisplay || gwStatusLatestDisplay || gwTarget || gwStatusLatest}
                manualCommand={effectiveCommand || ''}
                isChannelMove={gwChannelMovePending}
              />
            ) : gwManagedByCommand ? (
              // A policy-pinned command provider owns this update, and a
              // check-only pin has no in-app apply. The installer copy
              // below would tell the user to run the exact mechanism the
              // policy exists to bypass — and they might actually do it,
              // fighting the managed install.
              <p className="text-[13px] text-muted" data-testid="policy-managed-update-note">
                {i18nT('pages.settings.aboutPanel.updates_managed_by_policy')}
              </p>
            ) : (
            // This install cannot replace its own code (a `cli.sh` wheel
            // install, not a git checkout), so there is no Update button to
            // offer — pressing one would 409. Show the command that does
            // work instead. The channel is spelled out in it deliberately:
            // the installer defaults to stable and never reads the channel
            // file, so a bare re-run would silently move this install to a
            // different lane.
            <div className="flex flex-col gap-2" data-testid="manual-update-instructions">
              <p className="text-[13px] text-muted">
                {effectiveGwChannel
                  ? i18nT('pages.settings.aboutPanel.this_install_updates_by_re_running_the_installer_channel', { channel: effectiveGwChannel })
                  : i18nT('pages.settings.aboutPanel.this_install_updates_by_re_running_the_installer')}
              </p>
              {effectiveCommand && (
                <>
                  <div className="p-2.5 bg-bg rounded-lg border border-border font-mono text-[12px] text-text break-all"
                    data-testid="manual-update-command">
                    {effectiveCommand}
                  </div>
                  <div className="flex items-center gap-2 flex-wrap">
                    {/* copyToClipboard, not navigator.clipboard directly: the
                        Clipboard API is unavailable on a plain-HTTP remote
                        gateway — exactly the deployment this command targets —
                        and flipping the label regardless would tell the user
                        their shell paste is ready when the clipboard still
                        holds something else. Await it, then confirm. */}
                    <Btn onClick={() => copyCommand(effectiveCommand, setGwCommandCopied, setGwCommandCopyFailed)}>
                      <Copy size={13} className="lucide-inline" /> {gwCommandCopied
                        ? i18nT('pages.settings.aboutPanel.copied')
                        : i18nT('pages.settings.aboutPanel.copy_command')}
                    </Btn>
                    {/* The step the flow used to end without. The installer
                        replaced the code on disk; this process is still
                        running the old version until it re-execs. Primary
                        once the command has been copied, because that is
                        when restarting is the actual next action. */}
                    <RestartGatewayButton
                      primary={gwCommandCopied}
                      pending={gwRestart.isPending}
                      restarting={restarting}
                      onConfirm={() => { setApplyError(''); gwRestart.mutate() }}
                      testId="gateway-restart"
                    />
                  </div>
                  {/* Below the Copy/Restart row, not inside it: the hand-off
                      link counts as a third action there
                      (max-two-buttons-per-row). askAgent on: the command is
                      still on screen above. */}
                  {gwCommandCopyFailed && (
                    <ErrorNotice
                      variant="inline"
                      message={i18nT('pages.settings.aboutPanel.copy_failed_select_the_command_and_copy_it_manually')}
                      askAgent
                      testId="manual-update-copy-error"
                    />
                  )}
                  <p className="text-[12px] text-muted">
                    {i18nT('pages.settings.aboutPanel.restart_after_installer_note')}
                  </p>
                  {/* askAgent on: a refused restart leaves nothing unsaved —
                      the installer already ran in the user's terminal. */}
                  <ErrorNotice message={applyError} askAgent testId="manual-update-restart-error" />
                </>
              )}
            </div>
            )
          ) : (
            <div>
              <Btn primary onClick={() => { if (!gwChanges) gwCheck.mutate(); setApplyError(''); setRestarting(false); setShowConfirm(true) }}>
                {/* Whole-sentence keys, not "Update" + " to vX": the version
                    does not follow the verb in every language. */}
                <ArrowUp size={13} className="lucide-inline" /> {gwTarget
                  ? i18nT('pages.settings.aboutPanel.update_to_version', { version: gwTarget })
                  : i18nT('pages.settings.aboutPanel.update_now')}
              </Btn>
            </div>
          )}
        </>
      ) : (
        <>
          <p className="text-sm text-muted">
            {lastCheckedAt
              ? i18nT('pages.settings.aboutPanel.checks_for_updates_with_timing', {
                  name: botName || 'Kiro Crew',
                  timing: i18nT('pages.settings.aboutPanel.last_checked_ago_next_check_in', {
                    ago: fmtRelative(lastCheckedAt * 1000),
                    // Clamp: after machine sleep the scheduled check can be
                    // past-due, and an unclamped value renders a future event
                    // in the past tense ("next automatic check 8 hours ago").
                    next: fmtRelative(Math.max((lastCheckedAt + checkIntervalSecs) * 1000, Date.now())),
                  }),
                })
              : <>{botName || 'Kiro Crew'} {i18nT('pages.settings.aboutPanel.checks_for_updates_automatically_you_can_also_ch')}</>}
          </p>
          <div>
            <Btn onClick={() => gwCheck.mutate()} disabled={gwCheck.isPending}>
              <RefreshCw size={13} className={`lucide-inline ${gwCheck.isPending ? 'animate-spin' : ''}`} /> {i18nT('pages.settings.aboutPanel.check_for_updates')}
            </Btn>
          </div>
          {/* A 200 is transport success, NOT a verdict: `checked` is the
              verdict. Gating the success line on it is the fix for the
              original bug, where a wheel install's no-op check rendered
              "you're on the latest version" while being two releases
              behind. An unrecognised error code still lands here (in the
              error branch), never in the success branch. */}
          {gwCheck.isSuccess && gwChecked && !gwError && !gwUnavailableReason && !showUpdate && (
            gwDiverged ? (
              /* The third verdict: diverged. "No update available" here is
                 the no-auto-apply safety property doing its job, not
                 currency, so saying "latest version" would be false in the
                 other direction. Counts plus the manual next step, and
                 deliberately NO apply button: this panel's Update button
                 POSTs /api/update, whose git path is a bare `git pull` — an
                 unrequested merge into the diverged branch (the unattended
                 auto-update path is the `git reset --hard` that would
                 discard the commits outright; both are wrong here). */
              <span className="text-warn text-[13px] flex items-start gap-1.5" role="status" data-testid="diverged">
                <GitBranch size={13} className="lucide-inline shrink-0 mt-0.5" aria-hidden />
                <span>{gwDivergedText}</span>
              </span>
            ) : (
              <span className="text-ok text-[13px] flex items-center gap-1.5" data-testid="up-to-date"><CheckCircle2 size={13} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.you_re_on_the_latest_version')}</span>
            )
          )}
          {gwCheck.isSuccess && !!gwUnavailableReason && (
            <span className="text-muted text-[13px] flex items-center gap-1.5" data-testid="check-not-applicable"><Package size={13} className="lucide-inline" /> {gwCheckErrorText(gwUnavailableReason)}</span>
          )}
          {/* askAgent on: a failed read; the Check button above is the retry. */}
          {(gwCheck.isError || (gwCheck.isSuccess && !!gwError)) && (
            <ErrorNotice
              variant="inline"
              message={`${i18nT('pages.settings.aboutPanel.couldn_t_check_for_updates_2')}${gwError ? `: ${gwCheckErrorText(gwError)}` : ''}`}
              askAgent
              testId="check-failed"
            />
          )}
        </>
      )}
      {autoUpdateSwitch}

      {/* Standing maintenance action, NOT gated on an update being
          available. Restarting is how this install picks up code that
          already changed on disk — a re-run installer, a `pip install -e .`
          during development, a config change that needs a fresh process —
          and none of those imply a pending release. Keeping it out of the
          update branch is the difference between a control that is there
          when needed and one that only appears when an update happens to
          be offered.

          Hidden while the manual-update card is on screen: that card
          already renders a Restart button wired to the same mutation, and
          two identical buttons a few rows apart read as two different
          actions. */}
      {!showManualUpdate && (
        <div className="flex items-center justify-between gap-3 pt-2.5 border-t border-border"
          data-testid="gateway-restart-row">
          {/* One label, not a title + note pair: the button already names the
              action, so a "Restart gateway" heading beside a "Restart gateway"
              button says the same thing twice. This mirrors the auto-update
              row above — a single explanatory label plus its control — and
              spends the space on the consequence instead. */}
          <span className="text-sm text-muted">{i18nT('pages.settings.aboutPanel.restart_row_note')}</span>
          <div className="shrink-0">
            <RestartGatewayButton
              pending={gwRestart.isPending}
              restarting={restarting}
              onConfirm={() => { setApplyError(''); gwRestart.mutate() }}
              testId="gateway-restart-standing"
            />
          </div>
        </div>
      )}
      {/* The in-flow button renders its own error inside the update card;
          this one has no card, so the shared error surfaces here. Gated on
          the SAME condition as the row itself -- keying it on `showUpdate`
          instead left a self-updatable install with a pending update showing
          the row while swallowing its error, so a rejected restart gave the
          user no feedback whatsoever. */}
      {/* askAgent on: a refused restart of a standing action; nothing on
          this card is a draft. */}
      {!showManualUpdate && (
        <ErrorNotice message={applyError} askAgent testId="gateway-restart-error" />
      )}
    </div>
  )
}

/** The apply confirm a gateway install opens: the changelog, then the update itself. */
export function AboutPanelUpdateConfirm({
  gwUpdate,
  gwDiverged,
  gwDivergedText,
}: {
  gwUpdate: AboutPanelGatewayUpdateState
  gwDiverged: boolean
  gwDivergedText: string
}) {
  const {
    gwChanges, gwTarget, setShowConfirm, applyError, applyDirty, restarting, gwCheck, gwApply,
  } = gwUpdate
  return (
    // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions -- backdrop click-to-dismiss is a supplementary mouse affordance; the keyboard path is the document-level Escape listener in AboutPanel.tsx plus the Close button, and making the dialog surface itself a tab stop would put a stop in front of its own content
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/60 backdrop-blur-xs animate-rise"
         role="dialog" aria-modal="true" aria-label={i18nT('pages.settings.aboutPanel.update')}
         onClick={() => { if (!gwApply.isPending && !restarting) setShowConfirm(false) }}>
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions -- the only handler is a propagation guard keeping a click inside the panel from reaching the backdrop's dismiss; it performs no action, so there is no keyboard equivalent to provide */}
      <div role="document" className="bg-card border border-border rounded-xl p-6 max-w-md w-full mx-4 shadow-xl" onClick={e => e.stopPropagation()}>
        <div className="flex justify-between items-center mb-3">
          {/* Whole-sentence key, not "Update" + " to vX": the version does
              not follow the verb in every language. Diverged drops the
              version — there is nothing to update to. */}
          <div className="text-sm font-bold text-text-strong flex items-center gap-1.5"><Package size={15} className="lucide-inline" /> {gwTarget && !gwDiverged
            ? i18nT('pages.settings.aboutPanel.update_to_version', { version: gwTarget })
            : i18nT('pages.settings.aboutPanel.update')}</div>
          <button aria-label={i18nT('pages.settings.aboutPanel.close')} className="text-muted hover:text-text cursor-pointer bg-transparent border-none disabled:opacity-40 disabled:cursor-default" disabled={gwApply.isPending || restarting} onClick={() => { if (!gwApply.isPending && !restarting) setShowConfirm(false) }}><X size={15} /></button>
        </div>
        {gwCheck.isPending ? (
          <div className="text-[13px] text-muted flex items-center gap-1.5 mb-4"><RefreshCw size={13} className="lucide-inline animate-spin" /> {i18nT('pages.settings.aboutPanel.loading_changelog')}</div>
        ) : gwDiverged ? (
          /* The modal opened from a STALE update flag, and the check it
             fired came back diverged: there is nothing to apply, and the
             backend path behind the button below is a bare `git pull` — a
             silent merge into the user's branch. Say why instead. */
          <p className="text-[13px] text-warn mb-4" data-testid="diverged-modal">{gwDivergedText}</p>
        ) : gwChanges ? (
          <>
            <div className="text-[12px] font-medium text-muted uppercase tracking-wider mb-2">{i18nT('pages.settings.aboutPanel.what_s_new')}</div>
            <div className="p-3 bg-bg rounded-lg border border-border max-h-56 overflow-y-auto mb-4 text-[13px] text-text"><MarkdownRenderer content={gwChanges} /></div>
          </>
        ) : (
          <p className="text-[13px] text-muted mb-4">{i18nT('pages.settings.aboutPanel.a_newer_version_is_available')}</p>
        )}
        {/* The restart warning describes the apply below; a diverged modal
            offers no apply, so warning about its restart would keep the
            update promise the body just withdrew. */}
        {!gwDiverged && (
          <p className="text-[12px] text-muted mb-3">{i18nT('pages.settings.aboutPanel.updating_restarts_the_gateway_active_sessions_wi')}</p>
        )}
        {/* askAgent on: the apply's inputs are all server-side (a 409 dirty
            tree, a 400) and this dialog holds no draft. `ErrorNotice` exposes
            no onHandoff, but the hand-off navigates away and unmounts this
            panel — modal included — so nothing is left sitting over the chat. */}
        <ErrorNotice className="mb-3" message={applyError} askAgent testId="update-apply-error" />
        {applyDirty && applyError && (
          <div className="mb-3" data-testid="update-dirty-stash-command">
            <CopyCommand><code>git stash push -u -m "before kirocrew update"</code></CopyCommand>
          </div>
        )}
        {restarting ? (
          <div className="text-[13px] text-accent flex items-center justify-center gap-1.5 py-2" role="status">
            <RefreshCw size={13} className="lucide-inline animate-spin" /> {i18nT('pages.settings.aboutPanel.updating_gateway_restarting')}
          </div>
        ) : gwCheck.isPending ? (
          /* The pre-apply check is still running: its answer may be
             "diverged", so an enabled apply button here is a race the user
             can win against their own safety check. Hold the action until
             the verdict lands (the server enforces the same precondition,
             so this is honesty, not the only line of defense). */
          <Btn className="w-full justify-center" disabled>
            <RefreshCw size={13} className="lucide-inline animate-spin" /> {i18nT('pages.settings.aboutPanel.checking_for_updates')}
          </Btn>
        ) : gwDiverged ? (
          <Btn className="w-full justify-center" data-testid="diverged-modal-close" onClick={() => setShowConfirm(false)}>
            {i18nT('pages.settings.aboutPanel.close')}
          </Btn>
        ) : (
          <Btn primary className="w-full justify-center" disabled={gwApply.isPending} onClick={() => gwApply.mutate()}>
            {gwApply.isPending ? <><RefreshCw size={13} className="lucide-inline animate-spin" /> {i18nT('pages.settings.aboutPanel.updating')}</> : i18nT('pages.settings.aboutPanel.update_now')}
          </Btn>
        )}
      </div>
    </div>
  )
}
