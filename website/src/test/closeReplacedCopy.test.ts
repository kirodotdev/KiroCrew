import { describe, it, expect } from 'vitest'
import {
  ARTIFACT_CLOSE_FAILURE_COPY_KEY,
  CLOSE_FAILURE_COPY_KEY,
  CLOSE_FAILURE_TITLE_KEY,
  closeFailureKind,
} from '../utils/sessionCloseFailure'
import en from '../i18n/locales/en.manual.json'

const asServerSends = { status: 409, body: JSON.stringify({ code: 'target_replaced', definitive: true }) }
const catalog = en as unknown as Record<string, Record<string, Record<string, string>>>
const copy = (path: string): string => {
  const [a, b, c] = path.split('.')
  return catalog[a][b][c]
}

describe('a replaced session gets the determinate answer, not the hedge', () => {
  it('classifies the server 409 as replaced rather than unknown', () => {
    expect(closeFailureKind(asServerSends)).toBe('replaced')
  })

  it('renders copy that states what happened and never hedges', () => {
    const kind = closeFailureKind(asServerSends)
    const title = copy(CLOSE_FAILURE_TITLE_KEY[kind])
    const body = copy(CLOSE_FAILURE_COPY_KEY[kind])

    expect(title).not.toMatch(/couldn.t confirm/i)
    expect(body).not.toMatch(/couldn.t confirm|come back on its own|don.t close it again/i)
    expect(title).toMatch(/already closed/i)
    expect(body).toMatch(/the row still listed is a new session/i)
  })

  it('gives the artifact surface its own determinate body', () => {
    const body = copy(ARTIFACT_CLOSE_FAILURE_COPY_KEY[closeFailureKind(asServerSends)])

    expect(body).toMatch(/nothing left to close/i)
    expect(body).toMatch(/again to start fresh/i)
    expect(body).not.toMatch(/archive/i)
    expect(body).not.toMatch(/come back on its own/i)
  })
})
