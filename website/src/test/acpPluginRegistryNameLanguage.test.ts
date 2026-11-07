/**
 * The ACP provider's package-registry name is shown inside translated copy:
 * the Prompts tab description on Agent Capabilities, the Prompts card tip and
 * the Skills tab's read-only package group. A fixed English literal there left
 * the word "Packages" in English under every other language, so the label must
 * come from the catalog and be read at use time, after a language switch.
 */

import { describe, it, expect, afterAll } from 'vitest'
// `/all` for the non-English catalogs: `../i18n` registers English only.
import { i18next } from '../i18n/all'
import { AcpAdapter } from '../providers/adapters/acp'

afterAll(async () => {
  await i18next.changeLanguage('en')
})

describe('AcpAdapter plugin registry name', () => {
  it('follows the active language', async () => {
    const adapter = new AcpAdapter()
    await i18next.changeLanguage('en')
    expect(adapter.labels.pluginRegistryName).toBe('Packages')

    await i18next.changeLanguage('de')
    expect(adapter.labels.pluginRegistryName).toBe('Pakete')

    await i18next.changeLanguage('ja')
    expect(adapter.labels.pluginRegistryName).toBe('パッケージ')
  })
})
