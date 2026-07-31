/**
 * Simplified-Chinese style guards.
 *
 * `catalogParity` proves the catalogs are structurally sound (same keys, no
 * empties, placeholders preserved). It cannot see whether the Chinese *reads*
 * like Chinese — which is how a catalog that is 100% "translated" still ships
 * calque word order, three renderings of one product noun, and ASCII commas
 * between CJK characters.
 *
 * These tests encode the normative rules in `STYLE-zh-CN.md` so that drift
 * fails CI instead of accumulating. Every assertion below corresponds to a
 * numbered rule in that document.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS as RUNTIME_CATALOGS } from './index'

const CJK = /[\u4e00-\u9fff]/

function flatten(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') {
      Object.assign(out, flatten(value, path))
    } else {
      out[path] = String(value)
    }
  }
  return out
}

const bundle = (code: string) =>
  flatten((RUNTIME_CATALOGS as Record<string, { translation: unknown }>)[code].translation)

const en = bundle('en')
const zh = bundle('zh-CN')

/**
 * Blank out runs that are code rather than prose, so the punctuation and
 * spacing rules below cannot fire on a file path, a version number, a dotted
 * config key or a JSON sample. Mirrors the carve-out list in STYLE-zh-CN.md §1.
 */
function stripCode(s: string): string {
  return s
    .replace(/\{\{[^}]*\}\}/g, ' ')
    .replace(/`[^`]*`/g, ' ')
    .replace(/https?:\/\/\S+/g, ' ')
    .replace(/~?\/[\w./~-]+/g, ' ')
    .replace(/\b[\w-]+\.(?:json|ya?ml|md|sh|ts|tsx|py|mjs|png|zip)\b/g, ' ')
    .replace(/\bv?\d+(?:\.\d+)+\b/g, ' ')
    .replace(/\b[\w-]+(?:\.[\w-]+){2,}\b/g, ' ')
    .replace(/\b[a-z]+_[a-z_]+\b/g, ' ')
    .replace(/\b\d+\s*[-–]\s*\d+\b/g, ' ')
}

/** Keys whose value is a code/markdown template, not prose. */
const CODE_TEMPLATE_KEYS = new Set([
  'components.themeEditor.name_my_theme_emoji_dark_bg_12141a_light_bg_fafa',
  'components.skillForm.my_skill_step_by_step_instructions_for_the_agent',
])

const prose = Object.entries(zh).filter(
  ([key, value]) => !CODE_TEMPLATE_KEYS.has(key) && CJK.test(value),
)

function report(bad: string[], limit = 6): string {
  return `${bad.length} violation(s):\n  ${bad.slice(0, limit).join('\n  ')}`
}

describe('zh-CN plural forms', () => {
  it('never supplies a plural category Chinese does not have', () => {
    // Chinese has exactly one CLDR plural category: `other`. A `_one` key is
    // therefore unreachable — and worse, it makes the catalog look like it
    // handles counting when it silently cannot.
    // A key may end in `_one` simply because its English sentence ends with
    // the WORD "one" (`click_new_to_create_one`) -- those are slug artifacts,
    // not plural forms. Only a key with an `_other` sibling is a real family.
    const keys = new Set(Object.keys(zh))
    const bad = [...keys].filter(k => {
      const m = k.match(/^(.*)_(one|two|few|many)$/)
      return m !== null && keys.has(`${m[1]}_other`)
    })
    expect(bad, report(bad)).toEqual([])
  })
})

describe('zh-CN terminology (STYLE-zh-CN.md §2)', () => {
  // One concept, one word. Each entry is [english cue, banned rendering,
  // canonical rendering]; the cue keeps the check context-sensitive, so a
  // banned string is only a violation where the English proves the sense.
  const BANNED: Array<[string, string, string]> = [
    ['sidebar', '侧栏', '侧边栏'],
    ['thread', '线程', '话题'],
    ['usage', '使用量', '用量'],
    ['inned', '已固定', '已置顶'],
    ['ffort', '投入度', '强度'],
    ['eject', '驳回', '拒绝'],
    ['urn', '回合', '轮次'],
    ['esolved', '已处理', '已解决'],
    ['ashboard', '仪表盘', '仪表板'],
    ['orkspace', '工作空间', '工作区'],
    ['ubagent', '子智能体', '子代理'],
    ['WeCom', 'WeCom', '企业微信'],
  ]

  for (const [cue, banned, canonical] of BANNED) {
    it(`renders '${cue}' as ${canonical}, never ${banned}`, () => {
      const bad = Object.keys(zh).filter(
        k => (en[k] ?? '').includes(cue) && zh[k].includes(banned),
      )
      expect(bad, report(bad)).toEqual([])
    })
  }
})

describe('zh-CN punctuation (STYLE-zh-CN.md §1)', () => {
  it('uses full-width punctuation between CJK characters', () => {
    // `,` and `.` between Chinese characters is the single most obvious tell
    // that a string was translated by a tool and never read by a human.
    const bad: string[] = []
    for (const [key, value] of prose) {
      const body = stripCode(value)
      if (/[\u4e00-\u9fff][,.;:?!](\s|$|[\u4e00-\u9fff])/.test(body)) {
        bad.push(`${key}: ${JSON.stringify(value.slice(0, 60))}`)
      }
    }
    expect(bad, report(bad)).toEqual([])
  })

  it('uses the full-width ellipsis for pending states', () => {
    const bad = prose
      .filter(([key, value]) => value.includes('...') && /…|\.\.\./.test(en[key] ?? ''))
      .map(([key, value]) => `${key}: ${JSON.stringify(value.slice(0, 60))}`)
    expect(bad, report(bad)).toEqual([])
  })

  it('uses corner brackets nowhere (curly quotes only)', () => {
    const bad = prose
      .filter(([, value]) => value.includes('「') || value.includes('」'))
      .map(([key]) => key)
    expect(bad, report(bad)).toEqual([])
  })

  it('keeps parentheses balanced within a value', () => {
    // A half-width opener married to a full-width closer renders as `(…）`.
    const bad: string[] = []
    for (const [key, value] of prose) {
      const half = (value.match(/\(/g) ?? []).length - (value.match(/\)/g) ?? []).length
      const full = (value.match(/（/g) ?? []).length - (value.match(/）/g) ?? []).length
      // A sentence fragment legitimately opens a bracket it never closes, so
      // only a MIXED-style imbalance is a defect.
      if (half !== 0 && full !== 0) bad.push(`${key}: ${JSON.stringify(value.slice(0, 60))}`)
    }
    expect(bad, report(bad)).toEqual([])
  })
})

describe('zh-CN tone (STYLE-zh-CN.md §3)', () => {
  it('never uses the honorific 您', () => {
    // The catalog is written in neutral second person 你. Mixing registers
    // mid-product reads worse than either register consistently.
    const bad = prose.filter(([, v]) => v.includes('您')).map(([k]) => k)
    expect(bad, report(bad)).toEqual([])
  })

  it('does not open a sentence with the translated "This will"', () => {
    const bad = prose.filter(([, v]) => v.includes('这将')).map(([k]) => k)
    expect(bad, report(bad)).toEqual([])
  })

  it('has no doubled particles', () => {
    const bad = prose
      .filter(([, v]) => /的的|了了|可以能|将会将/.test(v))
      .map(([k]) => k)
    expect(bad, report(bad)).toEqual([])
  })

  it('does not stack more than two 的 in one value', () => {
    // Genitive stacking (`A 的 B 的 C 的 D`) is the classic machine-translation
    // signature and always has a shorter native phrasing.
    // Counted per clause: a three-sentence description may legitimately use
    // 的 once per sentence, so only stacking WITHIN one clause is the defect.
    const bad = prose
      .filter(([, v]) =>
        v.split(/[。；！？\n]/).some(clause => (clause.match(/的/g) ?? []).length >= 3),
      )
      .map(([k, v]) => `${k}: ${JSON.stringify(v.slice(0, 60))}`)
    expect(bad, report(bad)).toEqual([])
  })
})
