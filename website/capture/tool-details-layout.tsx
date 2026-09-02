/**
 * Geometry evidence for the tool-call details panel.
 *
 * Three states, because the complaint and the fix both live in how they differ:
 *   1. pending, input only  — the state a tool sits in while awaiting approval,
 *      and the one the reported screenshot showed. The section control has
 *      nothing to switch between here.
 *   2. done, both sections  — the only state where a section toggle is a real
 *      choice, so it must still be one.
 *   3. long display title   — `toolName` carries kiro-cli's display TITLE, and
 *      for a shell call that is the whole command line. This is the input that
 *      used to consume the meta row on its own.
 *
 * WHY THE WRAPPER USES INLINE STYLES. `tailwind.config.js` scans
 * `['./index.html', './src/**\/*.{ts,tsx}']` — `capture/` is NOT in that glob, so
 * a Tailwind class written HERE emits no rule and would render unstyled. Only
 * the real component out of `src/` is mounted; everything this file adds around
 * it is inline `style=` against theme variables.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n'
import { ToolDetails } from '../src/pages/chat/ToolDetails'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SHELL_TITLE =
  'Running: git -P status --short --branch && echo "---" && git -P log --oneline -n 5'

const SHELL_INPUT = JSON.stringify(
  {
    command: 'git -P status --short --branch && echo "---" && git -P log --oneline -n 5',
    working_dir: '/Volumes/workplace/KiroClaw',
    __thinking_purpose: '检查仓库当前状态和最近提交',
  },
  null,
  2,
)

const SHELL_OUTPUT = '## main...origin/main\n---\n88a51fa0e fix(ci): tell the advisory review lanes\n'

// A JSON result, so the frame exercises the section toggle and the render-mode
// toggle at the same time — the only state where both controls are on the strip.
const JSON_OUTPUT = JSON.stringify({ branch: 'main', ahead: 0, dirty: false }, null, 2)

const fmtTime = () => '12:58'

function Frame({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <div
        style={{
          font: '11px ui-monospace, monospace',
          letterSpacing: '.06em',
          color: 'var(--muted)',
        }}
      >
        {label}
      </div>
      <div
        style={{
          background: 'var(--card)',
          border: '1px solid var(--border)',
          borderRadius: 8,
          padding: '10px 12px',
        }}
      >
        {children}
      </div>
    </div>
  )
}

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <div
    data-capture-root
    style={{
      background: 'var(--bg)',
      color: 'var(--text)',
      display: 'flex',
      flexDirection: 'column',
      gap: 18,
      padding: 20,
      width: 760,
      font: '13px system-ui, sans-serif',
    }}
  >
    <Frame label="1 · pending · input only">
      <div data-state="pending">
        <ToolDetails
          purpose="检查仓库当前状态和最近提交"
          pillLabel="检查仓库当前状态和最近提交"
          toolName={SHELL_TITLE}
          input={SHELL_INPUT}
          output=""
          auto={false}
          pending
          ts={1}
          hasEntry
          fmtTime={fmtTime}
          barColor="color-mix(in srgb, var(--warn) 70%, transparent)"
          layoutId="cap-pending"
          flush
        />
      </div>
    </Frame>

    <Frame label="2 · done · both sections">
      <div data-state="both">
        <ToolDetails
          purpose="检查仓库当前状态和最近提交"
          pillLabel="检查仓库当前状态和最近提交"
          toolName={SHELL_TITLE}
          input={SHELL_INPUT}
          output={SHELL_OUTPUT}
          auto
          pending={false}
          ts={1}
          hasEntry
          fmtTime={fmtTime}
          barColor="color-mix(in srgb, var(--ok) 70%, transparent)"
          layoutId="cap-both"
          flush
        />
      </div>
    </Frame>

    <Frame label="3 · raw-label mode · JSON result · both controls on the strip">
      <div data-state="rawlabel">
        <ToolDetails
          purpose="检查仓库当前状态和最近提交"
          pillLabel={SHELL_TITLE}
          toolName={SHELL_TITLE}
          input={SHELL_INPUT}
          output={JSON_OUTPUT}
          auto={false}
          pending={false}
          ts={1}
          hasEntry
          fmtTime={fmtTime}
          barColor="color-mix(in srgb, var(--ok) 70%, transparent)"
          layoutId="cap-raw"
          flush
        />
      </div>
    </Frame>
    <Frame label="4 · 266px (phone transcript) · both controls must wrap, not clip">
      {/* 266px is the real tightest surface, not a round number: a 320px phone
          minus the transcript row's px-5 either side (40) minus this panel's rail
          and pl-3 (14). The two capsules measure 121 + 133 with an 8px gap = 262,
          so they wrap below 274 — and the row's disclosure wrapper clips overflow,
          which is what silently ate the render-mode toggle before `flex-wrap`. */}
      <div data-state="narrow" style={{ width: 266, overflow: 'hidden' }}>
        <ToolDetails
          purpose="检查仓库当前状态和最近提交"
          pillLabel={SHELL_TITLE}
          toolName={SHELL_TITLE}
          input={SHELL_INPUT}
          output={JSON_OUTPUT}
          auto={false}
          pending={false}
          ts={1}
          hasEntry
          fmtTime={fmtTime}
          barColor="color-mix(in srgb, var(--ok) 70%, transparent)"
          layoutId="cap-narrow"
          flush
        />
      </div>
    </Frame>
    <Frame label="5 · compact ghost · pending, input only · stays bare">
      {/* The approval bar's mirror. Its `compact` contract says it must not grow,
          and with only input there is nothing to switch between, so it carries no
          section control at all -- not even a naming label, which would say
          nothing here: a ghost only ever mirrors a pending call. The render-mode
          toggle still appears, because a JSON input is worth reading raw before
          approving it. */}
      <div data-state="ghost">
        <ToolDetails
          purpose="检查仓库当前状态和最近提交"
          pillLabel="检查仓库当前状态和最近提交"
          toolName={SHELL_TITLE}
          input={SHELL_INPUT}
          output=""
          auto={false}
          pending
          ts={1}
          hasEntry
          fmtTime={fmtTime}
          barColor="color-mix(in srgb, var(--warn) 70%, transparent)"
          layoutId="cap-ghost"
          compact
        />
      </div>
    </Frame>
  </div>,
)
