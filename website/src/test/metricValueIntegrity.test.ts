/**
 * A quantity must not break across lines.
 *
 * Measured with `unitDisplay: 'narrow'`, CLDR is not self-consistent about the
 * separator it puts between a number and its unit:
 *
 *   en     5,289MB    no separator at all
 *   zh-CN  5,289 MB   U+0020  <- a UAX #14 break opportunity
 *   de     5.289 MB   U+0020, but 1.280 GB uses U+00A0
 *   ru     5 289 МБ   U+00A0 for bytes, U+0020 for hours
 *   fr     5289Mo     U+202F (narrow no-break space)
 *
 * So `1,280 GB` could render with `GB` orphaned on its own line in zh-CN while
 * the identical value in English could not. Promoting plain spaces inside a
 * formatted quantity to U+00A0 makes the behaviour identical in all 12 locales.
 *
 * The other half of the defect was `word-break: break-all` on the value cell,
 * which breaks between letters whether or not anything overflows and therefore
 * split the digits themselves — `5,289 MB` as `5,` / `28` / `9 MB`. That is
 * pinned as a class contract because happy-dom does no layout.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { fmtBytes, fmtUnit, __resetFormatterCache } from '../i18n/format'
import { i18next } from '../i18n'

const LOCALES = ['en', 'zh-CN', 'de', 'ru', 'fr', 'ja']

async function withLanguage(code: string, run: () => void): Promise<void> {
  await i18next.changeLanguage(code)
  run()
}

afterEach(async () => {
  await i18next.changeLanguage('en')
  __resetFormatterCache()
})

describe('formatted quantities are unbreakable', () => {
  it('never contains a plain space, in any shipped locale', async () => {
    for (const loc of LOCALES) {
      await withLanguage(loc, () => {
        for (const v of [5_289_000_000, 1_280_000_000_000, 512, 20_500]) {
          const s = fmtBytes(v)
          expect(s, `${loc}: fmtBytes(${v}) = ${JSON.stringify(s)}`).not.toMatch(/ /)
        }
        const hours = fmtUnit(4, 'hour')
        expect(hours, `${loc}: fmtUnit(4,'hour') = ${JSON.stringify(hours)}`).not.toMatch(/ /)
      })
    }
  })

  it('still renders the same visible text, with U+00A0 where a space used to be', async () => {
    await withLanguage('zh-CN', () => {
      // The exact call ServicesTab makes for 内存（RSS）, and the exact reading
      // from the report: 5,289 MB rendered as `5,` / `28` / `9 MB`.
      const s = fmtUnit(5289, 'megabyte', { maximumFractionDigits: 0 })
      // The value is intact and the unit is still attached — only the separator
      // changed class, so nothing about the reading changes.
      expect(s.replace(/\u00A0/g, ' ')).toBe('5,289 MB')
      expect(s).toContain('\u00A0')
    })
  })

  it('leaves a locale that already uses a non-breaking separator alone', async () => {
    await withLanguage('fr', () => {
      // fr uses U+202F here, which is already non-breaking and must not be
      // rewritten to U+00A0 — the replace only ever touches plain U+0020.
      const s = fmtUnit(5289, 'megabyte', { maximumFractionDigits: 0 })
      expect(s).toContain('\u202F')
      expect(s).not.toContain('\u00A0')
    })
  })
})

describe('ServicesTab value cell', () => {
  it('breaks at word boundaries, never between letters or digits', async () => {
    const src = (await import('../pages/system/ServicesTab.tsx?raw')).default as string
    const cell = src.match(/className="text-text-strong font-mono[^"]*"/)
    expect(cell, 'expected the value cell in ServicesTab').not.toBeNull()
    expect(cell![0]).toContain('break-words')
    expect(cell![0]).not.toContain('break-all')
    // Metric columns should not re-flow as digits change width.
    expect(cell![0]).toContain('tabular-nums')
  })
})
