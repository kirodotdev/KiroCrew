/**
 * The top of the MCP page: the hero card (title, Learn more, the page-wide
 * sharing switch, the measured numbers and the reload banner), the approval
 * card, and the reload confirm. `McpManagement` owns the state and the writes;
 * these only draw it.
 *
 * The numbers are the gateway's own reading of this host
 * (`GET /api/mcp-gateway/footprint`, PSS from /proc). The row is drawn only
 * when both figures came back; nothing here estimates one.
 */
import { useEffect, useState } from 'react'
import { AlertTriangle, Check, ExternalLink, RefreshCw, Share2 } from 'lucide-react'
import type { McpGatewayFootprint, McpManagedServer } from '../../api/client'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../../components/ui/dialog'
import { useIsNarrowViewport } from '../../hooks/useIsMobile'
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { detailsWhyKey, DOCS_URL, gibNumber, type LaunchRefusal } from './mcpManagementModel'
import { RefusalLaunch, SHARE_BUTTON_CLASS } from './McpManagementLaunchReview'

export const HERO_TITLE_ID = 'mcp-hero-title'
const SWITCH_NOTE_ID = 'mcp-hero-switch-note'

function gib(bytes: number): string {
  return fmtNumber(gibNumber(bytes), { minimumFractionDigits: 1, maximumFractionDigits: 1 })
}

/** The page-wide switch, drawn larger than a row's and named by the hero title. */
function HeroSwitch({
  on,
  disabled,
  describedBy,
  onClick,
}: {
  on: boolean
  disabled: boolean
  describedBy?: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-labelledby={HERO_TITLE_ID}
      aria-describedby={describedBy}
      disabled={disabled}
      onClick={onClick}
      className="inline-flex shrink-0 items-center rounded-full p-1 transition-colors hover:bg-[var(--hover)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-50"
    >
      <span
        aria-hidden="true"
        className={[
          'relative inline-flex h-[24px] w-[42px] shrink-0 items-center rounded-full transition-colors',
          on ? 'bg-accent' : 'bg-[var(--border-strong,var(--border))]',
        ].join(' ')}
      >
        {/* The knob rides on the accent fill, so it keeps one light face in every theme. */}
        <span
          className={[
            'absolute h-[20px] w-[20px] rounded-full bg-white shadow transition-all',
            on ? 'left-[20px]' : 'left-[2px]',
          ].join(' ')}
        />
      </span>
    </button>
  )
}

export function SharingHero({
  on,
  switchDisabled,
  switchNote,
  onToggle,
  footprint,
  footprintFailed,
  staleCount,
  reloadBusy,
  reloadNote,
  reloadError,
  onReload,
}: {
  on: boolean
  switchDisabled: boolean
  /** Why the switch is disabled, read to assistive tech only (no visible line). */
  switchNote?: string | null
  onToggle: () => void
  footprint?: McpGatewayFootprint
  /** The footprint read itself failed (a request error, not a null figure). */
  footprintFailed?: boolean
  /** Open chats still running their own copy of a server shared now. */
  staleCount: number
  reloadBusy: boolean
  /** Busy chats the last reload skipped, shown in the banner. */
  reloadNote?: string | null
  /** Chats the last reload could not reload: a request failure. */
  reloadError?: string | null
  onReload: () => void
}) {
  const now = footprint?.using_now_bytes
  const withSharing = footprint?.with_sharing_bytes
  const showNumbers = typeof now === 'number' && typeof withSharing === 'number'
  const showBanner = on && (staleCount > 0 || !!reloadNote || !!reloadError)
  return (
    <section
      // What the footprint read is doing, for a test to wait on instead of a
      // timer: pending, ready (answered, figures or not) or error.
      data-footprint={footprintFailed ? 'error' : footprint ? 'ready' : 'pending'}
      className="min-w-0 space-y-4 rounded-xl border border-[var(--border)] bg-[var(--card)] px-4 py-4 md:px-5"
    >
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span
            aria-hidden="true"
            className="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[var(--accent-subtle,var(--hover))] text-[var(--accent)]"
          >
            <Share2 size={16} />
          </span>
          <div className="min-w-0">
            <h2 id={HERO_TITLE_ID} className="text-[15px] font-semibold leading-snug text-[var(--text)]">
              {i18nT('pages.mcpManagement.hero_title')}
            </h2>
            <a
              href={DOCS_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-0.5 inline-flex items-center gap-1 rounded text-[12.5px] text-[var(--accent)] hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]"
            >
              {i18nT('pages.mcpManagement.learn_more')}
              <ExternalLink size={12} aria-hidden="true" />
            </a>
          </div>
        </div>
        <HeroSwitch
          on={on}
          disabled={switchDisabled}
          describedBy={switchNote ? SWITCH_NOTE_ID : undefined}
          onClick={onToggle}
        />
        {switchNote && (
          <span id={SWITCH_NOTE_ID} className="sr-only">
            {switchNote}
          </span>
        )}
      </div>

      <ErrorNotice
        variant="inline"
        message={footprintFailed ? i18nT('pages.mcpManagement.footprint_failed') : null}
        askAgent
      />

      {showNumbers && (
        <dl className="flex min-w-0 flex-wrap items-end gap-x-4 gap-y-2">
          <div className="min-w-0">
            <dt className="text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
              {i18nT('pages.mcpManagement.using_now')}
            </dt>
            <dd className="m-0 font-mono text-[22px] font-semibold tabular-nums text-[var(--text)]">
              {i18nT('pages.mcpManagement.gb_value', { value: gib(now) })}
            </dd>
          </div>
          <span aria-hidden="true" className="pb-1.5 text-[18px] text-[var(--muted)]">
            →
          </span>
          <div className="min-w-0">
            <dt className="text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
              {i18nT('pages.mcpManagement.with_sharing')}
            </dt>
            <dd className="m-0 font-mono text-[22px] font-semibold tabular-nums text-[var(--accent)]">
              {i18nT('pages.mcpManagement.gb_value_approx', { value: gib(withSharing) })}
            </dd>
          </div>
        </dl>
      )}

      {showBanner && (
        <div
          role="status"
          className="flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-2 rounded-lg border border-[var(--warn)] bg-[var(--hover)] px-3 py-2 text-[13px] text-[var(--text)]"
        >
          <div className="min-w-0 space-y-0.5">
            {staleCount > 0 && (
              <p id="mcp-stale-banner" className="m-0">
                {i18nT('pages.mcpManagement.stale_banner', { count: staleCount })}
              </p>
            )}
            {reloadNote && <p className="m-0 text-[12.5px] text-[var(--muted)]">{reloadNote}</p>}
            <ErrorNotice variant="inline" message={reloadError ?? null} askAgent />
          </div>
          {staleCount > 0 && (
            <Btn
              type="button"
              className={SHARE_BUTTON_CLASS}
              disabled={reloadBusy}
              aria-describedby="mcp-stale-banner"
              onClick={onReload}
            >
              <RefreshCw size={14} aria-hidden="true" />
              {i18nT('pages.mcpManagement.reload_button', { count: staleCount })}
            </Btn>
          )}
        </div>
      )}
    </section>
  )
}

export function ApprovalCard({
  waiting,
  onReview,
}: {
  waiting: readonly McpManagedServer[]
  onReview: () => void
}) {
  if (waiting.length === 0) return null
  return (
    <section
      aria-labelledby="mcp-approval-title"
      className="min-w-0 space-y-3 rounded-xl border border-[var(--warn)] bg-[var(--card)] px-4 py-3.5 md:px-5"
    >
      <h2
        id="mcp-approval-title"
        className="m-0 flex items-center gap-2 text-[14px] font-semibold text-[var(--text)]"
      >
        <AlertTriangle size={15} className="shrink-0 text-[var(--warn)]" aria-hidden="true" />
        {i18nT('pages.mcpManagement.approval_title', { count: waiting.length })}
      </h2>
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <ul aria-labelledby="mcp-approval-title" className="m-0 flex min-w-0 list-none flex-wrap gap-1.5 p-0">
          {waiting.map(s => (
            <li
              key={s.name}
              className="break-all rounded-full border border-[var(--border)] px-2 py-0.5 font-mono text-[11.5px] text-[var(--text)]"
            >
              {s.name}
            </li>
          ))}
        </ul>
        <Btn
          type="button"
          primary
          className={SHARE_BUTTON_CLASS}
          aria-describedby="mcp-approval-title"
          onClick={onReview}
        >
          {i18nT('pages.mcpManagement.review_approve')}
        </Btn>
      </div>
    </section>
  )
}

/** The id of a Review block's Approve button. */
export function reviewApproveId(name: string): string {
  return `mcp-review-approve-${name}`
}

/** One waiting server as the Review dialog opened on it. */
export type ReviewItem = { server: McpManagedServer; refusal: LaunchRefusal }

type ReviewState = { kind: 'pending' | 'approving' | 'approved' | 'skipped'; error?: string }

/**
 * "Review and approve": the servers that wait for an OK, one block each, with
 * what it runs and its own Approve and Skip. No approve-all: every approval is
 * one server's exact launch.
 *
 * The list is the one the dialog opened on, so an approved block stays (as
 * "Approved") instead of vanishing when the status read moves on. Approve
 * pins the launch shown in the block (`refusal.expected_launch` as it was
 * when the dialog opened); a launch that changed underneath is refused by the
 * endpoint, and that refusal is shown in the block. Skip writes nothing. When
 * every block is handled the dialog closes itself.
 */
export function ReviewApprovalsDialog({
  open,
  items,
  supported,
  returnFocusTo,
  onApprove,
  onClose,
}: {
  open: boolean
  items: readonly ReviewItem[]
  supported: boolean
  /** Where focus goes when the dialog closes: the button that opened it. */
  returnFocusTo?: HTMLElement | null
  /** The table button's own write; resolves with an error line, or null. */
  onApprove: (name: string, expectedLaunch: string) => Promise<string | null>
  onClose: () => void
}) {
  const narrow = useIsNarrowViewport()
  const [states, setStates] = useState<Record<string, ReviewState>>({})
  // A fresh list each time the dialog opens.
  useEffect(() => {
    if (open) setStates({})
  }, [open, items])
  const stateOf = (name: string): ReviewState => states[name] ?? { kind: 'pending' }
  const allHandled =
    items.length > 0 && items.every(i => ['approved', 'skipped'].includes(stateOf(i.server.name).kind))
  useEffect(() => {
    if (open && allHandled) onClose()
  }, [open, allHandled, onClose])

  // After a block is handled, focus the next pending block's Approve (after
  // it, else before it), so the keyboard never lands on the collapsed block
  // or falls back to the page.
  const focusNextPending = (name: string, next: Record<string, ReviewState>) => {
    const at = items.findIndex(i => i.server.name === name)
    const order = [...items.slice(at + 1), ...items.slice(0, at)]
    const target = order.find(i => (next[i.server.name]?.kind ?? 'pending') === 'pending')
    if (!target) return
    window.requestAnimationFrame(() => {
      const button = document.getElementById(reviewApproveId(target.server.name))
      button?.focus({ preventScroll: true })
      button?.scrollIntoView?.({ block: 'nearest' })
    })
  }
  const settle = (name: string, state: ReviewState) =>
    setStates(s => {
      const next = { ...s, [name]: state }
      if (state.kind === 'approved' || state.kind === 'skipped') focusNextPending(name, next)
      return next
    })

  const approve = async (item: ReviewItem) => {
    const name = item.server.name
    setStates(s => ({ ...s, [name]: { kind: 'approving' } }))
    const error = await onApprove(name, item.refusal.expected_launch as string)
    settle(name, error ? { kind: 'pending', error } : { kind: 'approved' })
  }

  return (
    <Dialog
      open={open}
      onOpenChange={next => {
        if (!next) onClose()
      }}
    >
      <DialogContent
        maxWidth={narrow ? 10_000 : 760}
        onCloseAutoFocus={e => {
          if (returnFocusTo?.isConnected) {
            e.preventDefault()
            returnFocusTo.focus()
          }
        }}
        // Below md the dialog is a full-screen sheet sized to the DYNAMIC
        // viewport (100dvh, 100vh where dvh is unknown), so a mobile browser's
        // toolbar never hides the last block.
        style={narrow ? { maxHeight: 'none', width: '100%', borderRadius: 0 } : undefined}
        className={narrow ? 'h-screen supports-[height:100dvh]:h-dvh' : undefined}
      >
        <DialogHeader>
          {/* Counts what still waits, so it drops as blocks are handled. */}
          <DialogTitle>
            {i18nT('pages.mcpManagement.approval_title', {
              count: Math.max(1, items.filter(i => stateOf(i.server.name).kind !== 'approved' && stateOf(i.server.name).kind !== 'skipped').length),
            })}
          </DialogTitle>
        </DialogHeader>
        {/* The body is the only scroller; its overscroll never reaches the page. */}
        <DialogBody data-testid="mcp-review-body" className="overscroll-contain">
          <div className="space-y-3">
          {/* What approving risks and when it is fine, said once for every block.
              In the body, not the header: the header row would squeeze the title. */}
          <DialogDescription>
            {i18nT('pages.mcpManagement.share_risk')}{' '}
            {i18nT('pages.mcpManagement.review_when_safe')}
          </DialogDescription>
          {items.map(item => {
            const { server, refusal } = item
            const st = stateOf(server.name)
            const handled = st.kind === 'approved' || st.kind === 'skipped'
            const nameId = `mcp-review-name-${server.name}`
            const reasonId = `mcp-review-reason-${server.name}`
            return (
              <section
                key={server.name}
                aria-labelledby={nameId}
                className="min-w-0 rounded-lg border border-[var(--border)] bg-[var(--bg)] px-3 py-2.5"
              >
                <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
                  <h3 id={nameId} className="m-0 break-all font-mono text-[13px] font-semibold text-[var(--text)]">
                    {server.name}
                  </h3>
                  {handled && (
                    <span role="status" className="inline-flex items-center gap-1 text-[12.5px] text-[var(--muted)]">
                      {st.kind === 'approved' && <Check size={14} className="text-[var(--accent)]" aria-hidden="true" />}
                      {i18nT(st.kind === 'approved' ? 'pages.mcpManagement.review_approved' : 'pages.mcpManagement.review_skipped')}
                    </span>
                  )}
                </div>
                {!handled && (
                  <div className="mt-1.5 min-w-0 space-y-2 text-[12px] text-[var(--muted)]">
                    <p id={reasonId} className="m-0 text-[12.5px]">
                      {i18nT(detailsWhyKey(server, 'needs_reapproval', refusal))}
                    </p>
                    <RefusalLaunch refusal={refusal} />
                    {/* What the two buttons do, said in the block they act on. */}
                    <p id={`mcp-review-means-${server.name}`} className="m-0 text-[12.5px] text-[var(--text)]">
                      {i18nT('pages.mcpManagement.review_what_approve_does')}
                    </p>
                    <ErrorNotice variant="inline" message={st.error ?? null} askAgent />
                    <div className={['flex gap-2', narrow ? 'flex-col' : 'flex-wrap justify-end'].join(' ')}>
                      <Btn
                        type="button"
                        className={[SHARE_BUTTON_CLASS, narrow ? 'w-full justify-center' : ''].join(' ')}
                        disabled={st.kind === 'approving'}
                        aria-describedby={nameId}
                        onClick={() => settle(server.name, { kind: 'skipped' })}
                      >
                        {i18nT('pages.mcpManagement.review_skip')}
                      </Btn>
                      <Btn
                        id={reviewApproveId(server.name)}
                        type="button"
                        primary
                        className={[SHARE_BUTTON_CLASS, narrow ? 'w-full justify-center' : ''].join(' ')}
                        disabled={st.kind === 'approving' || !supported}
                        aria-describedby={`${nameId} ${reasonId} mcp-review-means-${server.name}`}
                        onClick={() => void approve(item)}
                      >
                        <Check size={14} aria-hidden="true" />
                        {i18nT('pages.mcpManagement.approve_button')}
                      </Btn>
                    </div>
                  </div>
                )}
              </section>
            )
          })}
          </div>
        </DialogBody>
      </DialogContent>
    </Dialog>
  )
}

export function ConfirmReload({
  open,
  count,
  busy,
  onCancel,
  onConfirm,
}: {
  open: boolean
  count: number
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={next => {
        if (!next && !busy) onCancel()
      }}
    >
      <DialogContent maxWidth={460}>
        <DialogHeader>
          <DialogTitle>{i18nT('pages.mcpManagement.reload_confirm_title', { count })}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <DialogDescription>
            {i18nT('pages.mcpManagement.reload_confirm_body')}
          </DialogDescription>
        </DialogBody>
        <DialogFooter>
          <div className="flex gap-2.5">
            <button
              type="button"
              onClick={onCancel}
              className="rounded-md border border-border px-3.5 py-2 text-[13.5px] text-text"
            >
              {i18nT('pages.mcpManagement.cancel')}
            </button>
            <button
              type="button"
              autoFocus
              disabled={busy}
              onClick={onConfirm}
              className="rounded-md bg-accent px-3.5 py-2 text-[13.5px] font-medium text-accent-fg disabled:opacity-60"
            >
              {i18nT('pages.mcpManagement.reload_button', { count })}
            </button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
