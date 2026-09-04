/**
 * What the tool-details panel shows for a completion whose only output carrier
 * is a `rawOutput` object that is NOT kiro-cli's `items[]` envelope (issue #7799).
 *
 * The panel itself is unchanged by that fix — it reads `meta.output` and has
 * always been correct. What changed is upstream: `_build_tool_result_event` used
 * to return None for such a frame, dropping the whole EVENT_TOOL_RESULT, so
 * `meta.output` was written as "". The two states below are therefore the SAME
 * component fed the before and after value of one string.
 *
 * Frames 1 and 2 use a verbatim captured KAS frame (`fetch_cloud_config`, the
 * first tool call of every KAS session): title, absent rawInput and
 * `rawOutput: {kind: "notEnabled", retracted: false}` are exactly what came off
 * the wire. Frame 3 is COMPOSED and says so on its face — real captured
 * `rawInput` from the same session paired with a rawOutput-only completion — to
 * show the two-section control the issue title names.
 *
 * WHY INLINE STYLES: `tailwind.config.js` scans ['./index.html',
 * './src/**\/*.{ts,tsx}'] — `capture/` is not in that glob, so a Tailwind class
 * written here emits no rule. Only the real component out of `src/` is mounted.
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

// ── Verbatim from the captured KAS session ──
const KAS_TITLE = 'Fetching your cloud config'
// json.dumps of the captured rawOutput, i.e. what the fixed parser emits.
const KAS_RAW_OUTPUT = '{"kind": "notEnabled", "retracted": false}'
// Captured rawInput of the same session's run_command call.
const KAS_INPUT = JSON.stringify({ command: 'echo KASPROBE123', run_in_background: false }, null, 2)
const KAS_RUN_OUTPUT = JSON.stringify(
  { output: 'KASPROBE123\n', exitCode: 0, message: 'Output:\nKASPROBE123\n\n\nExit Code: 0' },
  null,
  2,
)

const fmtTime = () => '12:58'

function Frame({ label, note, children }: { label: string; note?: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <div style={{ font: '11px ui-monospace, monospace', letterSpacing: '.06em', color: 'var(--muted)' }}>
        {label}
      </div>
      {note ? (
        <div style={{ font: '10px ui-monospace, monospace', color: 'var(--warn)', maxWidth: 700 }}>
          {note}
        </div>
      ) : null}
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
    <Frame label="1 · BEFORE · captured KAS fetch_cloud_config · result frame dropped, meta.output = &quot;&quot;">
      <div data-state="before">
        <ToolDetails
          purpose=""
          pillLabel={KAS_TITLE}
          toolName={KAS_TITLE}
          input=""
          output=""
          auto
          pending={false}
          ts={1}
          hasEntry
          fmtTime={fmtTime}
          barColor="color-mix(in srgb, var(--ok) 70%, transparent)"
          layoutId="cap-7799-before"
          flush
        />
      </div>
    </Frame>

    <Frame label="2 · AFTER · same frame · rawOutput serialised into meta.output">
      <div data-state="after">
        <ToolDetails
          purpose=""
          pillLabel={KAS_TITLE}
          toolName={KAS_TITLE}
          input=""
          output={KAS_RAW_OUTPUT}
          auto
          pending={false}
          ts={1}
          hasEntry
          fmtTime={fmtTime}
          barColor="color-mix(in srgb, var(--ok) 70%, transparent)"
          layoutId="cap-7799-after"
          flush
        />
      </div>
    </Frame>

    <Frame
      label="3 · AFTER · a rawOutput-only completion that also carried rawInput"
      note="COMPOSED FRAME — captured rawInput and captured rawOutput from the same KAS session, paired to show the two-section control. The captured run_command completion also sent a content block, so it was never affected by this defect."
    >
      <div data-state="after-both">
        <ToolDetails
          purpose=""
          pillLabel="Run Command"
          toolName="Run Command"
          input={KAS_INPUT}
          output={KAS_RUN_OUTPUT}
          auto
          pending={false}
          ts={1}
          hasEntry
          fmtTime={fmtTime}
          barColor="color-mix(in srgb, var(--ok) 70%, transparent)"
          layoutId="cap-7799-both"
          flush
        />
      </div>
    </Frame>
  </div>,
)
