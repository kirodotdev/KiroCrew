/**
 * Sample data for `/xr?view=land&fixture=1` (the room of pages, `/xr?fixture=1`, shows its crew too): tests,
 * screenshots and the tour video.
 *
 * It is written in the SAME shapes the gateway serves (roster rows, slots,
 * approvals, question cards, notifications), so the page folds it with the
 * same code as live data. Nothing here is read from a real crew.
 */
import type { MemberGoals, MemberRosterRow } from '../../api/client'
import type { PendingQuestions } from '../../api/client/approvals'
import { ICON_BG_PALETTE } from '../../lib/avatarPoses'
import type { ChatMessage, ChatSlot, CronJob, Notification } from '../../types'
import type { XrBriefing, XrPageState } from './glance'
import type { XrCoordinatorApproval } from './model'
import type { XrLine } from './panels'

/** The fixture's clock: every "updated 5 minutes ago" is measured from here. */
export const FIXTURE_NOW_MS = Date.UTC(2026, 10, 15, 17, 30, 0)
const NOW = FIXTURE_NOW_MS / 1000
const iso = (minAgo: number) => new Date(FIXTURE_NOW_MS - minAgo * 60_000).toISOString()

const bot = (name: string, slug: string, avatar: unknown, minAgo: number, last: string, running = false): MemberRosterRow => ({
  name, slug, slot_key: `member-${slug}`, running, display_name: name, last_active_ts: NOW - minAgo * 60, last_message: last,
  has_goal: slug !== 'sage', avatar,
} as MemberRosterRow)

const icon = (n: number, bg: number) => ({ kind: 'icon', pose: `pose-${n}`, bg: ICON_BG_PALETTE[bg % ICON_BG_PALETTE.length] })

const MEMBERS: MemberRosterRow[] = [
  bot('Atlas', 'atlas', icon(3, 0), 2, 'The release notes draft is ready. Two flaky tests still block the tag.', true),
  bot('Juno', 'juno', {}, 6, 'I can start the rollout once you pick the first region.'),
  bot('Mira', 'mira', icon(7, 3), 48, 'Weekly numbers are in. Sign-ups are up 8% on last week.'),
  bot('Otto', 'otto', {}, 1, 'Reading the incident timeline now.', true),
  bot('Pico', 'pico', icon(10, 5), 95, 'I stopped: the staging database did not answer.'),
  bot('Sage', 'sage', {}, 260, 'Nothing new since this morning.'),
]

const slot = (key: string, o: Partial<ChatSlot>): ChatSlot => ({ key, title: key, running: false, ...o } as ChatSlot)
const dm = (slug: string, o: Partial<ChatSlot> = {}): ChatSlot => slot(`member-${slug}`, { mode: 'member', agent: MEMBERS.find(m => m.slug === slug)?.name, ...o })
const work = (key: string, of: string, title: string, o: Partial<ChatSlot>): ChatSlot => slot(key, { title, mate_root: `member-${of}`, ...o })

const LONG_COMMAND = 'find build/cache -type f -mtime +14 -not -name "*.lock" -print0 | xargs -0 -n 50 rm -f && find build/cache -type d -empty -delete && du -sh build/cache && '
  + 'rsync -a --delete build/cache/ /mnt/shared/cache-mirror/ && echo "cache trimmed and mirrored" | tee -a logs/cache-maintenance.log && date -u +%FT%TZ >> logs/cache-maintenance.log'

const SLOTS: ChatSlot[] = [
  dm('atlas', { running: true, last_message: MEMBERS[0].last_message, last_activity_ts: iso(2) }),
  work('w-atlas-notes', 'atlas', 'Release notes for 0.15', { running: true, last_message: 'Drafting the "What changed" section from 41 merged pull requests.', last_activity_ts: iso(1) }),
  work('w-atlas-flaky', 'atlas', 'Flaky test sweep', {
    pending_approval: true,
    pending_approval_info: { origin: 'native', request_id: 'req-flaky-1', tool: 'shell', tool_kind: 'execute', tool_input: JSON.stringify({ command: 'npm run test -- --shard 3/8 --retry 0' }), tool_purpose: 'Run shard 3 again without retries to see the real failures' },
    last_message: 'I need to run shard 3 again without retries.', last_activity_ts: iso(4),
  }),
  work('w-atlas-cache', 'atlas', 'Trim the build cache', { last_message: 'The cache is 38 GB. I can trim files older than two weeks.', last_activity_ts: iso(12) }),
  dm('juno', { needs_input: true, last_message: MEMBERS[1].last_message, last_activity_ts: iso(6) }),
  dm('mira', { last_message: MEMBERS[2].last_message, last_activity_ts: iso(48) }),
  work('w-mira-report', 'mira', 'Weekly growth report', { last_message: 'The report is saved. Three charts changed since last week.', last_activity_ts: iso(50) }),
  dm('otto', { running: true, last_message: MEMBERS[3].last_message, last_activity_ts: iso(1) }),
  work('w-otto-incident', 'otto', 'Incident review: checkout latency', { running: true, last_message: 'Comparing the deploy times with the latency graph.', last_activity_ts: iso(1) }),
  dm('pico', { interrupted: true, last_message: MEMBERS[4].last_message, last_activity_ts: iso(95) }),
  dm('sage', { last_message: MEMBERS[5].last_message, last_activity_ts: iso(260) }),
  slot('chat-7f3a9c21', { title: 'chat-7f3a9c21', last_message: 'A scratch chat that no Bot owns.', last_activity_ts: iso(300) }),
]

const APPROVALS: XrCoordinatorApproval[] = [
  { id: 'coord-cache-1', instance: 'i-1', tool: 'shell', tool_input: JSON.stringify({ command: LONG_COMMAND }), tool_purpose: 'Trim the build cache and mirror it', slot: 'w-atlas-cache', ts: NOW - 12 * 60 },
]

const QUESTIONS: PendingQuestions[number][] = [
  { ask_id: 'ask-region-1', slot: 'member-juno', ts: NOW - 6 * 60, questions: [{ header: 'Rollout', question: 'Which region goes first?', options: [{ label: 'Frankfurt' }, { label: 'Oregon' }, { label: 'Tokyo' }] }, { header: 'Timing', question: 'Start today or after the release?', options: [{ label: 'Today' }, { label: 'After the release' }] }] },
]

/** The ask that "arrives" when the fixture's event key is pressed. */
const LATE_SLOT = work('w-otto-rollback', 'otto', 'Roll back the checkout deploy', {
  pending_approval: true,
  pending_approval_info: { origin: 'native', request_id: 'req-rollback-1', tool: 'shell', tool_kind: 'execute', tool_input: JSON.stringify({ command: 'deploy rollback checkout --to 2026.11.14-3' }), tool_purpose: 'Roll checkout back to yesterday\u2019s build' },
  last_message: 'Latency rose right after the 16:40 deploy. I want to roll it back.', last_activity_ts: iso(0),
})

const NOTES: Notification[] = [
  { kind: 'cron', title: 'Nightly backup finished', body: 'All 14 volumes copied. Took 22 minutes.', ts: iso(190), key: 'n1' },
  { kind: 'member', title: 'Mira saved the weekly report', body: 'Three charts changed since last week.', ts: iso(50), key: 'n2', slot: 'w-mira-report' },
]

const job = (id: string, agent: string, name: string, hoursAhead: number, enabled = true): CronJob => ({
  id, name, agent, message: '', enabled, schedule: '', last_status: 'ok', next_run_ts: enabled ? NOW + hoursAhead * 3600 : null,
})

const JOBS: CronJob[] = [
  job('j-atlas-sweep', 'Atlas', 'Nightly test sweep', 5),
  job('j-atlas-digest', 'Atlas', 'Release digest', 15),
  job('j-mira-weekly', 'Mira', 'Weekly growth report', 70),
  job('j-pico-refresh', 'Pico', 'Staging data refresh', 0, false),
]

const said = (who: XrLine['who'], minAgo: number, text: string): XrLine => ({ who, text, ts: NOW - minAgo * 60 })

const THREADS: Record<string, XrLine[]> = {
  'member-atlas': [
    said('you', 190, 'Morning. Where does the 0.15 release stand?'),
    said('bot', 189, 'Three things are left: the release notes, two flaky tests in shard 3, and the changelog check. I am on the notes now.'),
    said('you', 120, 'Keep the notes short. People read the first screen only.'),
    said('bot', 119, 'Understood. I will lead with the three changes people asked for and link the rest.'),
    said('tool', 60, 'read CHANGELOG.md'),
    said('bot', 58, 'The changelog check passes. I also found that the cache on the build host is 38 GB, so I asked to trim it.'),
    said('you', 20, 'What blocks the tag right now?'),
    said('bot', 19, 'Only the two flaky tests. I asked to run shard 3 again without retries, so we see the real failures and not the retried ones.'),
    said('bot', 2, 'The release notes draft is ready. Two flaky tests still block the tag.'),
  ],
  'w-atlas-notes': [
    said('bot', 30, 'I am drafting the release notes for 0.15 from the merged pull requests.'),
    said('tool', 12, 'gh pr list --state merged --limit 60'),
    said('bot', 1, 'Drafting the "What changed" section from 41 merged pull requests.'),
  ],
  'w-atlas-flaky': [
    said('bot', 9, 'Shard 3 failed twice this week and passed on retry both times.'),
    said('bot', 4, 'I need to run shard 3 again without retries.'),
  ],
  'w-atlas-cache': [said('bot', 12, 'The cache is 38 GB. I can trim files older than two weeks.')],
  'member-juno': [
    said('you', 40, 'Plan the rollout of the new sign-in page.'),
    said('bot', 38, 'Plan: one region first, watch errors for an hour, then the rest. All three regions are ready.'),
    said('bot', 6, 'I can start the rollout once you pick the first region.'),
  ],
  'member-mira': [
    said('bot', 48, 'Weekly numbers are in. Sign-ups are up 8% on last week. Churn is flat. Support tickets fell from 61 to 44.'),
  ],
  'w-mira-report': [said('bot', 50, 'The report is saved. Three charts changed since last week.')],
  'member-otto': [
    said('you', 14, 'Checkout felt slow after lunch. Can you look?'),
    said('bot', 13, 'Yes. I will line up the deploys with the latency graph.'),
    said('bot', 1, 'Reading the incident timeline now.'),
  ],
  'w-otto-incident': [said('bot', 1, 'Comparing the deploy times with the latency graph.')],
  'w-otto-rollback': [said('bot', 0, 'Latency rose right after the 16:40 deploy. I want to roll it back.')],
  'member-pico': [
    said('bot', 97, 'Starting the staging data refresh.'),
    said('tool', 96, 'psql -h staging-db -c "select 1"'),
    said('bot', 95, 'I stopped: the staging database did not answer.'),
  ],
  'member-sage': [said('bot', 260, 'Nothing new since this morning.')],
}

type Goals = Pick<MemberGoals, 'goals' | 'key_results' | 'reports'>
const kr = (id: string, text: string, target: string, values: string[], minAgo: number) => ({
  id, text, target, current: values[values.length - 1], updated_at: NOW - minAgo * 60, goal_ids: ['g1'],
  readings: values.map((value, i) => ({ value, at: NOW - (values.length - i) * 86_400 })),
})
const goal = (text: string) => ({ id: 'g1', number: 1, text, created_at: NOW - 20 * 86_400, updated_at: NOW - 86_400, updated_by: 'user' as const })

const GOALS: Record<string, Goals> = {
  atlas: {
    goals: [goal('Ship release 0.15 this week with no known P0 bug.')],
    key_results: [kr('k1', 'Release checklist items done', '12', ['6', '7', '9'], 30), kr('k2', 'Open P0 bugs', '0', ['4', '3', '2'], 30), kr('k3', 'Flaky tests left', '0', ['2', '2'], 30)],
    reports: [{ kind: 'weekly', slug: 'r1', title: 'Release week 46 report', at: NOW - 2 * 86_400 }],
  },
  juno: { goals: [goal('Roll out the new sign-in page to every region.')], key_results: [kr('k1', 'Regions live', '3', ['0', '0'], 400)], reports: [] },
  mira: {
    goals: [goal('Grow weekly sign-ups to 2,000.')],
    key_results: [kr('k1', 'Weekly sign-ups', '2000', ['1490', '1610'], 48), kr('k2', 'Support tickets this week', '40', ['61', '44'], 48)],
    reports: [{ kind: 'weekly', slug: 'r2', title: 'Weekly growth report', at: NOW - 50 * 60 }],
  },
  otto: { goals: [goal('Keep checkout fast: p95 under 400 ms.')], key_results: [kr('k1', 'Checkout p95 (ms)', '400', ['380', '610'], 5)], reports: [] },
  pico: { goals: [goal('Refresh staging data every night.')], key_results: [kr('k1', 'Nights refreshed this week', '7', ['4', '4'], 95)], reports: [] },
}

const BRIEFINGS: Record<string, XrBriefing> = {
  atlas: { text: 'Release 0.15 is one step from the tag: two flaky tests block it.', updated_ts: NOW - 120 },
  mira: { text: 'Growth is on track. Sign-ups are up 8% and tickets are down.', updated_ts: NOW - 48 * 60 },
  otto: { text: 'Checkout p95 rose to 610 ms after the 16:40 deploy.', updated_ts: NOW - 60 },
}

const PAGES: Record<string, XrPageState> = { atlas: 'live', juno: 'live', mira: 'stale', otto: 'live', pico: 'error', sage: 'empty' }

/** What the voice stand-in "hears": the tour has no speech service to call. */
export const FIXTURE_VOICE_LINE = 'Summarise what changed since this morning.'
/** What the scripted Bot of the sample data answers to a spoken sentence: one line, then its plan as a numbered list. */
export const FIXTURE_VOICE_REPLY = 'On it: three steps, about two minutes.\n1. Read what changed since this morning\n2. Check each change against the release list\n3. Write the summary'

export interface XrFixture {
  members: MemberRosterRow[]
  slots: ChatSlot[]
  approvals: XrCoordinatorApproval[]
  questions: PendingQuestions[number][]
  notices: Notification[]
  unread: string[]
  jobs: CronJob[]
}

const ASKED_LINE = 'How are sign-ups today?'
const REPLY_LINE = 'Sign-ups passed 1,700 today. The Friday campaign is the cause.'

/**
 * The sample crew. `late` adds the ask that arrives during the tour. `reply` plays one exchange with Mira the
 * way the roster reports it: at 1 the person has asked, so the chat's last line is the person's own and Mira
 * works; at 2 Mira has answered.
 */
export function xrFixture(late: boolean, reply = 0): XrFixture {
  const line = reply === 1 ? ASKED_LINE : reply >= 2 ? REPLY_LINE : ''
  const turn = { running: reply === 1, last_message: line }
  const members = line ? MEMBERS.map(m => (m.slug === 'mira' ? { ...m, ...turn } : m)) : MEMBERS
  const slots = line ? SLOTS.map(s => (s.key === 'member-mira' ? { ...s, ...turn } : s)) : SLOTS
  return { members, slots: late ? [...slots, LATE_SLOT] : slots, approvals: APPROVALS, questions: QUESTIONS, notices: NOTES, unread: ['w-mira-report'], jobs: JOBS }
}

const ROLE = { you: 'user', bot: 'assistant', tool: 'tool' } as const

/** A sample thread as the gateway would serve it, so it takes the same path to the panel as a real one. */
export const fixtureMessages = (slotKey: string): ChatMessage[] =>
  (THREADS[slotKey] ?? []).map(l => ({ role: ROLE[l.who], content: l.text, cls: '', ts: new Date(l.ts * 1000).toISOString() }))
/**
 * A sample session's messages for its Bot's words (`useSays.ts`, with `words=1`), as `GET /api/chat/slots/{slot}`
 * gives them: Pico's cut turn ends with the error row the gateway writes for a turn that failed
 * (`slot.append("error", str(exc), "msg msg-err")`, `chat_turn/app_agent_binding.py:220`).
 */
export const fixtureWords = (slotKey: string, alsoCut = ''): ChatMessage[] => [
  ...fixtureMessages(slotKey),
  // `alsoCut`: one more session whose turn was cut (`fixtureCrew.ts`, `failed=<slug>`), with the same error row.
  ...(slotKey === 'member-pico' || (alsoCut && slotKey === alsoCut) ? [{ role: 'error', content: '\u274C The staging database did not answer: connection timed out after 30 s.', cls: 'msg msg-err', ts: new Date((NOW - 95 * 60 + 5) * 1000).toISOString() }] : []),
]
export const fixtureGoals = (slug: string): Goals | null => GOALS[slug] ?? null
export const fixtureBriefing = (slug: string): XrBriefing | null => BRIEFINGS[slug] ?? null
export const fixturePage = (slug: string): XrPageState => PAGES[slug] ?? 'empty'
