import { describe, it, expect, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import fs from 'node:fs'
import path from 'node:path'
import { createRef, type MutableRefObject } from 'react'
import { renderWithProviders } from './helpers'
import PerformanceTab from '../pages/system/PerformanceTab'
import type { PlaneState } from '../pages/SystemPage'

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

const SYSTEM = {
  cpu_pct: 40, mem_used_gb: 8, mem_total_gb: 16, mem_free_gb: 8,
  disk_total_gb: 500, disk_free_gb: 250, net_rx_kbs: 10, net_tx_kbs: 5,
  hostname: 'build-host', os: 'linux', python: '3.12.4', cwd: '/srv/crew',
  arch: 'x86_64', cpu_count: 8, load_1m: 1, load_5m: 1, load_15m: 1,
  proc_cpu_pct: 1, proc_mem_mb: 200, thread_count: 10, ip: '127.0.0.1',
  mcp_total: 2,
}

vi.mock('../api/client', () => ({
  api: { system: () => Promise.resolve(SYSTEM) },
}))

const ROWS = ['hostname_row', 'os_row', 'python_row', 'working_directory_row'] as const
const SHIPPED = ['en', 'zh-CN', 'hi', 'es', 'fr', 'bn', 'pt', 'ru', 'de', 'ja', 'ko', 'it']

function performanceTab(lang: string): Record<string, string> {
  // English keeps these rows in the hand-authored catalog, not the extracted one.
  const read = (name: string) =>
    JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'i18n', 'locales', `${name}.json`), 'utf8'))
      .pages.performanceTab
  return lang === 'en' ? { ...read('en'), ...read('en.manual') } : read(lang)
}

describe('Performance machine-identity strip', () => {
  it.each(SHIPPED)('%s words each row as one message around its value', (lang) => {
    const block = performanceTab(lang)
    for (const key of ROWS) {
      // The catalog owns the colon and its spacing, so the label never needs an
      // English ": " glued on in code.
      expect(block[key], `${lang} ${key}`).toMatch(/^[^<]+<value\/>$/)
    }
  })

  it('writes the full-width colon for Chinese and Japanese', () => {
    for (const lang of ['zh-CN', 'ja']) {
      for (const key of ROWS) expect(performanceTab(lang)[key]).toContain('：<value/>')
    }
  })

  it('renders each row with its value in the monospace tag', async () => {
    const ref = createRef<PlaneState>() as MutableRefObject<PlaneState>
    ref.current = {}
    renderWithProviders(<PerformanceTab planeStateRef={ref} />)
    // Re-query inside waitFor: a re-render can replace the node a single find returned.
    await waitFor(() => {
      const host = screen.getByText('build-host')
      expect(host.tagName).toBe('STRONG')
      expect(host.parentElement?.textContent).toBe('Hostname: build-host')
      expect(screen.getByText('/srv/crew').parentElement?.textContent).toBe('Working directory: /srv/crew')
    }, { timeout: 8000 })
  })

  it('shows a working directory holding tag text exactly as written', async () => {
    SYSTEM.cwd = '/srv/a<br>b<i>c</i>'
    try {
      const ref = createRef<PlaneState>() as MutableRefObject<PlaneState>
      ref.current = {}
      const { container } = renderWithProviders(<PerformanceTab planeStateRef={ref} />)
      await waitFor(() => {
        expect(screen.getByText('/srv/a<br>b<i>c</i>').tagName).toBe('STRONG')
        expect(container.querySelector('br, i')).toBeNull()
      }, { timeout: 8000 })
    } finally {
      SYSTEM.cwd = '/srv/crew'
    }
  })
})
