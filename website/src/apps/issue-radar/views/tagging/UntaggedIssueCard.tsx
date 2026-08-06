import { Check, Plus, X } from 'lucide-react'
import type { RepoLabel, SuggestedLabel, UntaggedIssue } from '../../api'
import { readableText, relativeTime } from '../../lib/format'
import { safeHttpUrl } from '../../../../lib/safeUrl'
import ShimmerLine from '../../components/ShimmerLine'

import { i18nT } from '../../../../i18n/t'
/** One row of the untagged queue — deliberately ONE line high, so a 50-issue
 * batch is scannable without scrolling past a card per issue.
 *
 * The line carries only what you triage on: age, number, title, the labels
 * staged for it, and ONE action. Suggesting is a queue-level operation (the panel
 * header runs it in batches), and a label you don't want is dropped by clicking
 * its chip — so the row needs no controls of its own beyond Add. There is
 * deliberately no per-row label picker: hand-picking an arbitrary label belongs
 * to the issue detail pane, which already has one.
 *
 * The staged set is the row's only source of truth for what would be written,
 * and nothing here writes to GitHub: the parent owns every mutation so a bulk
 * apply and a single apply go through the same code path.
 *
 * Once applied the row STAYS, as a record of what was just written: its chips go
 * static and the action collapses to an "Added" marker. Removing the row instead
 * made the list jump under the cursor and left no confirmation of what had
 * happened.
 */
export default function UntaggedIssueCard({
  issue, staged, suggestions, analyzed, labels, canWrite, applying, busy, applied, error,
  onToggleSelect, selected, onStage, onApply, suggesting,
}: {
  issue: UntaggedIssue
  /** Label names staged for this issue (the model's proposal minus removals) —
   * or, once applied, the names actually written to GitHub. */
  staged: string[]
  /** The model's proposals, kept for their `reason` text. */
  suggestions: SuggestedLabel[]
  /** True once the issue has been through a generate pass — distinguishes
   * "nothing applies" from "not looked at yet", which read identically otherwise. */
  analyzed: boolean
  labels: RepoLabel[]
  canWrite: boolean
  applying: boolean
  /** True while SOME apply is in flight, anywhere on the page. Applies are not
   * serialized server-side, so only one may run at a time. */
  busy: boolean
  /** True once this row's labels reached GitHub. Freezes the row. */
  applied: boolean
  error?: string
  selected: boolean
  onToggleSelect: () => void
  onStage: (names: string[]) => void
  onApply: () => void
  /** True while a queue-level pass is producing this row's proposal. */
  suggesting: boolean
}) {
  const colorOf = (name: string) => labels.find((l) => l.name === name)?.color ?? '888888'
  const reasonOf = (name: string) => suggestions.find((s) => s.name === name)?.reason ?? ''
  // GitHub-derived, but guarded anyway: it reaches an <a href>, and the scheme
  // check is the one thing that keeps a javascript: URL out of the DOM.
  const href = issue.url ? safeHttpUrl(issue.url) : null
  // Both timestamps are optional on the queue row, and relativeTime renders ''
  // for a falsy/NaN input — so a row with neither simply shows no age.
  const age = relativeTime(new Date(issue.created_at ?? issue.updated_at ?? '').getTime())

  return (
    <div
      // `px-2 -mx-2` matches the suggestion rows in LabelsPanel: the negative
      // margin pulls the row's content edge out to the panel's inner edge, so the
      // Add column lines up with the Create column one panel above. Padding
      // alone left this row inset and the two right edges 10px apart.
      className={`rounded-md border px-2 py-1.5 -mx-2 transition-colors ${
        selected ? 'border-accent bg-accent-subtle/30' : 'border-transparent hover:bg-bg-hover'
      } ${applied ? 'opacity-70' : ''}`}
    >
      <div className="flex items-center gap-2.5 min-w-0">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggleSelect}
          disabled={applied}
          aria-label={i18nT('apps.issueRadar.views.tagging.untaggedIssueCard.select_issue', { number: issue.number })}
          className="flex-shrink-0 accent-accent cursor-pointer disabled:opacity-40"
        />

        {/* Age first — the column you scan down when deciding what to triage. */}
        <span className="flex-shrink-0 w-[72px] text-[12px] text-muted tabular-nums text-right">
          {age}
        </span>

        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noreferrer"
            className="flex-shrink-0 text-[13px] font-bold text-accent tabular-nums hover:underline"
          >
            #{issue.number}
          </a>
        ) : (
          <span className="flex-shrink-0 text-[13px] font-bold text-accent tabular-nums">
            #{issue.number}
          </span>
        )}

        <span className="flex-1 min-w-0 truncate text-[14px] text-text" title={issue.title}>
          {issue.title}
        </span>

        {/* Staged labels — click a chip to drop it. Once applied these are real
          * labels on GitHub, so they stop being removable here. */}
        {staged.length > 0 && (
          <span className="flex-shrink-0 flex items-center gap-1">
            {staged.map((name) => (
              applied ? (
                <span
                  key={name}
                  style={{ backgroundColor: `#${colorOf(name)}`, color: readableText(colorOf(name)) }}
                  className="inline-flex items-center rounded-full px-2 py-1 text-[12px] font-medium max-w-[140px]"
                >
                  <span className="truncate">{name}</span>
                </span>
              ) : (
                <button
                  key={name}
                  onClick={() => onStage(staged.filter((n) => n !== name))}
                  title={reasonOf(name) ? i18nT('apps.issueRadar.views.tagging.untaggedIssueCard.click_to_remove_2', { reason: reasonOf(name) }) : i18nT('apps.issueRadar.views.tagging.untaggedIssueCard.click_to_remove')}
                  className="inline-flex items-center gap-0.5 rounded-full pl-2 pr-1.5 py-1 text-[12px] font-medium cursor-pointer max-w-[140px]"
                  style={{ backgroundColor: `#${colorOf(name)}`, color: readableText(colorOf(name)) }}
                >
                  <span className="truncate">{name}</span>
                  <X size={9} className="flex-shrink-0 opacity-70" />
                </button>
              )
            ))}
          </span>
        )}

        {staged.length === 0 && (
          suggesting ? (
            // Where the chips will land, so the row doesn't jump when they do.
            <span className="flex-shrink-0 flex items-center gap-1">
              <ShimmerLine w="64px" />
              <ShimmerLine w="44px" delay={0.12} />
            </span>
          ) : analyzed ? (
            <span className="flex-shrink-0 text-[12px] text-muted opacity-70">{i18nT('apps.issueRadar.views.tagging.untaggedIssueCard.no_label_fits')}</span>
          ) : null
        )}

        <span className="flex-shrink-0 w-[76px] flex justify-end">
          {applied ? (
            <span className="inline-flex items-center gap-1 text-[12px] text-accent px-1">
              <Check size={12} /> {i18nT('apps.issueRadar.views.tagging.untaggedIssueCard.added')}
            </span>
          ) : (
            <button
              onClick={onApply}
              disabled={!canWrite || busy || staged.length === 0}
              // aria-label, not an sr-only span: every row's button reads "Add",
              // so the number is what makes each one identifiable.
              aria-label={i18nT('apps.issueRadar.views.tagging.untaggedIssueCard.add_labels_to', { number: issue.number })}
              title={canWrite ? i18nT('apps.issueRadar.views.tagging.untaggedIssueCard.add_these_labels_on_github') : i18nT('apps.issueRadar.views.tagging.untaggedIssueCard.read_only_repo_needs_triage_or_push_access')}
              className="inline-flex items-center gap-1 text-[12px] px-2 py-0.5 rounded border border-accent/40 text-accent hover:bg-accent-subtle disabled:opacity-30 disabled:hover:bg-transparent cursor-pointer bg-transparent"
            >
              <Plus size={11} className={applying ? 'animate-pulse' : ''} /> {i18nT('apps.issueRadar.views.tagging.untaggedIssueCard.add')}
            </button>
          )}
        </span>
      </div>

      {error && <div className="text-[12px] text-danger mt-1 ml-[42px]">{error}</div>}
    </div>
  )
}
