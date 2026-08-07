import { useState, useRef, useEffect, useId } from 'react'
import { createPortal } from 'react-dom'

import { i18nT } from '../i18n/t'

/** Tiny ? button that shows a tooltip on click. Portal-rendered to escape overflow clipping. */
export default function InfoTip({ text, placement = 'auto' }: { text: string; placement?: 'auto' | 'top' }) {
  const [open, setOpen] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)
  const tipRef = useRef<HTMLDivElement>(null)
  const tipId = useId()

  useEffect(() => {
    if (!open) return
    const h = (e: MouseEvent) => {
      if (btnRef.current?.contains(e.target as Node)) return
      if (tipRef.current?.contains(e.target as Node)) return
      setOpen(false)
    }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [open])

  const pos = () => {
    if (!btnRef.current) return { top: 0, left: 0 }
    const r = btnRef.current.getBoundingClientRect()
    const tipW = 300, tipH = 120
    if (placement === 'top') {
      // Centered above the button, bottom-anchored so height doesn't matter.
      let left = r.left + r.width / 2 - tipW / 2
      left = Math.max(8, Math.min(left, window.innerWidth - tipW - 8))
      if (r.top > tipH + 12) return { bottom: window.innerHeight - r.top + 6, left }
      return { top: r.bottom + 6, left } // not enough room above → fall below
    }
    let top = r.top
    let left = r.right + 6
    if (left + tipW > window.innerWidth) left = r.left - tipW - 6
    if (top + tipH > window.innerHeight) top = window.innerHeight - tipH - 8
    return { top, left }
  }

  return (
    <>
      <button
        ref={btnRef}
        /* The visible glyph is a bare "?", which assistive technology announces as
           "question mark" — a control with no discoverable purpose. The NAME is a
           short generic verb phrase and the tip text is attached as the
           DESCRIPTION instead, which matters for two reasons: a name is read
           whenever the button is reached, so a paragraph-length one is unusable;
           and an accessible name is the handle every other control is found by, so
           naming a help toggle after its own prose makes it collide with real
           actions whose labels happen to appear in that prose. */
        aria-label={i18nT('components.infoTip.more_information')}
        aria-expanded={open}
        aria-describedby={open ? tipId : undefined}
        onClick={(e) => { e.stopPropagation(); setOpen(!open) }}
        className="w-4 h-4 rounded-full border border-border text-muted text-[10px] hover:text-text hover:border-text/30 transition-all leading-none cursor-pointer flex items-center justify-center shrink-0"
        title={text}
      >?</button>
      {open && createPortal(
        <div
          ref={tipRef}
          id={tipId}
          role="tooltip"
          className="fixed z-[9999] rounded-lg border border-border p-2.5 text-[12px] text-muted leading-relaxed max-w-[300px] whitespace-normal"
          style={{ ...pos(), backgroundColor: 'var(--card)', boxShadow: '0 4px 24px rgba(0,0,0,0.5)' }}
        >
          {text}
        </div>,
        document.body
      )}
    </>
  )
}
