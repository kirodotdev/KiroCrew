import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const read = (rel: string): string => readFileSync(resolve(__dirname, rel), 'utf-8')

describe('the close names the incarnation it means to archive', () => {
  it('puts the expected incarnation on the DELETE', () => {
    const client = read('../api/client.ts')
    const at = client.indexOf('deleteChatSlot:')

    expect(client.slice(at, at + 260)).toMatch(/incarnation \? '\?incarnation=' \+ encodeURIComponent/)
  })

  it('takes it from the row the caller was looking at', () => {
    const slice = read('../store/chatSlice.ts')

    expect(slice).toMatch(
      /const expectedIncarnation = root\.dashboard\.slots\.find\(s => s\.key === key\)\?\.incarnation/,
    )
    expect(slice).toMatch(/closeSlotOnServer\(key, expectedIncarnation\)/)
  })
})

describe('the boot-failure repair sits inside the error surface', () => {
  it('passes the retry through the notice rather than beside it', () => {
    const app = read('../App.tsx')
    const at = app.indexOf('boot-slots-failed')
    const notice = app.slice(at - 400, at + 500)

    expect(notice).toMatch(/action=\{\(/)
    expect(notice).toMatch(/data-testid="boot-slots-retry"/)
    expect(notice).not.toMatch(/\/>\s*<div className="px-3 pb-3">/)
  })
})
