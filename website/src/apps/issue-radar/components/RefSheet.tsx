// Cross-reference sheet — the in-app window for an issue/PR link clicked inside
// a body or comment.
//
// Slides up from the bottom of the Issue Radar area over a blurred backdrop and
// renders the SAME detail pane the right column uses (IssueDetail / PrDetail),
// so a referenced issue reads exactly like a selected one: same header, same AI
// card, same timeline, same sidebar, same write actions.
//
// Following a reference from INSIDE the sheet pushes onto a stack, so "back"
// walks the trail you came in on and the surrounding workspace — list, filters,
// selected issue — is never disturbed.
//
// Scope note: `absolute inset-0`, not `fixed`, so it covers the Issue Radar app
// area (its `relative` wrapper in IssueRadarPage) rather than the whole KiroCrew
// window — matching ConnectRepoModal.
import { useCallback, useRef } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { ChevronLeft, CircleDot, ExternalLink, GitPullRequest, Loader2, X } from 'lucide-react'
import { PanelRightSolid } from '../../../components/icons/panels'
import Clickable from '../../../components/Clickable'
import { useDialogFocusTrap } from '../../../hooks/useDialogFocusTrap'
import { useIssueRadar } from '../context'
import { providerTerms } from '../lib/links'
import { placeholderIssue, placeholderPull, refKey, refUrl } from '../lib/refLinks'
import { useRefSummary } from './RefLink'
import IssueDetail from './IssueDetail'
import PrDetail from './PrDetail'

import { i18nT } from '../../../i18n/t'
const ICON_BTN =
  'inline-flex items-center gap-1 rounded-md p-1.5 text-muted hover:text-text hover:bg-bg-hover ' +
  'cursor-pointer bg-transparent border-0'

export default function RefSheet() {
  const {
    refStack, popRef, closeRefs, active, issues, pulls,
    setSelectedIssue, setSelectedPull, openIssues, openPulls,
  } = useIssueRadar()
  const { owner, repo } = active
  const terms = providerTerms(active)
  const reduceMotion = useReducedMotion()
  const dialogRef = useRef<HTMLDivElement>(null)

  const top = refStack.length > 0 ? refStack[refStack.length - 1] : null
  // Escape steps BACK one level rather than closing the whole trail — the same
  // thing the header's back button does, and what a nested reference implies.
  const onEscape = useCallback(() => { popRef() }, [popRef])
  useDialogFocusTrap(dialogRef, onEscape)

  // A target reached as `#123` (or as an `/issues/123` URL) may actually be a
  // PULL REQUEST on GitHub — it shares one number sequence with issues and
  // redirects that path — so which pane to render is decided by the ref summary,
  // not by the link's shape. Usually already warm: the hover card on the very
  // link that was clicked ran the same query. On GitLab the two sequences are
  // separate (`#5` vs `!5`), so its summary always answers "issue" and the
  // ambiguity simply does not arise.
  const refQuery = useRefSummary(active, top?.number ?? 0, !!top && top.kind === 'issue')
  const isPr = top?.kind === 'pull' || refQuery.data?.summary.is_pr === true
  // Only the ambiguous case waits. An explicit /pull/ link renders immediately,
  // and a FAILED lookup degrades to the issue pane rather than blocking on it.
  const resolving = !!top && top.kind === 'issue' && refQuery.isLoading

  // The list row for this target, when the loaded list happens to hold it: it
  // gives the pane an instant first paint (title, labels, author) and is what
  // makes "open in the list" meaningful. Otherwise a placeholder row carries the
  // number until the detail fetch lands.
  const listedIssue = top && !isPr ? issues.find((i) => i.number === top.number) ?? null : null
  const listedPull = top && isPr ? pulls.find((p) => p.number === top.number) ?? null : null
  const listed = listedIssue ?? listedPull

  /** Promote the target to the main workspace selection and drop the sheet.
   * Only offered when the row is in the loaded list — selecting a number the
   * list does not hold would leave the right column empty. */
  const openInWorkspace = () => {
    if (!top) return
    if (isPr) {
      setSelectedPull(top.number)
      openPulls()
    } else {
      setSelectedIssue(top.number)
      openIssues()
    }
    closeRefs()
  }

  return (
    <AnimatePresence>
      {top && (
        // The backdrop and the sheet are SIBLINGS inside a non-interactive
        // container: wrapping the sheet in the <Clickable> backdrop would give
        // every control inside it a `button` ancestor, which assistive tech can
        // flatten into one widget and suppress the descendant semantics.
        //
        // No bottom padding, and the sheet's bottom corners stay square: it is
        // anchored to the bottom edge so it reads as GROWING OUT of the page
        // rather than as a card that happens to sit low.
        <div className="absolute inset-0 z-50 flex items-end justify-center px-3 pt-3 sm:px-5 sm:pt-5">
          <Clickable
            className="absolute inset-0 bg-bg/50 backdrop-blur-sm"
            onClick={closeRefs}
            aria-label={i18nT('apps.issueRadar.components.refSheet.close_reference')}
          />
          <motion.div
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-label={`${isPr ? terms.changeRequestTitle : i18nT('apps.issueRadar.components.refSheet.issue')} ${isPr ? terms.sigil : '#'}${top.number} in ${owner}/${repo}`}
            tabIndex={-1}
            initial={reduceMotion ? { opacity: 0 } : { y: '100%' }}
            animate={reduceMotion ? { opacity: 1 } : { y: 0 }}
            exit={reduceMotion ? { opacity: 0 } : { y: '100%' }}
            transition={{ duration: 0.24, ease: 'easeOut' }}
            // Sizing: takes most of the app area (a detail pane is a two-column
            // layout with a 236px sidebar, so cramping it costs real reading
            // width), but never all of it — the workspace staying visible around
            // the edges is what tells you this is a detour, not a navigation.
            // The px caps only bite on a very large display, where 94% would be
            // wider than any line worth reading.
            className="relative w-[min(1800px,94%)] h-[min(1500px,93%)] min-w-0 min-h-0 flex flex-col overflow-hidden rounded-t-2xl border border-border border-b-0 bg-bg shadow-2xl outline-none"
            // The workspace's own shortcuts (list navigation, `/` to search)
            // must not fire while the sheet has focus.
            onKeyDown={(e) => e.stopPropagation()}
          >
            {/* Sheet chrome — the only thing added around the reused pane. */}
            <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-bg-elevated/60 flex-shrink-0">
              {refStack.length > 1 && (
                <button onClick={popRef} aria-label={i18nT('apps.issueRadar.components.refSheet.back_to_the_referencing_item')} title={i18nT('apps.issueRadar.components.refSheet.back')} className={ICON_BTN}>
                  <ChevronLeft className="lucide-inline" aria-hidden="true" />
                </button>
              )}
              <span className="inline-flex items-center gap-1.5 text-[12.5px] text-muted min-w-0">
                {isPr
                  ? <GitPullRequest className="lucide-inline text-accent" aria-hidden="true" />
                  : <CircleDot className="lucide-inline text-accent" aria-hidden="true" />}
                <span className="truncate">{owner}/{repo}</span>
                <span className="font-mono text-text">{isPr ? terms.sigil : '#'}{top.number}</span>
                {refStack.length > 1 && (
                  <span className="text-muted opacity-70">· {refStack.length} {i18nT('apps.issueRadar.components.refSheet.deep')}</span>
                )}
              </span>
              <div className="ml-auto flex items-center gap-1">
                {listed && (
                  <button
                    onClick={openInWorkspace}
                    aria-label={i18nT('apps.issueRadar.components.refSheet.open_this_item_in_the_workspace')}
                    title={i18nT('apps.issueRadar.components.refSheet.open_in_the_list_detail_column')}
                    className={`pi-morph ${ICON_BTN}`}
                  >
                    <PanelRightSolid className="lucide-inline" aria-hidden="true" />
                  </button>
                )}
                <a
                  href={refUrl(active, { kind: isPr ? 'pull' : 'issue', number: top.number })}
                  target="_blank"
                  rel="noreferrer"
                  aria-label={`Open on ${terms.providerName}`}
                  title={`Open on ${terms.providerName}`}
                  className={ICON_BTN}
                >
                  <ExternalLink className="lucide-inline" aria-hidden="true" />
                </a>
                <button onClick={closeRefs} aria-label={i18nT('apps.issueRadar.components.refSheet.close')} title={i18nT('apps.issueRadar.components.refSheet.close_esc')} className={ICON_BTN}>
                  <X className="lucide-inline" aria-hidden="true" />
                </button>
              </div>
            </div>

            {/* The reused detail pane. Keyed on the target so switching entries
              * remounts it — the panes reset their own per-item transient state
              * on a number change, but a remount is the unambiguous reset and
              * costs nothing here (the detail query is cached). */}
            <div key={refKey(top)} className="flex-1 min-h-0">
              {resolving
                ? (
                  <div className="h-full flex items-center justify-center">
                    <Loader2 className="lucide-inline animate-spin text-muted" aria-hidden="true" />
                    <span className="sr-only">{i18nT('apps.issueRadar.components.refSheet.loading')}{top.number}</span>
                  </div>
                )
                : isPr
                  ? <PrDetail pull={listedPull ?? placeholderPull(active, top.number, refQuery.data?.summary.state)} />
                  : <IssueDetail issue={listedIssue ?? placeholderIssue(active, top.number, refQuery.data?.summary.state)} />}
            </div>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
