// "Export all outputs" bundle builder (#32982). Composes every enabled agent's
// output into ONE self-contained HTML file a person can share: the notes agents'
// Markdown and the sketch agents' diagrams in a single artifact, named after the
// meeting. It is a snapshot, same as the single exports.
//
// WHY ONE HTML FILE, NOT A ZIP. The frontend ships no zip library, and the app's
// own conventions prefer a library already in the tree over a new dependency.
// More decisively, `saveBlob` (utils/download.ts) only reaches disk on an
// Android in-app WebView by handing the bytes to the gateway, which accepts
// `text/html`, `text/plain`, `application/json`, `image/svg+xml` and `image/png`
// only (dashboard/handlers/artifact_download.py) — an `application/zip` blob
// would silently fail to save there. A single `text/html` file saves on every
// download path.
//
// ISOLATION. Each sketch is at LEAST as isolated here as in its own single
// export: its self-contained document (from `buildSketchExport` — scrubbed of the
// model's scripts, its own `<meta>` CSP, the vendored Mermaid runtime inlined) is
// embedded in a sandboxed null-origin `<iframe srcdoc>`. The sketch's scripts are
// NEVER inlined into the bundle page; they stay behind that nested sandbox, so a
// prompt-injected diagram cannot read or exfiltrate from the bundle document.
// Notes go in as their Markdown TEXT inside a `<pre>` — never as raw HTML — which
// is the same bytes the single `.md` export preserves.
//
// NO USER/LLM TEXT IN HTML TEMPLATES. Every agent name, note, sketch document,
// title and label is set through `textContent` or `setAttribute`, and the
// finished document is serialized from the DOM — the same construction as
// `buildSketchSrcdoc` in sketchSrcdoc.ts, for the same reason: the
// `frontend-security` rule forbids interpolating user/LLM content into an HTML
// template string even when escaped, and the typed DOM APIs cannot be broken out
// of. Only the fixed page chrome (DOCTYPE, the static CSS) is literal.

import { buildSketchExport } from './sketchSrcdoc'
import type { AgentDef } from '../api'

/** Frame chrome for the bundle page itself. Deliberately small: a readable
 *  default face, section spacing, and a border so an embedded sketch iframe
 *  reads as its own panel. A fixed literal — it carries no user content. */
const BUNDLE_CSS =
  "body { margin: 0 auto; max-width: 900px; padding: 24px 16px; " +
  "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; " +
  "color: #1a1a1a; background: #fff; } " +
  'h1 { font-size: 1.5rem; margin: 0 0 4px; } ' +
  '.meeting-date { color: #666; margin: 0 0 24px; } ' +
  'section { margin: 0 0 32px; } ' +
  'h2 { font-size: 1.15rem; margin: 0 0 8px; border-bottom: 1px solid #e5e5e5; padding-bottom: 4px; } ' +
  'pre { white-space: pre-wrap; word-wrap: break-word; margin: 0; ' +
  "font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.9rem; } " +
  '.sketch-frame { width: 100%; height: 480px; border: 1px solid #e5e5e5; border-radius: 6px; } ' +
  '.no-output { color: #888; font-style: italic; margin: 0; }'

/** `<YYYY-MM-DD>` in the viewer's time zone, or `''` for an absent/invalid date.
 *  Matches `exportFileName`'s day formatting. */
function dayLabel(date: string | undefined): string {
  const when = date ? new Date(date) : null
  if (!when || Number.isNaN(when.getTime())) return ''
  return `${when.getFullYear()}-${String(when.getMonth() + 1).padStart(2, '0')}-${String(when.getDate()).padStart(2, '0')}`
}

/**
 * Build the "export all outputs" bundle as one self-contained HTML string.
 *
 * @param agents  The enabled agents, in menu order (the caller already orders
 *   them notes-first, then sketches — the same order `ExportMenuItems` uses).
 * @param outputs The EFFECTIVE output per agent id (the agent's text, or the
 *   user's saved edit) — the same map the single exports read.
 * @param title   The meeting title, shown as the bundle heading.
 * @param date    ISO 8601 start (else creation) time, shown under the title.
 * @param noOutputLabel The already-translated marker for an enabled agent with
 *   no output. Passed in resolved so this builder holds no user-facing copy: the
 *   `i18nT` lookup stays at the gated call site (see `ExportMenuItems.tsx`).
 *
 * An agent with no output is kept and marked, so the bundle is a complete record
 * of what the meeting had enabled.
 */
export async function buildMeetingBundle(
  agents: AgentDef[],
  outputs: Record<string, string>,
  title: string,
  date: string | undefined,
  noOutputLabel: string,
): Promise<string> {
  // A detached document assembled with typed DOM APIs: no user or LLM byte ever
  // enters an HTML template string (see the file header).
  const doc = document.implementation.createHTMLDocument('')
  doc.documentElement.setAttribute('lang', 'en')

  const charset = doc.createElement('meta')
  charset.setAttribute('charset', 'utf-8')
  doc.head.appendChild(charset)

  const viewport = doc.createElement('meta')
  viewport.setAttribute('name', 'viewport')
  viewport.setAttribute('content', 'width=device-width, initial-scale=1')
  doc.head.appendChild(viewport)

  const titleEl = doc.createElement('title')
  titleEl.textContent = title
  doc.head.appendChild(titleEl)

  const style = doc.createElement('style')
  style.textContent = BUNDLE_CSS
  doc.head.appendChild(style)

  const h1 = doc.createElement('h1')
  h1.textContent = title
  doc.body.appendChild(h1)

  const day = dayLabel(date)
  if (day) {
    const dateEl = doc.createElement('p')
    dateEl.className = 'meeting-date'
    dateEl.textContent = day
    doc.body.appendChild(dateEl)
  }

  for (const agent of agents) {
    const output = outputs[agent.id] ?? ''
    const section = doc.createElement('section')

    const h2 = doc.createElement('h2')
    h2.textContent = agent.name
    section.appendChild(h2)

    if (!output) {
      // Agents with no output are kept and clearly marked (same "the export is
      // there, nothing in it yet" idea as the disabled single item). The label
      // arrives translated from the call site.
      const marker = doc.createElement('p')
      marker.className = 'no-output'
      marker.textContent = noOutputLabel
      section.appendChild(marker)
    } else if (agent.widget_type === 'html') {
      // The sketch's OWN single-export document, embedded behind a sandboxed
      // null-origin iframe. Its scripts live inside that nested document, never
      // on the bundle page. The document goes in via `srcdoc` as a typed
      // attribute, not interpolated into markup.
      const sketchDoc = await buildSketchExport(output)
      const frame = doc.createElement('iframe')
      frame.className = 'sketch-frame'
      frame.setAttribute('sandbox', 'allow-scripts')
      frame.setAttribute('srcdoc', sketchDoc)
      section.appendChild(frame)
    } else {
      // Notes as their Markdown TEXT — same bytes as the single `.md` export,
      // set through `textContent`, never inserted as raw HTML.
      const pre = doc.createElement('pre')
      pre.textContent = output
      section.appendChild(pre)
    }

    doc.body.appendChild(section)
  }

  // Serialize the assembled DOM. The only literal is the fixed DOCTYPE prefix;
  // every user/LLM value was set as a node property above.
  return `<!DOCTYPE html>\n${doc.documentElement.outerHTML}`
}
