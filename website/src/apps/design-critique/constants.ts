import {
  AlertTriangle, AlertCircle, Wrench, Sparkle,
} from 'lucide-react'
import type { SevInfo, SeverityKey, Report, Screen, Blocked } from './types'

// The core agent, not a bundled one. A builtin's declared `agents` are never
// registered (see the CRITIC note in prompts.ts), so naming `design-critic` here
// would fail every request; the persona travels with the prompt instead.
export const AGENT = 'kirocrew'
export const HKEY = 'dc-history-v1'
export const JOBKEY = 'dc-current-job'
// Every slot we create, until we've deleted it. Only ONE job record is kept, so
// without this list a slot abandoned at the scoping step is forgotten forever and
// lingers in the session list as a stray "design-critic" conversation.
export const SLOTSKEY = 'dc-open-slots-v1'
// Slots with a run currently in flight. Distinct from SLOTSKEY (every slot we
// ever opened, which is what the reaper sweeps): the reaper must spare EVERY
// live run, not just the one in JOBKEY, or starting a second critique orphans
// the first. Entries carry a timestamp so a tab closed mid-run cannot keep a
// slot un-reapable forever.
export const LIVEKEY = 'dc-live-runs-v1'
export const LIVE_TTL_MS = 30 * 60 * 1000

export const RAIL_W = '440px'
export const MAX_SCREENS = 20
// Backstop only — a job past this is stuck. Measured against Date.now(), never a
// tick count (see hooks/poll logic).
export const HARD_CAP_MS = 15 * 60 * 1000

export const SEV: Record<SeverityKey, SevInfo> = {
  catastrophe: { label: 'Catastrophe', rank: 0, color: '#e5484d', icon: AlertTriangle },
  major:       { label: 'Major',       rank: 1, color: '#f5a623', icon: AlertCircle },
  minor:       { label: 'Minor',       rank: 2, color: '#e2c541', icon: Wrench },
  cosmetic:    { label: 'Cosmetic',    rank: 3, color: 'var(--muted, #8a8f98)', icon: Sparkle },
}
export const sevOf = (s: string | undefined): SevInfo => SEV[(s as SeverityKey)] || SEV.cosmetic

export const KIND_LABEL: Record<string, string> = {
  figma: 'Figma file', repo: 'repo', local: 'local code', url: 'running app',
}

// The stages a critique actually goes through, in SOP order. The last one is
// driven by a real signal (the critic has started replying); the rest advance on
// elapsed time, so they read as "what I'm doing now", not a verified percentage.
export const STAGES: Array<{ at: number; label: string }> = [
  { at: 0,  label: 'Getting real pixels to look at' },
  { at: 8,  label: 'Reading the screens' },
  { at: 22, label: 'Checking hierarchy, contrast, and content' },
  { at: 45, label: 'Weighing it against the task' },
  { at: 70, label: 'Double-checking each finding' },
]
export const WRITING_STAGE = { label: 'Writing up the critique' }
export const SCAN_STAGES: Array<{ at: number; label: string }> = [
  { at: 0, label: 'Finding the screens' },
  { at: 6, label: 'Working out which ones I can render' },
  { at: 14, label: 'Grouping them into likely flows' },
]

// "I couldn't get in" is a different problem from "I got in and found nothing".
// Each cause has a different fix, so name it and pre-load the way forward.
export const BLOCKED: Record<string, Blocked> = {
  'no-access': {
    say: 'I couldn’t open that repo. It’s either private (and I have no credentials for it) or it doesn’t exist — GitHub returns the same error for both.',
    fix: 'local', hint: 'If you have it checked out, give me the folder path instead.',
    auth: {
      lead: 'Git access is set up per machine, not per app — do it once and every private repo works, here and everywhere else. In your terminal:',
      cmds: ['gh auth login', '# or, to use the macOS keychain:', 'git config --global credential.helper osxkeychain'],
      tail: 'Then come back and press Try again. I never ask for or store a token — anything pasted into this app would end up in a saved transcript.',
    },
  },
  'not-found': {
    say: 'That repo doesn’t exist at that URL.',
    fix: 'retype', hint: 'Check the owner and name, or give me a local folder path.',
  },
  'figma-app-missing': {
    say: 'I can’t reach Figma — the desktop app isn’t running, or I don’t have the Figma tools available.',
    fix: 'shots', hint: 'Open the file in Figma desktop and try again, or export the frames as PNGs and drop them here.',
  },
  'figma-file-closed': {
    say: 'Figma is running, but that file isn’t open.',
    fix: 'retry', hint: 'Open it in Figma desktop, then try again.',
  },
  'figma-no-permission': {
    say: 'Your Figma account can’t view that file.',
    fix: 'shots', hint: 'Ask for access, or export the frames as PNGs and drop them here.',
    auth: {
      lead: 'Figma access is per account, and I read whatever the desktop app has open — I have no login of my own. To fix it:',
      cmds: ['1. Ask the file owner to share it with your Figma account', '2. Open it in Figma desktop', '3. Come back and press Try again'],
      tail: 'If access isn’t coming, exporting the frames as PNGs works just as well — I critique pixels either way.',
    },
  },
  other: { say: 'I couldn’t get into that.', fix: 'shots', hint: 'Screenshots are the quickest way around it.' },
}

export const KIND_WAIT: Record<string, string> = {
  figma: 'Opening the Figma file and listing its frames…',
  repo: 'Cloning the repo and listing its routes…',
  local: 'Scanning the code for routes…',
  url: 'Loading the page and following its links…',
}

// The built-in example is a real 4-screen checkout flow, captured from a real
// (small) site, so "See an example" demonstrates flow mode rather than one screen.
// Every box below is MEASURED from the rendered page, not estimated. Images live
// under the dashboard's public assets (copied by the build).
const SAMPLE = (name: string) => '/app-assets/design-critique/samples/' + name + '.png'
export const SAMPLE_SCREENS: Screen[] = [
  { step: 1, label: 'Cart', url: SAMPLE('1-cart') },
  { step: 2, label: 'Shipping', url: SAMPLE('2-shipping') },
  { step: 3, label: 'Payment', url: SAMPLE('3-payment') },
  { step: 4, label: 'Confirmation', url: SAMPLE('4-confirm') },
]
export const SAMPLE_REPORT: Report = {
  overallRead: 'The checkout gets the job done, but it loses momentum in the middle — the main button changes colour and label at every step, and nothing tells you how far along you are.',
  health: 'Promising, needs work',
  tally: { catastrophe: 0, major: 2, minor: 2, cosmetic: 1 },
  screens: SAMPLE_SCREENS.map(s => ({ step: s.step, label: s.label, path: '' })),
  findings: [
    { severity: 'major', scope: 'flow', steps: [1, 2, 3, 4], title: 'The primary button changes colour and label on every step', category: 'Consistency', location: 'Bottom action row, all four steps', evidence: 'Continue (dark) → Next step (blue) → Pay $130 (green). Three colours, three labels, and it moves from right-aligned to left-aligned on step 2.', fix: 'Consider one primary style for the whole flow and one verb pattern — "Continue to shipping", "Continue to payment", "Pay $130" — keeping its position fixed.', rules: ['Nielsen: consistency & standards', 'Gestalt: common fate', 'Fitts’s Law: moving target'], box: null },
    { severity: 'major', scope: 'flow', steps: [1, 2, 3, 4], title: 'No sense of progress — how many steps are left?', category: 'System status', location: 'All four steps', evidence: 'Nothing on any screen says step 2 of 4, so the length of the flow is unknowable until it ends.', fix: 'Consider a small step indicator in the header, or naming the next step in the button.', rules: ['Nielsen: visibility of system status'], box: null },
    { severity: 'minor', scope: 'flow', steps: [1, 2], title: 'No way back until step 3', category: 'User control', location: 'Steps 1 and 2', evidence: 'Payment is the first screen with a Back button; before that the only exit is the browser.', fix: 'You might add a consistent Back on every step after the first.', rules: ['Nielsen: user control & freedom'], box: null },
    { severity: 'minor', scope: 'screen', steps: [2], title: 'Required fields aren’t marked', category: 'Content', location: 'Address form labels', evidence: 'Only Phone is annotated, as "Optional" — so the other five read as ambiguous rather than required.', fix: 'Mark the optional one and leave the rest plain, or mark required fields explicitly. Pick one convention.', rules: ['Content Design: clear', 'Nielsen: error prevention'], box: { x: 0.182, y: 0.211, w: 0.636, h: 0.02 } },
    { severity: 'cosmetic', scope: 'screen', steps: [4], title: 'The confirmation is a dead end', category: 'Usability', location: 'Below the delivery estimate', evidence: 'The final screen has no onward action — no order detail, no continue shopping.', fix: 'You might offer one clear next step so the flow ends on an action rather than a full stop.', rules: ['Peak-end rule', 'Shneiderman: closure'], box: { x: 0.156, y: 0.074, w: 0.689, h: 0.514 } },
  ],
  keep: ['Single-column forms with generous field spacing — easy to move through.', 'The cart states the total plainly, with no surprise fees later in the flow.'],
  couldNotSee: ['Validation and error states (none were reachable in the captured pages).'],
}
