import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it, vi } from 'vitest'

// The shell's loading sign: what a first load shows while #root is still empty,
// instead of a dark blank page. Runs the REAL inline script and reads the REAL
// markup out of index.html, as bootFailureSurface.test.ts does for the panel.

const INDEX_PATH = path.resolve(__dirname, '..', '..', 'index.html')
const html = readFileSync(INDEX_PATH, 'utf8')

/** The inline script that wires the sign. */
function signScript(): string {
  const start = html.indexOf("var sign = document.getElementById('boot-loading')")
  expect(start).toBeGreaterThan(-1)
  const end = html.indexOf('</scr' + 'ipt>', start)
  expect(end).toBeGreaterThan(start)
  return html.slice(start, end).replace(/\}\)\(\)\s*$/, '')
}

function run(reducedMotion: boolean) {
  const sign = { style: { opacity: '0' }, offsetWidth: 0 }
  const ring = { animate: vi.fn() }
  const nodes: Record<string, unknown> = { 'boot-loading': sign, 'boot-loading-ring': ring }
  const document = { getElementById: (id: string) => nodes[id] ?? null }
  const window = { matchMedia: () => ({ matches: reducedMotion }) }
  new Function('document', 'window', signScript())(document, window)
  return { sign, ring }
}

describe('boot loading sign', () => {
  it('sits beside #root, not inside it, so the failure panel still sees an empty root', () => {
    expect(html).toContain('<div id="root"></div>')
    const root = html.indexOf('<div id="root"></div>')
    const sign = html.indexOf('<div id="boot-loading"')
    const panel = html.indexOf('<div id="boot-failure"')
    expect(sign).toBeGreaterThan(root)
    expect(panel).toBeGreaterThan(sign)
  })

  it('shows while #root is empty and is hidden once anything is put into it', () => {
    // The shell's own body markup, from #root to the end of the sign, with its
    // style rule, so the cascade (inline style included) is the real one.
    const start = html.indexOf('<div id="root"></div>')
    const signEnd = html.indexOf('<script>', html.indexOf('<div id="boot-loading"'))
    expect(signEnd).toBeGreaterThan(start)
    const parsed = new DOMParser().parseFromString(html.slice(start, signEnd), 'text/html')
    const host = document.createElement('div')
    // The parser may hoist the <style> into the parsed head; both halves come along.
    host.replaceChildren(...[...Array.from(parsed.head.childNodes), ...Array.from(parsed.body.childNodes)]
      .map(n => document.importNode(n, true)))
    document.body.appendChild(host)
    try {
      const sign = host.querySelector<HTMLElement>('#boot-loading')!
      const root = host.querySelector<HTMLElement>('#root')!
      expect(getComputedStyle(sign).display).toBe('flex')
      root.appendChild(document.createElement('div'))
      expect(getComputedStyle(sign).display).toBe('none')
    } finally {
      host.remove()
    }
  })

  it('declares no inline display, which would outrank the hiding rule', () => {
    const start = html.indexOf('<div id="boot-loading"')
    const tag = html.slice(start, html.indexOf('>', start) + 1)
    expect(tag).not.toMatch(/display\s*:/)
  })

  it('announces itself as a status with text a screen reader reads', () => {
    expect(html).toContain('<div id="boot-loading" role="status"')
    const parsed = new DOMParser().parseFromString(html, 'text/html')
    expect(parsed.getElementById('boot-loading')?.textContent?.trim()).toBe('Loading')
  })

  it('fades in and spins', () => {
    const { sign, ring } = run(false)
    expect(sign.style.opacity).toBe('1')
    expect(ring.animate).toHaveBeenCalledTimes(1)
  })

  it('keeps the ring still under reduced motion, but still shows it', () => {
    const { sign, ring } = run(true)
    expect(sign.style.opacity).toBe('1')
    expect(ring.animate).not.toHaveBeenCalled()
  })
})
