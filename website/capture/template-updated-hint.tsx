/**
 * Isolated capture entry for the Schedule detail dialog's "template updated"
 * hint.
 *
 * The hint element mirrors SchedulePage's real markup (classes, testid, icon,
 * i18n key, dismiss control). The `assert` gate builds a job whose SAVED
 * template snapshot differs from the live preset's current prompt and checks
 * the REAL `templateUpdate` returns a result -- so the still documents the
 * attributable "the template moved" state, not a state the shipped code would
 * not produce.
 *
 * Theme via query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Info, X } from 'lucide-react'

import { initI18n } from '../src/i18n'
import { i18nT } from '../src/i18n/t'
import { SCHEDULE_PRESETS, templateUpdate, presetCanonicalPrompt } from '../src/utils/schedulePresets'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'kiro-light' : 'kiro-dark'
document.documentElement.setAttribute('data-theme', theme)

initI18n('en')

const sample = SCHEDULE_PRESETS[0]
// Saved snapshot differs from the live canonical prompt -> the template moved.
const movedJob = { source_preset: sample.id, source_template_prompt: presetCanonicalPrompt(sample.id) + ' (older template wording)' }
const update = templateUpdate(movedJob)
if (!update) {
  throw new Error('capture gate: templateUpdate should report a moved template')
}

function Scene() {
  return (
    <div className="bg-bg p-6 text-text">
      <div
        data-capture-root
        className="flex flex-col gap-4 rounded-xl border border-border-strong bg-card p-5"
        style={{ width: 560 }}
      >
        <div className="text-[15px] font-semibold text-text-strong">{sample.prefill.name}</div>
        {/* EXACT shipped hint markup (see SchedulePage.tsx TemplateUpdatedNotice). */}
        <div
          className="flex items-start gap-2 px-3 py-2 rounded-lg bg-accent-subtle text-[12.5px] text-muted"
          role="note"
          data-testid="schedule-template-updated-notice"
        >
          <Info size={14} className="shrink-0 mt-0.5" aria-hidden="true" />
          <span className="flex-1">{i18nT('pages.schedulePage.template_updated_notice', { name: update.title })}</span>
          <button
            type="button"
            className="shrink-0 -mr-1 -mt-0.5 p-0.5 rounded text-muted hover:text-text hover:bg-accent-hover"
            aria-label={i18nT('pages.schedulePage.template_updated_dismiss')}
          >
            <X size={13} aria-hidden="true" />
          </button>
        </div>
        {/* The "Message" field below -- the notice's copy uses the same word so
            a reader can tell the two refer to the same thing. */}
        <label className="text-[12px] text-muted">Message</label>
        <textarea
          className="rounded-lg border border-border bg-input px-3 py-2 text-[13px] text-text"
          rows={3}
          readOnly
          value={sample.prefill.message}
        />
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Scene />)
