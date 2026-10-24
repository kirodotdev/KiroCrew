import { memo, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useInRouterContext } from 'react-router-dom'
import { AlertTriangle, CheckCircle2, Globe, Loader2 } from 'lucide-react'

import { ApiError } from '../../api/apiError'
import { api, browserInstallConflictJob, type BrowserInstallData } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { hasBrowserInstallMarker } from '../../utils/detectBrowserInstallMarker'
import { STAGE_KEY } from '../settings/BrowserInstallStatus'
import { INSTALL_KEY, type PendingRequest, withConflictJob } from '../settings/browserInstallState'
import { browserInstallCardState } from './browserInstallCardState'

/** Re-read cadence while an install runs; at rest the card does not poll. */
const RUNNING_POLL_MS = 2_000

const BROWSER_SETTINGS_ROUTE = '/settings/browser'

// Same button and link primitives as the redaction cards; touch targets grow
// to 44px on hover-less screens.
const touchTarget = '[@media(hover:none)]:min-h-11'
const btnBase = [
  'inline-flex', 'items-center', 'justify-center', 'whitespace-nowrap', 'rounded-md', 'border', 'bg-card',
  'px-2.5', 'py-[3px]', 'text-[12px]', 'leading-[18px]', 'min-h-7', 'text-card-fg', 'cursor-pointer',
  'hover:border-border-strong', 'hover:bg-bg-hover', 'disabled:cursor-default', 'disabled:opacity-70', touchTarget,
].join(' ')
const btnCls = [btnBase, 'border-border'].join(' ')
const btnPrimaryCls = [btnBase, 'border-accent', 'bg-accent-subtle'].join(' ')
const linkCls = [
  'inline-flex', 'items-center', 'border-0', 'bg-transparent', 'px-0.5', 'py-1', 'min-h-6', 'text-[12px]',
  'text-accent', 'no-underline', 'hover:underline', touchTarget,
].join(' ')

/** Put text in the composer for the reader to send; never sends it. */
function prefillComposer(text: string) {
  window.dispatchEvent(new CustomEvent('mc:prefill-composer', { detail: { text } }))
}

/**
 * The in-chat "Install browser support" card, shown under a finished reply that
 * carries `<!-- kirocrew:browser-install -->` (see detectBrowserInstallMarker).
 *
 * The agent writes that marker when its `browser` tool reports that browsing
 * is not set up on this host, so a customer who asked to open a page can fix it
 * where they asked, without knowing Settings -> Browser exists. The card runs
 * the same owner-gated install that page runs, shows its progress, and on
 * success offers to continue: that pre-fills a message in the composer for the
 * reader to send, as every other in-chat suggestion does.
 *
 * Rendering it installs nothing and starts no poll at rest; the status is read
 * once, re-read on window focus, and polled only while an install runs.
 */
const BrowserInstallCard = memo(function BrowserInstallCard({ content }: {
  /** The reply's text as the bubble shows it. */
  content: string
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const offered = useMemo(() => hasBrowserInstallMarker(content), [content])
  if (!offered) return null
  return <BrowserInstallCardBody />
})

function BrowserInstallCardBody() {
  const qc = useQueryClient()
  const inRouter = useInRouterContext()
  const [continued, setContinued] = useState(false)

  const installMut = useMutation({
    mutationFn: () => api.installBrowserCli(),
    onSuccess: (fresh) => qc.setQueryData(INSTALL_KEY, fresh),
    onError: (e) => {
      const job = browserInstallConflictJob(e)
      if (job) qc.setQueryData(INSTALL_KEY, withConflictJob(job))
    },
    onSettled: () => { void qc.invalidateQueries({ queryKey: INSTALL_KEY }) },
  })
  const pending: PendingRequest | null = installMut.isPending ? { kind: 'cli_setup' } : null

  const q = useQuery<BrowserInstallData>({
    queryKey: INSTALL_KEY,
    queryFn: api.getBrowserInstall,
    // Stale at once (the global default is never stale), so mounting the card
    // or returning to the window re-reads the status, as Settings -> Browser does.
    staleTime: 0,
    refetchOnMount: 'always',
    refetchOnWindowFocus: true,
    refetchInterval: (query) => {
      const d = query.state.data
      const busy = d?.installing || d?.install_job?.status === 'running' || installMut.isPending
      return busy ? RUNNING_POLL_MS : false
    },
  })

  const statusReadError = q.isError && q.error instanceof Error && q.error.message
    ? q.error.message
    : null
  const state = q.isError
    ? { kind: 'unavailable' as const }
    : browserInstallCardState(q.data, pending, installMut.submittedAt)

  // The owner gate refused the POST. Retrying from this browser repeats the
  // same 403, so the card stops offering Install and says who can. A 403 that
  // only means this dashboard's sign-in lapsed (or a proxy's sign-in page) is
  // not the owner gate: it keeps the retry, which works once re-authenticated.
  const ownerOnly = installMut.error instanceof ApiError
    && installMut.error.status === 403
    && !installMut.error.authRequired
    && !installMut.error.edgeChallenge
  // The POST itself was refused (a non-owner 403, a 500, a lost answer). A 409
  // naming the running job is not an error here: the card shows that job.
  const requestError = ownerOnly
    ? i18nT('pages.chat.browserInstallCard.owner_only')
    : installMut.isError && !browserInstallConflictJob(installMut.error)
      ? installMut.error instanceof Error && installMut.error.message
        ? installMut.error.message
        : i18nT('pages.settings.browserPanel.install_request_failed')
      : null

  const install = () => { setContinued(false); installMut.mutate() }
  // An in-app route change, not a page load: the chat page's unmount flush
  // keeps the composer draft, and the reader stays in the dashboard. Outside a
  // Router (the capture harness, other hosts) a plain link is all there is.
  const settingsLinkAs = (className: string) => inRouter ? (
    <Link to={BROWSER_SETTINGS_ROUTE} className={className} data-testid="browser-install-card-settings">
      {i18nT('pages.chat.browserInstallCard.open_settings')}
    </Link>
  ) : (
    <a href={BROWSER_SETTINGS_ROUTE} className={className} data-testid="browser-install-card-settings">
      {i18nT('pages.chat.browserInstallCard.open_settings')}
    </a>
  )
  const settingsLink = settingsLinkAs(linkCls)

  let icon = <Globe size={16} className="text-accent shrink-0 mt-[2px]" aria-hidden="true" />
  let title: string
  let body: string | null = null
  let failureDetail: string | null = null
  let actions: ReactNode = null

  switch (state.kind) {
    case 'checking':
      icon = <Loader2 size={16} className="text-muted shrink-0 mt-[2px] animate-spin" aria-hidden="true" />
      title = i18nT('pages.chat.browserInstallCard.checking')
      break
    case 'unavailable':
      title = i18nT('pages.chat.browserInstallCard.status_unavailable')
      actions = (
        <>
          <button type="button" className={btnCls} onClick={() => void q.refetch()}>
            {i18nT('pages.chat.browserInstallCard.retry')}
          </button>
          {settingsLink}
        </>
      )
      break
    case 'running':
      icon = <Loader2 size={16} className="text-accent shrink-0 mt-[2px] animate-spin" aria-hidden="true" />
      title = i18nT('pages.chat.browserInstallCard.installing')
      body = state.stage && STAGE_KEY[state.stage]
        ? i18nT('pages.settings.browserPanel.stage_line', { stage: i18nT(STAGE_KEY[state.stage]) })
        : null
      break
    case 'installed':
      icon = <CheckCircle2 size={16} className="text-ok shrink-0 mt-[2px]" aria-hidden="true" />
      title = i18nT('pages.chat.browserInstallCard.installed')
      // Once clicked, the confirmation REPLACES the button: a second click would
      // only pre-fill the same message again.
      body = continued ? i18nT('pages.chat.browserInstallCard.continue_prefilled') : null
      actions = continued ? null : (
        <button
          type="button"
          className={btnPrimaryCls}
          data-testid="browser-install-card-continue"
          onClick={() => {
            prefillComposer(i18nT('pages.chat.browserInstallCard.continue_prompt'))
            setContinued(true)
          }}
        >
          {i18nT('pages.chat.browserInstallCard.continue')}
        </button>
      )
      break
    case 'failed':
      icon = <AlertTriangle size={16} className="text-danger shrink-0 mt-[2px]" aria-hidden="true" />
      title = i18nT('pages.chat.browserInstallCard.failed')
      // Plain-language next step first; the installer's raw output under it.
      body = i18nT('pages.chat.browserInstallCard.failed_next_step')
      failureDetail = state.detail || i18nT('pages.settings.browserPanel.failure_no_detail')
      // Settings shows what is missing, so it leads; a blind retry usually fails
      // the same way.
      actions = (
        <>
          {settingsLinkAs(btnPrimaryCls)}
          {!ownerOnly && (
            <button type="button" className={btnCls} onClick={install} data-testid="browser-install-card-install">
              {i18nT('pages.chat.browserInstallCard.retry')}
            </button>
          )}
        </>
      )
      break
    case 'needed':
      title = i18nT('pages.chat.browserInstallCard.needed_title')
      body = i18nT('pages.chat.browserInstallCard.needed_body')
      actions = (
        <>
          {!ownerOnly && (
            <button type="button" className={btnPrimaryCls} onClick={install} data-testid="browser-install-card-install">
              {i18nT('pages.chat.browserInstallCard.install')}
            </button>
          )}
          {settingsLink}
        </>
      )
      break
  }

  return (
    <section
      aria-label={i18nT('pages.chat.browserInstallCard.region_label')}
      className="ft-block-reveal mt-2 mb-1.5 max-w-[520px] rounded-md border border-border bg-card text-card-fg px-3 py-2.5 flex flex-col gap-2"
      data-testid="browser-install-card"
      data-state={state.kind}
    >
      {state.kind === 'unavailable' ? (
        <div className="flex items-start gap-2">
          {/* No hand-off: navigating away could discard an unsaved composer
              draft; this card only writes through the append-only prefill event. */}
          <ErrorNotice
            variant="inline"
            title={statusReadError ? title : undefined}
            message={statusReadError || title}
            testId="browser-install-card-status-error"
          />
        </div>
      ) : (
        // One live region: stage changes and the outcome are announced, the
        // buttons are not.
        <div role="status" className="flex items-start gap-2">
          {icon}
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-medium">{title}</div>
            {body && <div className="text-[13px] text-muted">{body}</div>}
          </div>
        </div>
      )}
      {/* No hand-off: navigating away could discard an unsaved composer
          draft; this card only writes through the append-only prefill event. */}
      {failureDetail && (
        <div className="pl-6">
          <ErrorNotice
            variant="inline"
            showIcon={false}
            message={failureDetail}
            messageClassName="font-mono whitespace-pre-wrap break-words"
            testId="browser-install-card-failure-detail"
          />
        </div>
      )}
      {/* No hand-off: navigating away could discard an unsaved composer
          draft; this card only writes through the append-only prefill event. */}
      {requestError && (
        <div className="pl-6">
          <ErrorNotice variant="inline" message={requestError} testId="browser-install-card-error" />
        </div>
      )}
      {actions && <div className="flex flex-wrap items-center gap-2 pl-6">{actions}</div>}
    </section>
  )
}

export default BrowserInstallCard
