/**
 * What the Dev Fleet page shows between its header and the worktree table: the
 * how-to line, fleet totals, the inferred main checkout, the Switch back banner,
 * the restart and row-action failures, the host capability notes, the stat
 * cards and the disk-usage failure.
 */
import { type CSSProperties, type RefObject, type Dispatch, type SetStateAction } from 'react'
import { Btn, StatCard } from '../components/ui'
import Clickable from '../components/Clickable'
import ErrorNotice from '../components/ErrorNotice'
import { Server, X, Undo2, Info, AlertTriangle } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { fmtBytes } from '../i18n/format'
import { persistDismissedUndoKey, type Worktree, type UndoTarget, type FleetData } from './devFleetPageModel'
import { iconLabel } from './DevFleetPageRow'

/** The notices between the page header and the worktree table. */
export function DevFleetPageNotices({
  fleet, diskFailed, diskError, actionError, actionErrorRef, dismissActionError, gatewayError,
  setGatewayError, setDismissedUndoKey, gatewayMutating, makeLive, wts, running, needsProv, undoTarget,
  undoCurrent, undoKey, showUndoBanner, podsAvailable, podsReason, gatewayReason, servingReason, noFleet,
  diskGb,
}: {
  fleet: FleetData | undefined
  diskFailed: boolean
  diskError: Error | null
  actionError: string | null
  actionErrorRef: RefObject<HTMLDivElement>
  dismissActionError: () => void
  gatewayError: string | null
  setGatewayError: Dispatch<SetStateAction<string | null>>
  setDismissedUndoKey: Dispatch<SetStateAction<string | null>>
  gatewayMutating: boolean
  makeLive: (w: Worktree | UndoTarget, opts?: { undo?: boolean }) => Promise<void>
  wts: Worktree[]
  running: number
  needsProv: number
  undoTarget: UndoTarget | null
  undoCurrent: Worktree | null
  undoKey: string | null
  showUndoBanner: boolean
  podsAvailable: boolean
  podsReason: string | null
  gatewayReason: string | null
  servingReason: string | null
  noFleet: boolean
  diskGb: string
}) {
  return (
    <>
      {/* The how-to describes row actions; with no readable fleet there are
          no rows, and instructions for absent controls read as a broken page. */}
      {!noFleet && (
      <p className="text-[12.5px] text-muted leading-relaxed mt-3 mb-1">
        {i18nT('pages.devFleetPage.each_row_below_is_a_git_worktree_discovered_from')}{' '}
        <span className="text-text-strong">{i18nT('pages.devFleetPage.pull_build')}</span> {i18nT('pages.devFleetPage.on_the_main_row_to_fast_forward_it_from_origin_a')} <span className="text-text-strong">{i18nT('pages.devFleetPage.pod_2')}</span> {i18nT('pages.devFleetPage.boots_any_worktree_as_an_isolated_throwaway_gate')}{' '}
        <span className="text-text-strong">{i18nT('pages.devFleetPage.rebase')}</span> {i18nT('pages.devFleetPage.moves_a_feature_branch_onto_the_latest_main_and')}{' '}
        <span className="text-text-strong">{i18nT('pages.devFleetPage.prune')}</span> {i18nT('pages.devFleetPage.safely_removes_worktrees_whose_pr_has_already_me')}
      </p>
      )}
      {/* Fleet-level totals: worktree disk, pod-home disk, and orphan
          count — so "this needs cleaning" is legible where the operator
          already is. Each figure renders only when the host could
          measure it; the whole strip is hidden when none are present. */}
      {!noFleet && fleet?.fleet_totals && (
        fleet.fleet_totals.pod_home_bytes != null ||
        (fleet.fleet_totals.orphan_pods != null && fleet.fleet_totals.orphan_pods > 0)
      ) ? (
        <div
          data-testid="fleet-totals"
          className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-2 text-[12px] leading-relaxed text-muted"
          style={{ fontVariantNumeric: 'tabular-nums' } as CSSProperties}
        >
          {/* Worktree disk is intentionally absent here: the stat cards
              below already show it, sourced from the async `/disk`
              endpoint. Repeating it from a second measurement would give
              one label two numbers. */}
          {fleet.fleet_totals.pod_home_bytes != null ? (
            <span title={i18nT('pages.devFleetPage.total_disk_used_by_running_pod_homes')}>
              <Server size={12} className="lucide-inline" /> <span className="text-text-strong">{i18nT('pages.devFleetPage.pod_home_disk', { value: fmtBytes(fleet.fleet_totals.pod_home_bytes) })}</span>
            </span>
          ) : null}
          {fleet.fleet_totals.orphan_pods != null && fleet.fleet_totals.orphan_pods > 0 ? (
            <span title={i18nT('pages.devFleetPage.pod_homes_left_on_disk_with_no_live_pod')} style={{ color: 'var(--warn)' } as CSSProperties}>
              <AlertTriangle size={12} className="lucide-inline" /> {i18nT('pages.devFleetPage.orphan_pods_label', { value: fleet.fleet_totals.orphan_pods })}
            </span>
          ) : null}
        </div>
      ) : null}
      {!noFleet && fleet?.main_repo_inferred && fleet.main_repo && (
        <div
          role="note"
          data-testid="inferred-main-checkout"
          className="flex items-center gap-2 mt-2 text-[12px] leading-relaxed text-text-strong"
        >
          <Info size={13} className="lucide-inline shrink-0" />
          <span>{i18nT('pages.devFleetPage.the_primary_checkout_this_fleet_is_discovered_fr')}:</span>
          <code className="min-w-0 break-all rounded bg-bg-elevated px-1.5 py-0.5 text-text-strong select-text">{fleet.main_repo}</code>
        </div>
      )}
      {showUndoBanner && undoTarget && undoCurrent && (
        <div
          role="status"
          aria-live="polite"
          data-testid="make-live-undo-banner"
          className="flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-3 rounded-md border border-ok/40 bg-ok-subtle px-3 py-2.5 mt-3 text-[12.5px] leading-relaxed text-text-strong"
        >
          <div className="flex min-w-0 flex-1 items-center gap-2">
            <Undo2 className="lucide-inline h-4 w-4 shrink-0 text-ok" aria-hidden />
            <span className="min-w-0 break-words">
              {i18nT('pages.devFleetPage.live_checkout_changed_to_name', { name: undoCurrent.name })}
            </span>
          </div>
          <div className="flex items-center gap-2 self-end sm:self-auto">
            <Btn onClick={() => makeLive(undoTarget, { undo: true })} disabled={gatewayMutating}>
              {iconLabel(<Undo2 className="lucide-inline h-3.5 w-3.5" aria-hidden />, i18nT('pages.devFleetPage.undo_to_name', { name: undoTarget.name }))}
            </Btn>
            <Clickable
              aria-label={i18nT('pages.devFleetPage.dismiss')}
              onClick={() => {
                setDismissedUndoKey(undoKey)
                persistDismissedUndoKey(undoKey)
              }}
              className="inline-flex h-8 w-8 items-center justify-center rounded-md text-muted hover:bg-bg-hover hover:text-text"
            >
              <X className="lucide-inline h-4 w-4" aria-hidden />
            </Clickable>
          </div>
        </div>
      )}
      {/* Restart / make-live failures. The message can be a pair of
          commands with absolute paths the operator has to run, so it
          stays selectable. Nothing typed on this page — hand-off on. */}
      <ErrorNotice
        message={gatewayError}
        askAgent
        onDismiss={() => setGatewayError(null)}
        className="mt-3 select-text"
        testId="gateway-restart-error"
      />
      {/* Latest failed row action (pod up/down, remove, rebase, prune,
          restart…). Every such site reports through `notify(…, error)`,
          which lands here instead of in the transient toast, so the
          failure stays readable (and selectable) until dismissed. Inputs
          are all server-side, so the hand-off is safe. The restart sites
          both notify AND set gatewayError, so the same text is not shown
          twice. */}
      <div ref={actionErrorRef}>
        <ErrorNotice
          message={actionError && actionError !== gatewayError ? actionError : null}
          askAgent
          onDismiss={dismissActionError}
          className="mt-3 select-text"
          testId="devfleet-action-error"
        />
      </div>
      {servingReason && (
        <div
          role="alert"
          data-testid="serving-install-warning"
          className="flex items-start gap-2 rounded-md border border-warn/40 bg-warn-subtle px-3 py-2.5 mt-3 text-[12.5px] leading-relaxed text-warn-text"
        >
          <AlertTriangle size={14} className="lucide-inline shrink-0 mt-0.5" />
          <div className="min-w-0">
            {/* break-words: the two embedded install paths are unbroken
                tokens and CSS does not wrap at '/'. */}
            <span className="break-words">{servingReason}</span>
          </div>
        </div>
      )}
      {!podsAvailable && (
        <div
          role="note"
          className="flex items-start gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2.5 mt-3 text-[12.5px] leading-relaxed"
        >
          <Info size={14} className="lucide-inline shrink-0 mt-0.5 text-muted" />
          <div className="min-w-0">
            <span className="text-text-strong">{i18nT('pages.devFleetPage.pods_are_unavailable_on_this_host')}</span>{' '}
            {podsReason ? <><span className="text-muted">{podsReason}</span>{' '}</> : null}
            <span className="text-muted">{i18nT('pages.devFleetPage.pod_and_make_live_actions_are_hidden_everything')}</span>
          </div>
        </div>
      )}
      {gatewayReason && (
        <div
          role="note"
          className="flex items-start gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2.5 mt-3 text-[12.5px] leading-relaxed"
        >
          <Info size={14} className="lucide-inline shrink-0 mt-0.5 text-muted" />
          <div className="min-w-0">
            <span className="text-muted">{gatewayReason}</span>
          </div>
        </div>
      )}
      {/* Dashes, not zeros, whenever the fleet is unknown: "WORKTREES 0" is a
          claim about a fleet that was never read, which is the same
          false certainty the discovery fix exists to remove. */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 my-3.5">
        <StatCard label={i18nT('pages.devFleetPage.running_pods')} value={noFleet ? '—' : running} accent={!noFleet} />
        <StatCard label={i18nT('pages.devFleetPage.worktrees')} value={noFleet ? '—' : wts.length} />
        <StatCard label={i18nT('pages.devFleetPage.needs_provision')} value={noFleet ? '—' : needsProv} />
        <StatCard label={i18nT('pages.devFleetPage.disk_worktrees')} value={noFleet || diskFailed ? '—' : diskGb} />
      </div>
      {/* A failed /disk read shows "—" in the card (not the "…" that reads
          as still measuring) and is named here. Read failure, nothing
          typed — hand-off on. */}
      {!noFleet && diskFailed && (
        <ErrorNotice
          title={i18nT('pages.devFleetPage.disk_usage_unavailable')}
          message={diskError instanceof Error ? diskError.message : String(diskError)}
          variant="inline"
          askAgent
          className="mb-3"
          testId="disk-error"
        />
      )}
    </>
  )
}
