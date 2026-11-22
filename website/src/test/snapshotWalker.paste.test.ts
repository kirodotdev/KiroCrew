/**
 * The `paste` op's page half. One paste event carries `text/html` and `text/plain`
 * to the field; `handled` says whether the page's own editor took it (cancelled the
 * event), since a script's paste event inserts nothing by itself and the gateway
 * then types the plain text.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'
import { setHtml } from './domFixture'

const HARNESS_DIR = join(__dirname, '../../../src/kiro_crew/browser_harness')

function walkerSource(): string {
  const lines = readFileSync(join(HARNESS_DIR, 'snapshot_walker.js'), 'utf8').split('\n')
  while (lines.length && (!lines[0].trim() || lines[0].trimStart().startsWith('//'))) lines.shift()
  return lines.join('\n').trim()
}

type Pasted = { ok: boolean; handled?: boolean; code?: string }
const walker = () =>
  new Function(`return (${walkerSource()})`)() as {
    paste: (ref: string | null, html: string, text: string) => Pasted
  }

afterEach(() => {
  setHtml(document.body, '')
})

describe('paste', () => {
  it('hands both formats to the focused editor and reports that it took them', () => {
    setHtml(document.body, '<div id="ed" contenteditable="true"></div>')
    const editor = document.getElementById('ed') as HTMLElement
    const got: string[] = []
    editor.addEventListener('paste', event => {
      const data = (event as ClipboardEvent).clipboardData
      got.push(data?.getData('text/html') ?? '', data?.getData('text/plain') ?? '')
      event.preventDefault()
    })
    editor.focus()
    expect(walker().paste(null, '<table><tr><td>a</td></tr></table>', 'a')).toEqual({ ok: true, handled: true })
    expect(got).toEqual(['<table><tr><td>a</td></tr></table>', 'a'])
  })

  it('reports a field whose page left the paste alone, and a ref that is gone', () => {
    setHtml(document.body, '<textarea id="t"></textarea>')
    ;(document.getElementById('t') as HTMLTextAreaElement).focus()
    expect(walker().paste(null, '', 'plain')).toEqual({ ok: true, handled: false })
    expect(walker().paste('e99', '', 'plain')).toEqual({ ok: false, code: 'ref_stale' })
  })
})
