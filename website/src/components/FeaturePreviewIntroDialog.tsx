import { useEffect, useState } from 'react'
import { ChevronLeft, ChevronRight, CircleHelp } from 'lucide-react'

import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from './ui/dialog'
import { IconButton, Toggle } from './ui'
import { useIsDark } from '../hooks/useIsDark'
import { i18nT } from '../i18n/t'
import { cn } from '../lib/utils'

/**
 * "See what it looks like" for one Feature Preview: a help button beside the
 * preview's toggle that opens a dialog showing what the feature LOOKS like — a real
 * screenshot or a short GIF of the surface the flag reveals — with one or two
 * sentences on what it does and where it appears once on, and the same switch
 * again in the footer so a reader who is convinced can flip it without closing.
 *
 * Why real captures and not a drawn mockup: the toggle copy already SAYS what
 * the preview holds; what a reader cannot get from words is what to look for
 * afterwards. A mockup would show the design intent, and a preview is by
 * definition the thing whose current state differs from the intent — the whole
 * reason it is held. So `media` is always a capture of the actual page (light
 * and dark variants, the reader's current theme picks one), taken on a running
 * instance, and a preview with NO capture yet gets NO button rather than a
 * dialog with an empty picture frame (`FeaturePreviewIntroButton` renders
 * nothing when `media` is empty). Files live under `public/app-assets/feature-previews/`
 * so they ride the static asset path and never enter a JS chunk, and they are
 * REGENERATED, never redrawn: `scripts/capture-feature-previews.mjs` re-shoots
 * the whole set from a running pod. Run it whenever a previewed surface
 * changes — the captures are the promise this dialog makes.
 *
 * Resolved STRINGS, not catalog keys, on purpose. `check-i18n-keys.mjs` can
 * only verify a key it can see at the call site; `i18nT(intro.summaryKey)`
 * inside this component would be a dynamic site the gate has to ratchet, and a
 * nested `media[i].captionKey` cannot be resolved at all. The preview's card
 * (`FeaturePreviewsSection.tsx`) therefore builds its intro at render time from
 * literal `i18nT('…')` calls and hands the finished copy over.
 *
 * The footer toggle is the SAME control as the card's, not a copy of its state:
 * `checked` / `onChange` are the card's own props passed through, so the two
 * can never disagree, and flipping either one is one `setPreviewFlag` write.
 *
 * Built on `ui/dialog.tsx` (Radix): it owns the focus trap, focus restore,
 * Escape, scroll lock and the open/close animation. `useDialogFocusTrap` is
 * the hand-rolled trap `components/Modal.tsx` still carries and would double
 * up on Radix's here, so it is deliberately not used.
 */

type FeaturePreviewIntroMedia = {
  /** A still capture or a short (≤10 s) looping GIF of an interaction. */
  kind: 'image' | 'gif'
  /** Absolute public paths, one per theme. */
  light: string
  dark: string
  /** What the reader is looking at — doubles as the `alt`. */
  caption: string
}

export type FeaturePreviewIntro = {
  /** What the feature does, one or two sentences. */
  summary: string
  /** Where it appears once the preview is on. */
  whereToFind: string
  /** Empty means "no real capture yet": no button is offered. */
  media: FeaturePreviewIntroMedia[]
}

interface FeaturePreviewIntroButtonProps {
  /** The preview's visible label; names the dialog and the help button. */
  title: string
  intro: FeaturePreviewIntro
  /** The card toggle's own state and setter, passed through unchanged. */
  checked: boolean
  onChange: (on: boolean) => void
}

export function FeaturePreviewIntroButton({ title, intro, checked, onChange }: FeaturePreviewIntroButtonProps) {
  const [open, setOpen] = useState(false)
  if (intro.media.length === 0) return null
  return (
    <>
      <button
        type="button"
        data-testid="feature-preview-intro-button"
        onClick={() => setOpen(true)}
        aria-label={i18nT('components.featurePreviewIntro.learn_more_about', { feature: title })}
        className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent bg-transparent border-none cursor-pointer px-0 py-1 hover:underline"
      >
        <CircleHelp size={13} className="lucide-inline" />
        {i18nT('components.featurePreviewIntro.learn_more')}
      </button>
      <FeaturePreviewIntroDialog open={open} onOpenChange={setOpen} title={title} intro={intro} checked={checked} onChange={onChange} />
    </>
  )
}

interface FeaturePreviewIntroDialogProps extends FeaturePreviewIntroButtonProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

function FeaturePreviewIntroDialog({ open, onOpenChange, title, intro, checked, onChange }: FeaturePreviewIntroDialogProps) {
  const isDark = useIsDark()
  const [index, setIndex] = useState(0)
  // Start from the first capture every time the dialog opens; a reader who
  // paged to the last one and comes back expects the same first frame.
  useEffect(() => { if (open) setIndex(0) }, [open])
  const media = intro.media[Math.min(index, intro.media.length - 1)]
  const many = intro.media.length > 1
  const descId = 'feature-preview-intro-desc'
  const toggleLabelId = 'feature-preview-intro-toggle-label'

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent maxWidth={720} aria-describedby={descId}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        <DialogBody className="flex flex-col gap-3">
          {media && (
            <figure className="m-0">
              <div className="relative overflow-hidden rounded-lg border border-border bg-bg-accent">
                {/* `key` on the theme+index pair forces a fresh <img>, so a GIF
                    restarts from its first frame when paged to rather than
                    resuming mid-loop from a cached decode.
                    Height-capped to the viewport: the captures are full-window
                    frames (3:2), and at the 90vh the dialog allows, one at full
                    width pushed the "where you'll see it" sentence under a fold
                    that showed no scrollbar (UX review on #9519) — the clause
                    that explains the feature was the part a reader lost. The
                    picture scales down and centres; the copy stays in view. */}
                <img
                  key={`${isDark ? 'dark' : 'light'}-${index}`}
                  src={isDark ? media.dark : media.light}
                  alt={media.caption}
                  className="mx-auto block h-auto max-h-[42vh] w-auto max-w-full"
                  loading="eager"
                  decoding="async"
                />
                {/* EVERY capture wears a corner badge, not just the GIF: a real
                    "Turn off" button photographed inside the frame sits inches
                    from the real "Turn this preview on" switch below it, and a
                    reader who clicks the picture gets silence. The badge is the
                    one cue that says "this is a picture" before that click. */}
                <span className="absolute right-2 top-2 rounded bg-bg/80 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[.04em] text-muted">
                  {media.kind === 'gif'
                    ? i18nT('components.featurePreviewIntro.gif_badge')
                    : i18nT('components.featurePreviewIntro.screenshot_badge')}
                </span>
              </div>
              {/* Paging controls sit in the caption row, OUTSIDE the picture: an
                  arrow overlaid on a screenshot of the app reads as part of the
                  screenshot (a reviewer cataloguing the frame found only the
                  dots), so the frame stays a pure capture and the controls stay
                  controls. */}
              {/* The caption WRAPS rather than truncates: it carries the one
                  sentence that says what the reader is looking at, and a cut
                  caption would hide exactly the clause that disambiguates. */}
              <figcaption className="mt-1.5 flex items-start justify-between gap-3 text-[12px] leading-relaxed text-muted">
                <span className="min-w-0">{media.caption}</span>
                {many && (
                  <span className="flex shrink-0 items-center gap-1">
                    <IconButton
                      aria-label={i18nT('components.featurePreviewIntro.previous')}
                      onClick={() => setIndex(i => (i - 1 + intro.media.length) % intro.media.length)}
                      className="focus-ring"
                    >
                      <ChevronLeft size={14} />
                    </IconButton>
                    <span className="flex items-center gap-1" role="tablist" aria-label={i18nT('components.featurePreviewIntro.captures')}>
                      {intro.media.map((m, i) => (
                        <button
                          key={m.light}
                          type="button"
                          role="tab"
                          aria-selected={i === index}
                          aria-label={i18nT('components.featurePreviewIntro.capture_n_of_m', { n: i + 1, m: intro.media.length })}
                          onClick={() => setIndex(i)}
                          className={cn('h-1.5 w-1.5 rounded-full transition-colors', i === index ? 'bg-accent' : 'bg-border-strong hover:bg-muted')}
                        />
                      ))}
                    </span>
                    <IconButton
                      aria-label={i18nT('components.featurePreviewIntro.next')}
                      onClick={() => setIndex(i => (i + 1) % intro.media.length)}
                      className="focus-ring"
                    >
                      <ChevronRight size={14} />
                    </IconButton>
                  </span>
                )}
              </figcaption>
            </figure>
          )}
          <DialogDescription id={descId} className="text-[13px] text-text">
            {intro.summary}
          </DialogDescription>
          <div className="flex flex-col gap-0.5">
            <span className="text-[11px] font-semibold uppercase tracking-[.04em] text-muted">
              {i18nT('components.featurePreviewIntro.where_to_find')}
            </span>
            <p className="m-0 text-[12px] leading-relaxed text-muted">{intro.whereToFind}</p>
          </div>
        </DialogBody>
        <DialogFooter className="justify-between">
          {/* The label follows the switch: "Turn this preview on" beside a
              switch that is already on reads as a contradiction, so an ON
              switch is labelled with its state instead of an instruction. */}
          <span id={toggleLabelId} className="min-w-0 mr-4 text-[13px] font-semibold text-text">
            {checked
              ? i18nT('components.featurePreviewIntro.is_on')
              : i18nT('components.featurePreviewIntro.turn_on')}
          </span>
          <Toggle checked={checked} onChange={onChange} label={title} describedBy={toggleLabelId} />
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
