/**
 * OutlineView — the engine's `specs/outline.md` as a storyboard: numbered
 * chapters, and one card per slide carrying its claim (the future headline),
 * what it says (`body`), what it shows (`visual`) and where the facts come from
 * (`evidence`).
 *
 * Read-only on purpose. The outline is the spec the user reviews slide by slide
 * before composition, so its structure has to be legible at a glance — a raw
 * nested bullet list buries the claim under its own sub-items. Editing stays in
 * the chat, where the agent owns the file. The raw Markdown is one toggle away
 * for anything the grammar does not cover.
 */
import { useMemo, useState } from 'react'
import { FileText, LayoutGrid } from 'lucide-react'
import SegmentedControl from '../../components/SegmentedControl'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { i18nT } from '../../i18n/t'
import { hasTbd, parseOutline, type OutlineSlide } from './outline'

type OutlineMode = 'storyboard' | 'markdown'

function SubItem({ label, value }: { label: string; value: string }) {
  if (!value) return null
  return (
    <div className="text-[12px] leading-relaxed">
      <span className="text-muted font-medium mr-1.5">{label}</span>
      <span className={hasTbd(value) ? 'text-warn' : 'text-muted-strong'}>{value}</span>
    </div>
  )
}

function SlideCard({ slide, number }: { slide: OutlineSlide; number: number }) {
  const incomplete = hasTbd(slide.message) || hasTbd(slide.body) || hasTbd(slide.visual) || hasTbd(slide.evidence)
  return (
    <article
      className={`flex flex-col gap-2 rounded-lg border bg-card p-3 min-w-0 ${
        incomplete ? 'border-warn' : 'border-border'
      }`}
      data-testid="outline-slide"
    >
      <div className="flex items-baseline gap-2 min-w-0">
        <span className="text-[12px] font-semibold text-accent tabular-nums shrink-0">
          {String(number).padStart(2, '0')}
        </span>
        <span className="text-[11px] font-mono text-muted truncate">{slide.slug}</span>
      </div>
      <h4 className="text-sm font-medium text-text leading-snug">{slide.message || slide.slug}</h4>
      {slide.body && (
        <p className={`text-sm leading-relaxed ${hasTbd(slide.body) ? 'text-warn' : 'text-muted-strong'}`}>
          {slide.body}
        </p>
      )}
      {(slide.visual || slide.evidence) && (
        <div className="flex flex-col gap-1 pt-2 mt-auto border-t border-border">
          <SubItem label={i18nT('apps.pptxMaker.outlineView.visual')} value={slide.visual} />
          <SubItem label={i18nT('apps.pptxMaker.outlineView.evidence')} value={slide.evidence} />
        </div>
      )}
    </article>
  )
}

export default function OutlineView({ markdown }: { markdown: string }) {
  const [mode, setMode] = useState<OutlineMode>('storyboard')
  const outline = useMemo(() => parseOutline(markdown), [markdown])
  // An outline with no `- [slug]` lines is not in the engine's grammar (an older
  // deck, or one still being written): the storyboard would be empty, so the
  // Markdown is the honest rendering.
  const structured = outline.slideCount > 0
  const effective: OutlineMode = structured ? mode : 'markdown'

  let running = 0
  return (
    <div className="flex flex-col gap-4 min-w-0">
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex-1 min-w-0">
          {outline.title && <h3 className="text-[15px] font-semibold text-text truncate">{outline.title}</h3>}
          {structured && (
            <div className="text-[12px] text-muted">
              {i18nT('apps.pptxMaker.pptxMakerPage.slide_count', { count: outline.slideCount })}
            </div>
          )}
        </div>
        {structured && (
          <SegmentedControl
            segments={[
              {
                key: 'storyboard',
                label: i18nT('apps.pptxMaker.outlineView.storyboard'),
                icon: <LayoutGrid className="lucide-inline" />,
              },
              {
                key: 'markdown',
                label: i18nT('apps.pptxMaker.outlineView.markdown'),
                icon: <FileText className="lucide-inline" />,
              },
            ]}
            value={effective}
            onChange={(next) => setMode(next as OutlineMode)}
            layoutId="pptx-outline-mode"
            collapse={false}
          />
        )}
      </div>

      {effective === 'markdown' ? (
        <div className="max-w-3xl">
          <MarkdownRenderer content={markdown} />
        </div>
      ) : (
        outline.chapters.map((chapter, index) => {
          const chapterNumber = outline.chapters.slice(0, index + 1).filter((c) => c.section).length
          return (
            <section key={`${index}-${chapter.section?.title ?? ''}`} className="flex flex-col gap-2">
              {chapter.section && (
                <div className="flex items-baseline gap-2">
                  <span className="text-[12px] font-semibold text-muted tabular-nums">
                    {String(chapterNumber).padStart(2, '0')}
                  </span>
                  <h4 className="text-sm font-semibold text-text">{chapter.section.title}</h4>
                  <span className="text-[12px] text-muted">
                    {i18nT('apps.pptxMaker.pptxMakerPage.slide_count', { count: chapter.slides.length })}
                  </span>
                  <div className="flex-1 border-t border-border self-center" aria-hidden="true" />
                </div>
              )}
              {chapter.prose.map((p, i) => (
                <p key={i} className="text-sm text-muted-strong">{p.text}</p>
              ))}
              <div className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(min(100%,240px),1fr))]">
                {chapter.slides.map((slide) => {
                  running += 1
                  return <SlideCard key={slide.slug} slide={slide} number={running} />
                })}
              </div>
            </section>
          )
        })
      )}
    </div>
  )
}
