/**
 * Isolated capture entry for PR #7616 — un-toggling a follow-up chip must
 * remove ONLY the suffix the chip itself appended, never user-typed text that
 * merely equals it.
 *
 * WHY ISOLATED: the defect is a data transform on the composer draft, driven by
 * a click on the REAL FollowUpBar. This mounts that real bar and a real
 * <textarea> composer, and wires the bar's onSelect to the composer through the
 * REAL shipped helper (fix=on) or the verbatim pre-fix content-match algorithm
 * (fix=off):
 *
 *   fix=on  — the shared ownership helper this PR ships
 *             (src/lib/followUpToggle: appendFollowUpOption / removeFollowUpOption),
 *             the exact functions ChatPane.tsx / ChatPage.tsx call.
 *   fix=off — the pre-fix content-match algorithm, verbatim
 *             (`prev === suffix` / `prev.endsWith(', ' + suffix)`). Reverting the
 *             exact logic the fix changes makes the before arm a faithful revert
 *             (mirrors capture/followup-chip-columns).
 *
 * Scenario driven by scripts/capture-chip-suffix-untoggle.mjs:
 *   1. the chip "Alpha" is appended (composer := "Alpha"),
 *   2. the user rewrites the draft to "other, Alpha" (their own text, same tail),
 *   3. the user un-toggles the lit "Alpha" chip.
 * fix=off deletes the user's ", Alpha" (composer := "other"); fix=on keeps it.
 *
 * window.__state() reports the composer value and picked set for assertions.
 *
 * Query string: ?theme=dark&fix=on
 */
import { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n'
import FollowUpBar from '../src/components/FollowUpBar'
import { appendFollowUpOption, removeFollowUpOption, type OwnedSuffix } from '../src/lib/followUpToggle'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const fixOn = params.get('fix') !== 'off'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const OPTIONS = ['Alpha', 'Beta']

declare global {
  interface Window {
    __state: () => { value: string; picked: string[]; fix: boolean }
  }
}

function Scene() {
  const [value, setValue] = useState('')
  const [picked, setPicked] = useState<Set<string>>(() => new Set())
  const pickedRef = useRef(picked); pickedRef.current = picked
  const inputRef = useRef(value); inputRef.current = value
  const ownedRef = useRef<OwnedSuffix | null>(null)

  useEffect(() => {
    window.__state = () => ({ value, picked: Array.from(pickedRef.current), fix: fixOn })
  }, [value])

  const onSelect = (o: string) => {
    if (pickedRef.current.has(o)) {
      const next = new Set(pickedRef.current); next.delete(o)
      pickedRef.current = next
      if (fixOn) {
        // REAL shipped helper — the exact code path ChatPane/ChatPage take.
        const r = removeFollowUpOption(inputRef.current, ownedRef.current, o)
        ownedRef.current = r.owned; inputRef.current = r.value; setValue(r.value)
      } else {
        // Pre-fix content-match algorithm — verbatim from the shipped bug.
        const priorSuffix = [...Array.from(next), o].join(', ')
        const remainingSuffix = Array.from(next).join(', ')
        setValue(prev => {
          if (prev === priorSuffix) return remainingSuffix
          const delimitedSuffix = ', ' + priorSuffix
          if (!prev.endsWith(delimitedSuffix)) return prev
          const draft = prev.slice(0, -delimitedSuffix.length)
          return remainingSuffix ? draft + ', ' + remainingSuffix : draft
        })
      }
      setPicked(next)
    } else {
      const next = new Set(pickedRef.current); next.add(o)
      pickedRef.current = next
      if (fixOn) {
        const r = appendFollowUpOption(inputRef.current, ownedRef.current, o)
        ownedRef.current = r.owned; inputRef.current = r.value; setValue(r.value)
      } else {
        setValue(prev => prev.trim() ? prev.trimEnd() + ', ' + o : o)
      }
      setPicked(next)
    }
  }

  return (
    <div className="bg-bg text-text flex flex-col justify-end min-h-screen">
      <div className="px-4 pb-3 pt-2 mx-auto w-full flex flex-col" style={{ maxWidth: 760 }}>
        <div className="mb-2 text-[12px] text-muted" data-mode>
          {fixOn ? 'AFTER — span ownership (this PR): un-toggle keeps the user\u2019s text'
                 : 'BEFORE — content match: un-toggle deletes the user\u2019s own text'}
        </div>
        <div data-bar>
          <FollowUpBar options={OPTIONS} picked={picked} onSelect={onSelect} onSend={() => {}} layout="multiline" />
        </div>
        <textarea
          aria-label="Message input"
          data-composer
          value={value}
          onChange={e => {
            // Mirror the real components: a direct user edit invalidates chip
            // ownership (fix=on only). fix=off keeps the pre-fix behavior where
            // ownership is content-derived and survives edits.
            if (fixOn) ownedRef.current = null
            inputRef.current = e.target.value
            setValue(e.target.value)
          }}
          rows={2}
          className="mt-1 rounded-2xl border border-border bg-bg-elevated px-3 py-3 text-[14px] text-text font-mono w-full resize-none"
        />
      </div>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
