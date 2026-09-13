import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const read = (rel: string): string => readFileSync(resolve(__dirname, rel), 'utf-8')
const app = read('../App.tsx')
const page = read('../pages/ArtifactDetailPage.tsx')

describe('a save failure is never masked by a stale close banner', () => {
  it('shows the save failure in preference to an older close banner', () => {
    const at = page.indexOf('ARTIFACT_CLOSE_FAILURE_COPY_KEY[closeFailure.kind]')
    const notice = page.slice(at - 200, at + 300)

    expect(notice).toMatch(/message=\{\s*saveError\s*\?\?/)
    expect(notice).toMatch(/closeFailure && !saveError/)
  })

  it('starts the archive gesture with both cleared, so neither can be stale', () => {
    const at = page.indexOf('setCloseFailure(null)')
    const gesture = page.slice(at - 120, at + 120)

    expect(gesture).toMatch(/sessionOpBusyRef\.current = true/)
    expect(gesture).toMatch(/setSaveError\(null\)/)
  })
})

describe('boot-list recovery latches instead of tracking the live count', () => {
  it('does not read recovery off the current slot count', () => {
    expect(app).not.toMatch(/const bootSlotsRecovered = useAppSelector\([^)]*slots\.length/)
  })

  it('keeps recovery in state so an emptied list cannot un-recover it', () => {
    expect(app).toMatch(/const \[bootSlotsRecovered, setBootSlotsRecovered\] = useState\(false\)/)
    // an APPLIED snapshot, not a row count: a zero-session user's list is legitimately empty
    expect(app).toMatch(/slotsEverApplied \|\| appliedGeneration !== generationAtMount\.current/)
    expect(app).toMatch(/setBootSlotsRecovered\(true\)/)
  })
})

describe('the refused close notice waits for the reader', () => {
  it('retires on no timer, matching the unknown variant', () => {
    expect(app).not.toMatch(/setTimeout\([^)]*setSessionCloseFailure\(null\)/)
    expect(app).not.toMatch(/setSessionCloseFailure\(null\)\)\s*,\s*12000/)
  })
})

describe('a notice offering an action cannot expire under the reach', () => {
  it('leaves the agent-switch hand-off notice on screen', () => {
    expect(app).not.toMatch(/setTimeout\([^)]*setAgentSwitchNotice\(null\)/)
    expect(app).not.toMatch(/setAgentSwitchNotice\(null\)\)\s*,\s*6000/)
  })
})

describe('the artifact-page close notice can be retired', () => {
  it('settles against the applied slot list, as the shell reducer does', () => {
    const at = page.indexOf("closeFailure.kind !== 'unknown'")
    const settle = page.slice(at - 200, at + 300)

    expect(settle).toMatch(/appliedSlots\.some\(s => s\.key === closeFailure\.key\)/)
    expect(settle).toMatch(/kind: 'confirmed'/)
  })

  it('waits for a list applied after the failure, since absence starts out normal', () => {
    const at = page.indexOf("closeFailure.kind !== 'unknown'")
    const settle = page.slice(at, at + 220)

    expect(settle).toMatch(/appliedGeneration <= closeFailure\.atGeneration/)
  })

  it('offers a dismiss for an outcome the list cannot answer', () => {
    expect(page).toMatch(/onDismiss=\{closeFailure && !saveError \? \(\) => setCloseFailure\(null\)/)
  })
})

describe('the boot-failure copy does not restate its own button', () => {
  it('leaves the retry wording to the button', () => {
    const copy = JSON.parse(read('../i18n/locales/en.manual.json')) as {
      app: Record<string, string>
    }

    expect(copy.app.boot_slots_failed).not.toMatch(/try again|retry/i)
    expect(copy.app.boot_slots_retry).toMatch(/retry loading sessions/i)
  })
})
