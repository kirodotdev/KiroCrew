/**
 * Verification entry for the auto-nudge REDACTED-PROJECTION surface.
 *
 * Mounts the REAL `AutoNudgePopover` on a loop whose served `message` is a
 * FABRICATED masked goal, so the three states this PR adds can be photographed:
 * the notice explaining why the text reads `[REDACTED: ...]`, the
 * overwrite-confirm row that arms when the user edits that masked text, and the
 * notice shown when a save kept the stored goal.
 *
 * The fixture is invented on purpose: these frames are permanent and
 * outward-facing, and credential-shaped text is this PR's whole subject, so the
 * harness must never be seeded from a real loop store.
 *
 * `?theme=dark|light` selects the palette. The driver asserts each state first.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRoot } from 'react-dom/client'

import AutoNudgePopover from '../src/components/AutoNudgePopover'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'kiro-light' : 'kiro-dark'

document.documentElement.setAttribute('data-theme', theme)
initI18n('en')

/**
 * A masked goal, exactly as the backend's egress scrub would serve it: the
 * operator's own words with the credential-shaped run replaced in place. The
 * key id here is the documented AWS example value, not a real credential.
 */
const REDACTED_GOAL = 'deploy the release with [REDACTED: aws-access-key-id] then post the summary'

const loop = {
  id: 'loop-capture-1',
  slot_key: 'chat-1-123',
  message: REDACTED_GOAL,
  message_redacted: true,
  idle_secs: 300,
  max_cycles: 0,
  cycle_count: 4,
  active: true,
  last_fire_ts: 0,
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    {/* Inline width: capture/ sits outside Tailwind's content globs, so an
        arbitrary-value class written here would never compile. */}
    <div
      data-capture-root
      className="bg-bg text-text p-6"
      style={{ width: 520, minHeight: 560 }}
    >
      <AutoNudgePopover
        open
        onOpenChange={() => {}}
        slotKey="chat-1-123"
        loop={loop as never}
        onChange={() => {}}
      />
    </div>
  </QueryClientProvider>,
)
