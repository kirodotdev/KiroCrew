/**
 * ScheduleTemplateGallery — a modal listing every schedule preset grouped
 * into category sections. Clicking a card opens the standard create flow
 * seeded from that preset (the user reviews and saves before anything runs).
 *
 * Sections and their order/labels come from PRESET_CATEGORIES; empty
 * categories are skipped so the gallery grows automatically as presets are
 * added — no counts or titles are hardcoded here.
 */
import Clickable from './Clickable'
import Modal from './Modal'
import { GitPullRequestArrow } from 'lucide-react'
import { SCHEDULE_PRESETS, PRESET_CATEGORIES, type SchedulePreset } from '../utils/schedulePresets'
import { i18nT } from '../i18n/t'
import { formatCadence } from '../utils/scheduleCadence'

interface Props {
  open: boolean
  onClose: () => void
  onPick: (p: SchedulePreset) => void
}

export default function ScheduleTemplateGallery({ open, onClose, onPick }: Props) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={i18nT('components.scheduleTemplateGallery.title')}
      maxWidth={860}
    >
      <div className="flex flex-col gap-6">
        <p className="text-[13px] text-muted m-0 -mt-1">
          {i18nT('components.scheduleTemplateGallery.subtitle')}
        </p>

        {PRESET_CATEGORIES.map(cat => {
          const presets = SCHEDULE_PRESETS.filter(p => p.category === cat.id)
          if (presets.length === 0) return null
          return (
            <section key={cat.id} aria-label={cat.label}>
              <h4 className="text-left text-[12px] font-medium uppercase tracking-[.04em] text-muted mb-3">
                {cat.label}
              </h4>
              <div className="grid gap-4 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3">
                {presets.map(p => (
                  <Clickable
                    key={p.id}
                    onClick={() => { onPick(p); onClose() }}
                    aria-label={i18nT('components.scheduleTemplateGallery.use_template', { title: p.title })}
                    className="group flex flex-col items-start gap-2 text-left px-5 py-5 rounded-[20px] bg-card border border-border hover:border-accent/50 hover:bg-bg-hover transition-colors focus-ring cursor-pointer"
                  >
                    <span className="text-accent shrink-0">{p.icon}</span>
                    <span className="text-[15px] font-semibold text-text-strong leading-snug">{p.title}</span>
                    <span className="text-[13px] leading-[18px] text-muted">{p.description}</span>
                    <span className="flex items-center gap-2 mt-auto pt-1">
                      <span className="text-[12px] text-muted/80 font-medium">{formatCadence(p.prefill)}</span>
                      {p.writes && (
                        <span className="inline-flex items-center gap-1 text-[11px] font-medium text-warn-fg bg-warn-subtle rounded-full px-2 py-0.5" title={i18nT('pages.schedulePage.writes_badge_tooltip')}>
                          <GitPullRequestArrow size={11} aria-hidden="true" />
                          {i18nT('pages.schedulePage.writes_to_your_repos')}
                        </span>
                      )}
                    </span>
                  </Clickable>
                ))}
              </div>
            </section>
          )
        })}
      </div>
    </Modal>
  )
}
