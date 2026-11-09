/**
 * The ACP provider's hooks-section and session-process names are shown inside
 * translated copy: the Hooks page section heading and its load-failure and
 * empty-state titles, and the Agent Config tab's tip. Fixed English literals
 * there left "ACP Agent Hooks" and "ACP subprocess" in English under every
 * other language, so both must come from the catalog and be read at use time.
 */

import { describe, it, expect, afterAll } from 'vitest'
// `/all` for the non-English catalogs: `../i18n` registers English only.
import { i18next } from '../i18n/all'
import { AcpAdapter } from '../providers/adapters/acp'

afterAll(async () => {
  await i18next.changeLanguage('en')
})

describe('AcpAdapter hooks section and session process labels', () => {
  it('follow the active language', async () => {
    const adapter = new AcpAdapter()
    await i18next.changeLanguage('en')
    expect(adapter.labels.hooksSection).toBe('ACP Agent Hooks')
    expect(adapter.labels.sessionProcess).toBe('ACP subprocess')

    await i18next.changeLanguage('de')
    expect(adapter.labels.hooksSection).toBe('ACP-Agent-Hooks')
    expect(adapter.labels.sessionProcess).toBe('ACP-Unterprozess')

    await i18next.changeLanguage('ja')
    expect(adapter.labels.hooksSection).toBe('ACP エージェントフック')
    expect(adapter.labels.sessionProcess).toBe('ACP サブプロセス')
  })
})
