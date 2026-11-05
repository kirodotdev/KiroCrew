/**
 * The shared agent-switch notice is the only surface that announces a per-slot write failure.
 *
 * Three surfaces report such a failure twice: in place, next to the control that failed, and
 * through the floating `agentSwitchNotice` toast. Both rendered `role="alert"` with the identical
 * string, so a screen reader read the failure out twice. The in-place copies now pass
 * `announce={false}` and the toast does the announcing alone.
 *
 * That trade is safe because `reportSlotWriteFailure` writes BOTH values in one call, so the
 * pairing holds by construction rather than by inspection. The first two tests below are the real
 * guarantee; the source checks after them only stop a caller going around the helper.
 */
import { describe, expect, it, vi } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { clearSlotWriteFailure, reportSlotWriteFailure } from '../components/slotWriteFailure'
import { setAgentSwitchNotice } from '../store/chatSlice'
import type { AppDispatch } from '../store'
import { initI18n } from '../i18n/all'

initI18n('en')

const SRC = join(__dirname, '..')

describe('reportSlotWriteFailure writes both surfaces or neither', () => {
  it('sets the in-place copy and the announcing toast from one call', () => {
    const dispatch = vi.fn() as unknown as AppDispatch
    const setInPlace = vi.fn()

    const notice = { message: 'the write did not persist', error: true }
    reportSlotWriteFailure(notice, dispatch, setInPlace)

    expect(setInPlace).toHaveBeenCalledExactlyOnceWith(notice.message)
    expect(dispatch).toHaveBeenCalledExactlyOnceWith(setAgentSwitchNotice(notice))
  })

  it('clears both, so a repeat of the same refusal remounts the alert and announces again', () => {
    const dispatch = vi.fn() as unknown as AppDispatch
    const setInPlace = vi.fn()

    clearSlotWriteFailure(dispatch, setInPlace)

    expect(setInPlace).toHaveBeenCalledExactlyOnceWith('')
    expect(dispatch).toHaveBeenCalledExactlyOnceWith(setAgentSwitchNotice(null))
  })
})

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.tsx?$/.test(name) && !name.includes('.test.')) out.push(p)
  }
  return out
}

const sources = walk(SRC)

/** The in-place error values rendered by a silenced notice, and the call site that renders each. */
const SILENCED: Record<string, string> = {
  setSwitchError: 'components/ChatPane.tsx',
  setAutoCompactError: 'components/chat-input/ContextShelf.tsx',
}

describe('nothing sets a silenced in-place error except through the helper', () => {
  it('is rendered by exactly the call sites this file pins', () => {
    // `.tsx` only: the prop is JSX, which TypeScript does not allow in a `.ts`
    // file, so a `.ts` match is prose about the prop rather than a render.
    const found = sources
      .filter(p => p.endsWith('.tsx') && readFileSync(p, 'utf8').includes('announce={false}'))
      .map(p => relative(SRC, p))
      .sort()
    // A new consumer must be added here deliberately, and must report through
    // reportSlotWriteFailure. Silencing a notice with no floating twin would
    // leave the failure unannounced.
    expect(found).toEqual(Object.values(SILENCED).sort())
  })

  for (const [setter, callSite] of Object.entries(SILENCED)) {
    it(`${setter} is only ever invoked to clear, never with a message`, () => {
      // Per OCCURRENCE, with no line window: an invocation carrying anything but
      // the empty string is a hand-rolled set that skips the toast. Passing the
      // setter to the helper reads as `${setter})`, which is not an invocation.
      const offenders: string[] = []
      for (const path of sources) {
        readFileSync(path, 'utf8').split('\n').forEach((text, i) => {
          for (const m of text.matchAll(new RegExp(`${setter}\\(([^)]*)\\)`, 'g'))) {
            if (m[1].trim() === "''" || m[1].trim() === '""') continue
            offenders.push(`${relative(SRC, path)}:${i + 1}: ${text.trim()}`)
          }
        })
      }
      expect(
        offenders,
        `${setter} is set outside reportSlotWriteFailure, so ${callSite} could silence the only surface`,
      ).toEqual([])
    })
  }
})

describe('a switch clears the stale toast before reporting a new failure', () => {
  const source = readFileSync(join(SRC, 'components/useChatPanePickers.ts'), 'utf8')

  /** Each `const switchX = useCallback(async …)` body, up to its dependency array. */
  const bodies = Array.from(
    source.matchAll(/const (switch\w+) = useCallback\(async[\s\S]*?\n {2}\}, \[/g),
  ).map(m => ({ name: m[1], body: m[0] }))

  const reporters = bodies.filter(b => b.body.includes('reportSlotWriteFailure('))

  it('finds both switch actions, so neither can be renamed out of this pin', () => {
    expect(reporters.map(r => r.name).sort()).toEqual(['switchAgent', 'switchModel'])
  })

  for (const { name, body } of reporters) {
    it(`${name} clears it first, so a repeat of the same refusal announces again`, () => {
      const beforeTry = body.slice(0, body.indexOf('try {'))
      expect(
        beforeTry,
        `${name} reports a failure through the unkeyed toast but does not clear it first, so a second identical refusal changes no DOM and is announced on no surface`,
      ).toMatch(/clearSlotWriteFailure\(/)
    })
  }
})

describe('an auto-compact retry clears the stale toast before it writes again', () => {
  it('pushAutoCompact clears both surfaces before scheduling the write', () => {
    const source = readFileSync(join(SRC, 'components/chat-input/autoCompact.ts'), 'utf8')
    const body = source.slice(source.indexOf('const pushAutoCompact = useCallback('))
    const beforeSchedule = body.slice(0, body.indexOf('autoCompactTimer.current = setTimeout('))
    expect(beforeSchedule).toMatch(/clearSlotWriteFailure\(dispatch, setAutoCompactError\)/)
  })
})

describe('the banner renders in every layout', () => {
  it('mounts once for the popout and embed frames and once for the dashboard', () => {
    const app = readFileSync(join(SRC, 'App.tsx'), 'utf8')
    expect(app.match(/<AgentSwitchNoticeBanner\b/g)).toHaveLength(2)
    expect(app).toMatch(/\{\(isPopout \|\| isEmbed\) && agentSwitchNotice && <AgentSwitchNoticeBanner\b/)
  })
})
