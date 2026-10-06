import { memo } from 'react'
import { Archive, Square } from 'lucide-react'

import ErrorNotice from '../../components/ErrorNotice'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { isSystemNoticeKind } from '../../lib/systemNotice'
import CompactionKeepLine from './CompactionKeepLine'
import { decisionStripFieldOf, readCompactionKeepRecord } from './decisionRecord'
import MarkdownDisclosureCard from './MarkdownDisclosureCard'
import NoticeCard from './NoticeCard'
import type { ChatMessage } from '../../types'

/**
 * The gateway appends every compaction outcome as an ASSISTANT-role row tagged
 * `kind="compaction"`. The tag is shared by every writer that wants the row
 * skipped by the last-real-message scans (see lib/systemNotice.ts), so the
 * content is one of a closed set of shapes, not one:
 *
 *   ✅ Conversation compacted: <summary>     kiro-cli's /compaction/status
 *   ✅ Conversation compacted.               no summary (Claude backend, manual)
 *   ❌ Compaction failed: <reason>           first failures of a streak
 *   ❌ Compaction has failed Nx in a row …   the streak's collapsed notice
 *   ⚠️ Compaction timed out.                 the manual /compact wait
 *   ⚠ Auto-compact failed at N% — …          threshold auto-compact (state.py)
 *   🔄 Auto-compacted at N%.                  threshold auto-compact, success
 *   ♻️ This session was recycled …           watchdog recycle notice
 *   ⏳ This turn has produced nothing …       stuck-turn notice
 *   ⏹ Compaction at N% was ended by Stop …  a Stop landed on the /compact turn
 *
 * Only the first is a document worth folding: on kiro-cli `<summary>` is the
 * backend's whole context digest (Goal / Status / Technical / Decisions …),
 * routinely several kilobytes. Drawn as a bubble it reads as a reply the
 * assistant never made and pushes the real conversation off-screen.
 *
 * Parsed on the frontend, at render time, so no history row needs rewriting:
 * a transcript persisted by any gateway since the notice existed gets the card.
 *
 * One row of that set can also carry a `meta.decisions_strip` record from the
 * `compaction.keep` shadow point, and every shape above can: the scoring runs before
 * the compaction picks an arm, so the record outlives whichever arm it took. That
 * record draws one line UNDER whatever the card drew (`CompactionKeepLine`), saying
 * what an oracle WOULD have kept — nothing was kept, and the compaction is identical
 * whatever it answered.
 */
export type CompactionStatus = 'completed' | 'failed' | 'notice'

export interface ParsedCompaction {
  status: CompactionStatus
  /** `completed`: the context summary the backend shipped; empty when it shipped none. */
  summary: string
  /** `failed`: the reason with the ❌/⚠ lead stripped (ErrorNotice carries severity). */
  reason: string
  /** `notice`: the row with a known status glyph (🔄 ♻️ ⏳) stripped; NoticeCard
   *  parses its own ℹ️/⚠️/⛔ lead from what remains. */
  text: string
  /** `notice`: the lead was ⏹, the user's own forced Stop ended the compaction. */
  stopCaused?: boolean
}

// `✅` is U+2705 (no variation selector in practice, tolerate one). The colon
// is optional: the no-summary form ends in a period instead.
const COMPLETED_RE = /^\s*\u2705\uFE0F*\s*Conversation compacted\s*[:.]?\s*/u
// Failure shapes. Two writers, two glyphs: chat_utils / chat_runner lead a
// failure with ❌ (U+274C) — "Compaction failed: …", "Compaction has failed
// Nx …" — and the manual /compact timeout and state.py's threshold path lead
// with ⚠ (U+26A0, with or without the variation selector) — "Compaction timed
// out.", "Auto-compact failed at N% …". All four are the outcome of something
// that FAILED, so all four are errors however they are dressed (AUTOSDE
// errors-use-error-notice decides by where the value comes from). The lookahead
// pins the failure wording, so a ⚠ row that is merely a warning stays a notice.
const FAILED_LEAD_RE =
  /^\s*(?:\u274C|\u26A0)\uFE0F*\s*(?=(?:Compaction|Auto-compact)\b[^\n]*?\b(?:failed|timed out)\b)/u
// Status glyphs the state.py writers bake into the text (🔄 auto-compacted,
// ♻️ recycled, ⏳ stuck turn). NoticeCard strips only its own ℹ️/⚠️/⛔ set and
// keeps any other leading glyph as content, so these would render as raw emoji
// icons beside the lucide glyph (AUTOSDE no-emoji-as-icons). Stripped here,
// where the writer set is known; the tone stays info — none is a warning.
// ⏳ is U+23F3 (HOURGLASS WITH FLOWING SAND), the glyph state.py writes — not
// U+231B (⌛ HOURGLASS). ⏹ is U+23F9, the lead on the "compaction ended by
// Stop" notice (#14841): a status, not a failure, so it lands here.
const STATUS_LEAD_RE = /^\s*(?:\u{1F504}|\u267B|\u23F3|\u23F9)\uFE0F*\s*/u
// The one lead that names an actor rather than the app: the notice for a
// compaction the user's own forced Stop ended. It keeps the info tone but takes
// a stop-square glyph, so the two restart notices (this and ♻️'s) are told apart
// at a glance and the reader is not left guessing which restart was theirs.
const STOP_LEAD_RE = /^\s*\u23F9\uFE0F*\s*/u

export function parseCompactionNotice(content: string): ParsedCompaction {
  const raw = content ?? ''
  const done = COMPLETED_RE.exec(raw)
  if (done) return { status: 'completed', summary: raw.slice(done[0].length).trim(), reason: '', text: raw }
  const failed = FAILED_LEAD_RE.exec(raw)
  if (failed) return { status: 'failed', summary: '', reason: raw.slice(failed[0].length).trim(), text: raw }
  return {
    status: 'notice',
    summary: '',
    reason: '',
    text: raw.replace(STATUS_LEAD_RE, ''),
    stopCaused: STOP_LEAD_RE.test(raw),
  }
}

/** The tag lives on `kind` for a row that arrived live over the websocket and
 *  on `meta.kind` for one reloaded from disk — the same split systemNotice.ts /
 *  completedTurns.ts already read. */
function noticeKindOf(m: Pick<ChatMessage, 'kind' | 'meta'>): string | undefined {
  return m.kind ?? (m.meta?.kind as string | undefined)
}

/** Any assistant-role system notice the gateway injects (compaction, session
 *  reload). One predicate shared with the last-real-message scans, so a kind
 *  those scans skip can never fall through to the reply bubble here. */
export function isSystemNoticeRow(m: Pick<ChatMessage, 'role' | 'kind' | 'meta'>): boolean {
  return m.role === 'assistant' && isSystemNoticeKind(noticeKindOf(m))
}

/**
 * Collapsed system card for a compaction notice.
 *
 * Shares NoticeCard / RecoveryCard's visual grammar — same ring, background,
 * radius, px-3/py-2 step, 13px/leading-5 type and 13px lucide icon — so the
 * gateway-authored rows read as one family in the transcript. A completed
 * compaction is a routine event, so the header is muted and the summary is
 * folded behind a chevron; expanding it renders the summary as markdown inside
 * a capped, internally scrolling region so a 20 KB summary cannot take the
 * viewport. No chevron and no button when there is nothing to expand.
 *
 * A failure is not folded: it renders through ErrorNotice (the shared error
 * surface, AUTOSDE errors-use-error-notice), because a compaction that failed
 * is the outcome of something that FAILED — that covers the ❌ rows AND the
 * ⚠-led "Auto-compact failed" / "Compaction timed out" rows, since the rule
 * classifies by where the text comes from, not by its glyph. The agent hand-off
 * is deliberately off (see the failed branch). Every other shape the tag
 * carries — the threshold auto-compact success line, the recycle and stuck-turn
 * notices — is status, not a document and not a failure, and draws on
 * NoticeCard.
 *
 * Expansion is per-row disclosure state, not persisted (same as RecoveryCard).
 */
const CompactionCard = memo(function CompactionCard({ content, disclosureKey, keepRecord }: { content: string; disclosureKey?: string;
  /** Raw `decisions_strip` field off the notice row, validated here. Absent renders nothing. */
  keepRecord?: unknown }) {
  // memo() boundary rendering i18nT() strings: subscribe so a language switch repaints.
  useLanguageGeneration()
  const parsed = parseCompactionNotice(content)
  // Validated here rather than at the render site, the rule `decisionRecord.ts` states:
  // this payload names what left the machine and what came back, and a line printing a
  // shape nobody checked would describe a measurement nobody made.
  const keep = readCompactionKeepRecord(keepRecord)

  // Appended BELOW whatever shape the row took, rather than inside one of the three
  // branches. All three are reachable with a record on them — the threshold success
  // notice, the recycle notice and the ⚠-led auto-compact failure — because the
  // scoring runs before the compaction picks an arm, so a record exists whichever arm
  // it then took. Putting the line in one branch would drop it on the other two.
  const withKeep = (body: JSX.Element) =>
    keep === null ? body : (
      <div className="self-center w-full max-w-full min-w-0 flex flex-col" data-testid="compaction-card-host">
        {body}
        <CompactionKeepLine record={keep} />
      </div>
    )

  if (parsed.status === 'failed') {
    // No `askAgent` hand-off, deliberately. This row is drawn inside every
    // transcript host — the main chat, but also ChatPane (Crew Members DM),
    // SideChat and ChatEmbed, whose composers hold an unsent draft in local
    // state. The hand-off navigates to /chat and unmounts that subtree, so a
    // draft typed while a compaction failed would be destroyed (see the
    // `askAgent` contract in components/ErrorNotice.tsx: leave it off next to
    // any editable field whose contents are not yet saved). The reason is still
    // rendered through ErrorNotice so it keeps the shared error chrome and the
    // journal lookup by message.
    return withKeep(
      <ErrorNotice
        message={parsed.reason}
        className="self-center w-full max-w-full min-w-0 animate-scale-in"
        testId="compaction-card-error"
      />,
    )
  }
  if (parsed.status === 'notice') {
    return withKeep(<NoticeCard content={parsed.text} icon={parsed.stopCaused ? Square : undefined} />)
  }

  const hasSummary = parsed.summary.length > 0
  return withKeep(
    <MarkdownDisclosureCard
      icon={Archive}
      title={i18nT('pages.chat.compactionCard.title')}
      detail={({ expanded, hasBody }) => {
        if (!hasBody) return i18nT('pages.chat.compactionCard.no_summary')
        return expanded ? null : i18nT('pages.chat.compactionCard.summary_hint')
      }}
      body={hasSummary ? <MarkdownRenderer content={parsed.summary} /> : undefined}
      disclosureKey={disclosureKey}
      testId="compaction-card"
      bodyTestId="compaction-card-body"
      status={parsed.status}
    />,
  )
})

export default CompactionCard

/**
 * Registry entry body for every assistant-role system notice: the compaction
 * kinds fold into CompactionCard, the session-reload confirmation is a plain
 * NoticeCard. One renderer for the whole `SYSTEM_NOTICE_KINDS` set, so adding a
 * kind to the scans' skip list and forgetting the transcript cannot leave it
 * painting as a reply.
 */
export function SystemNoticeRow({ message, disclosureKey }: { message: ChatMessage; disclosureKey?: string }) {
  if (noticeKindOf(message) === 'compaction') {
    return (
      <CompactionCard
        content={message.content}
        disclosureKey={disclosureKey}
        // The same field the assistant and user receipts ride, read through the same
        // helper so a live frame and a row reloaded from history are treated alike.
        keepRecord={decisionStripFieldOf(message)}
      />
    )
  }
  return <NoticeCard content={message.content} />
}
