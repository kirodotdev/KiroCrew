import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { i18nT } from './t'
import { fmtNumber } from './format'

const P = 'components.reportProblemModal'
const ready = (secrets: number, files: number) =>
  i18nT(`${P}.diagnostics_ready`, {
    secrets: i18nT(`${P}.diagnostics_secrets_redacted`, { count: secrets, n: fmtNumber(secrets) }),
    where: i18nT(`${P}.diagnostics_in_files`, { count: files, n: fmtNumber(files) }),
  })

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('the diagnostics-ready line counts secrets and files with real plurals', () => {
  it('reads each English count in its own form', () => {
    expect(ready(1, 1)).toBe('Diagnostics ready — 1 secret redacted across 1 file.')
    expect(ready(3, 12)).toBe('Diagnostics ready — 3 secrets redacted across 12 files.')
    expect(ready(0, 4)).toBe('Diagnostics ready — 0 secrets redacted across 4 files.')
  })

  it('drops the (s) shortcut in German, French and Portuguese', async () => {
    await i18next.changeLanguage('de')
    expect(ready(1, 2)).toBe('Diagnose bereit — in 2 Dateien 1 Geheimnis redigiert.')
    await i18next.changeLanguage('fr')
    expect(ready(1, 1)).toBe('Diagnostics prêts — 1 secret masqué dans 1 fichier.')
    await i18next.changeLanguage('pt')
    expect(ready(2, 3)).not.toContain('(s)')
  })

  it('uses the Russian forms for each count', async () => {
    await i18next.changeLanguage('ru')
    expect(ready(1, 1)).toBe('Диагностика готова — скрыт 1 секрет в 1 файле.')
    expect(ready(5, 3)).toBe('Диагностика готова — скрыто 5 секретов в 3 файлах.')
  })
})
