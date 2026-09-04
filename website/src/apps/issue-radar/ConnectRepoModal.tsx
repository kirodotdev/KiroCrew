import type { ActiveRepo } from './lib/types'
// Connect-a-repo modal — the LAST slide of the WelcomeCarousel (the connect
// card) lifted out on its own and overlaid on top of the current Issue Radar
// view (typically the Settings page), which shows through as a blurred
// backdrop. Used for "connect ANOTHER repo" once at least one repo is already
// connected; the full-screen WelcomeCarousel is reserved for the first-run
// (no repos) onboarding.
//
// The card body is the SHARED <ConnectPanel> (provider rows + repo
// multi-select), and the connect state lives in the shared `useConnectFlow`
// hook — because the Connect button renders in this card's footer, OUTSIDE the
// panel. Picking GitHub grows the card from COLLAPSED_CARD to EXPANDED_CARD so
// the repo column has room.
//
// Scope note: this is `absolute inset-0`, not `fixed`, so the blur covers only
// the Issue Radar app area (its `relative` wrapper in IssueRadarPage) rather
// than the whole KiroCrew window — the settings page becomes the blurred
// backdrop, per product intent.
//
// Motion is Framer Motion (not CSS keyframe animations) per the frontend rule:
// only AnimatePresence can hold the dialog in the DOM long enough to play an
// exit animation on unmount.
import { useCallback, useRef } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { RefreshCw, X } from 'lucide-react'
import ConnectPanel, { COLLAPSED_CARD, EXPANDED_CARD, expandsCard, useConnectFlow } from './ConnectPanel'
import { useIssueRadar } from './context'
import Clickable from '../../components/Clickable'
import { useDialogFocusTrap } from '../../hooks/useDialogFocusTrap'

import { i18nT } from '../../i18n/t'
export default function ConnectRepoModal({
  onConnected,
  onClose,
}: {
  onConnected: (repo: ActiveRepo) => void
  onClose: () => void
}) {
  // This modal only ever renders inside <IssueRadarProvider> (see
  // IssueRadarPage), so the workspace's view state is live here. After a
  // successful connect we switch to the issue list ourselves rather than
  // relying on the persisted hint IssueRadarPage writes — the provider is
  // already mounted, so it would overwrite that hint with its current view on
  // the next render. Same for the OPEN filter: the auto-selected first issue
  // should be an open one, not whatever a leftover "closed" filter surfaces.
  const {
    openIssues, setSelectedIssue, setStateFilter, setQuery, clearFilters,
    setSelectedPull, setPrQuery, clearPrFilters,
  } = useIssueRadar()
  const flow = useConnectFlow((repo) => {
    setSelectedIssue(null)
    // The previous repo's search text and label/member filters would otherwise
    // carry over and can exclude every issue in the new repo.
    setQuery('')
    clearFilters()
    setStateFilter('open')
    // Same reset on the PR side, mirroring `switchRepo` and the persisted path
    // in IssueRadarPage. `selectedPull` matters most: it's a NUMBER, so a
    // leftover #42 silently auto-opens the new repo's unrelated #42.
    setSelectedPull(null)
    setPrQuery('')
    clearPrFilters()
    openIssues()
    onConnected(repo)
  })
  const expanded = expandsCard(flow.provider)
  const count = flow.targets.length

  const dialogRef = useRef<HTMLDivElement>(null)

  // Dismissal is BLOCKED while a connect is in flight: closing only unmounts
  // the UI, it does not cancel the sequential fetch loop, so repos would keep
  // connecting (and the success callback would still fire) after the user
  // thought they'd cancelled.
  const requestClose = useCallback(() => {
    if (flow.pending) return
    onClose()
  }, [flow.pending, onClose])

  // Focus in/out, Escape dismissal, and the Tab focus trap — shared with the
  // cross-reference sheet (see hooks/useDialogFocusTrap).
  useDialogFocusTrap(dialogRef, requestClose)

  return (
    <AnimatePresence>
      {/* The backdrop and the dialog are SIBLINGS inside a non-interactive
        * container. Wrapping the dialog in the <Clickable> backdrop gave every
        * control inside it a `button` ancestor, which assistive technology can
        * treat as one flattened widget and suppress the descendant semantics
        * (the role="dialog", the checkboxes, the text input). */}
      <div className="absolute inset-0 z-50 flex items-center justify-center p-3">
        <Clickable
          className="absolute inset-0 bg-bg/50 backdrop-blur-sm"
          onClick={requestClose}
          aria-label={i18nT('apps.issueRadar.connectRepoModal.close_connect_dialog')}
        />
        <motion.div
          ref={dialogRef}
          role="dialog"
          aria-modal="true"
          aria-label={i18nT('apps.issueRadar.connectRepoModal.connect_a_repo')}
          tabIndex={-1}
          initial={{ opacity: 0, y: 8, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 8, scale: 0.98 }}
          transition={{ duration: 0.18, ease: 'easeOut' }}
          className={`relative border border-border rounded-[14px] bg-card flex flex-col items-center justify-between p-10 shadow-2xl outline-none transition-[width,height] duration-200 ease-out ${
            expanded ? EXPANDED_CARD : COLLAPSED_CARD
          }`}
          onKeyDown={(e) => e.stopPropagation()}
        >
          <button
            onClick={requestClose}
            disabled={flow.pending}
            aria-label={i18nT('apps.issueRadar.connectRepoModal.close')}
            className="absolute top-3 right-3 p-1.5 rounded-md text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-0 disabled:opacity-30 disabled:cursor-default"
          >
            <X size={16} />
          </button>

          {/* min-h-0 + overflow-y-auto: the footer row below is a sibling, so
           * without constraining the body here a taller-than-expected body — or
           * a card capped by max-h on a short viewport — would push
           * Back/Connect past the card's height and out of reach. It scrolls
           * rather than clips so capped content stays reachable.
           * Top-anchored only once EXPANDED, for the same reason as the
           * carousel's connect slide — centring the two-column form left a
           * lopsided gap above the title, while the collapsed source list is
           * short enough that anchoring it to the top just strands it above a
           * large empty area. */}
          <div
            className={`flex-1 min-h-0 overflow-y-auto flex flex-col items-center gap-3.5 w-full ${
              expanded ? 'justify-start' : 'justify-center'
            }`}
          >
            <ConnectPanel flow={flow} />
          </div>

          {/* Footer action row — Connect sits bottom-right, mirroring the slot
           * Next/Connect occupy in the carousel. Only shown once a provider is
           * chosen; before that the provider rows are the only action. */}
          <div className="flex items-center justify-end w-full pt-3 min-h-[34px] flex-shrink-0">
            {flow.provider && (
              <button
                onClick={flow.submit}
                disabled={count === 0 || flow.pending}
                className="min-w-[84px] inline-flex items-center justify-center gap-1 px-4 py-1.5 rounded-md border border-accent text-accent bg-transparent text-xs font-semibold cursor-pointer hover:bg-accent-subtle disabled:opacity-30"
              >
                <RefreshCw size={12} className={flow.pending ? 'animate-spin' : ''} />
                {flow.progress
                  ? i18nT('apps.issueRadar.connectRepoModal.connecting', { done: flow.progress.done + 1, total: flow.progress.total })
                  : count > 1 ? `Connect ${count}` : 'Connect'}
              </button>
            )}
          </div>
        </motion.div>
      </div>
    </AnimatePresence>
  )
}
