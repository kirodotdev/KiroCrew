/**
 * Traditional Chinese (Taiwan) style guards for `locales/zh-TW.json`.
 *
 * A zh-TW catalog produced by running `zh-CN.json` through a Simplified→Traditional
 * character converter passes every structural gate in this directory — identical
 * keys, placeholders and plural categories, no empty values, no stray English — and
 * still reads as foreign software, because the words are Mainland words in
 * Traditional glyphs: 設置 instead of 設定, 軟件 instead of 軟體, 服務器 instead of
 * 伺服器. `catalogParity` cannot see that; these tests encode `style/zh-TW.md` so
 * that drift fails CI.
 *
 * Two tripwires aim at the two halves of that failure, plus one typographic rule:
 *
 *  1. an UNCONVERTED paste — Simplified-only characters in the catalog;
 *  2. a CONVERTED paste — the unambiguous Mainland forms from `zh-TW.md` §2;
 *  3. Taiwan quotes with corner brackets 「」, not the curly pair zh-CN uses.
 *
 * Neither list is a classifier. §1 is a fixed set of high-frequency Simplified-only
 * characters with every character that is ALSO standard Traditional usage removed
 * (后, 松, 里, 台, 制, 只, 干, 表, 面, 系, 越 … are deliberately absent). §2 lists only
 * forms that are wrong in every sense; homographs such as 代碼 (an identifier code is
 * correct Taiwanese) and 文件 (a document) stay review-only. A native reviewer still
 * owns everything else in the style guide.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS as RUNTIME_CATALOGS } from '../catalogs'

function flatten(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') Object.assign(out, flatten(value, path))
    else out[path] = String(value)
  }
  return out
}

const FLAT = flatten(
  (RUNTIME_CATALOGS as Record<string, { translation: unknown }>)['zh-TW'].translation,
)

/** §1 — Simplified-only characters. Each one's Traditional form is a different code point. */
const SIMPLIFIED_ONLY = new Set(
  (
    '会个无开动时话发请关这载启选并复设运没务为标败录内项过记试认对显库机审网连问闭读'
    + '评实装签该条夹从将览添检来题处点图现预创编页轮数仓删进择论换调与态当码结写计证辑'
    + '间确状暂输绝产则仅还据导视击规链户称测牌变权报续队语匹区筛议浏执单验断键许员刷备'
    + '频档决远边构给组钥隐访忆经继径终错类销盘么词凭转树识储贴们统让询级获强误志随栏钟'
    + '册顶带义侧独线粘响仪盖静渠环屏扫笔额样缩详长联弃归说别风费头达监绪总两触丢脚络观'
    + '着围叠赖画阶维局阅离团简颜优传译资声较应体账号'
    + '儿双严万亿龙岁书买卖习亲众华医丽举乐农劳卫厂历压县参叹'
  ).split(''),
)

/**
 * Keys allowed to contain Simplified characters. Narrow on purpose: only a LITERAL the
 * user has to match against something on their screen. Both name UI in the WeCom admin
 * console, which exists only in Simplified, and the English source quotes them verbatim.
 */
const SIMPLIFIED_EXEMPT: Record<string, string> = {
  'pages.settings.weComPanel.guide_body': 'quotes the WeCom console menu path 应用管理 → AI 智能体',
  'pages.settings.weComPanel.allowlist_description': 'names the WeCom field 账号',
}

/** §2 — unambiguous Mainland forms, each with the Taiwanese word to use instead. */
const MAINLAND_WORDING: ReadonlyArray<readonly [string, string]> = [
  ['設置', '設定'],
  ['默認', '預設'],
  ['軟件', '軟體'],
  ['硬件', '硬體'],
  ['源代碼', '程式碼'],
  ['內存', '記憶體'],
  ['文件夾', '資料夾'],
  ['服務器', '伺服器'],
  ['網絡', '網路'],
  ['加載', '載入'],
  ['創建', '建立'],
  ['保存', '儲存 / 保留'],
  ['運行', '執行'],
  ['隊列', '佇列'],
  ['鏈接', '連結'],
  ['視頻', '影片'],
  ['音頻', '音訊'],
  ['數據', '資料'],
  ['日誌', '記錄'],
  ['賬號', '帳號'],
  ['賬戶', '帳戶'],
  ['屏幕', '螢幕'],
  ['打印', '列印'],
  ['端口', '連接埠'],
  ['線程', '執行緒'],
  ['緩存', '快取'],
  ['激活', '啟用'],
  ['禁用', '停用'],
  // Taiwan splits Mainland 信息 into 訊息 (message) and 資訊 (information).
  ['信息', '訊息 / 資訊'],
]

describe('zh-TW is not a script conversion of zh-CN', () => {
  it('contains no Simplified-only characters outside the documented exemptions', () => {
    const offenders: string[] = []
    for (const [key, value] of Object.entries(FLAT)) {
      if (key in SIMPLIFIED_EXEMPT) continue
      const found = [...new Set([...value].filter(ch => SIMPLIFIED_ONLY.has(ch)))]
      if (found.length > 0) offenders.push(`${key} (${found.join('')})`)
    }
    expect(offenders, `Simplified characters in the Traditional catalog: ${offenders.join(', ')}`)
      .toEqual([])
  })

  it('keeps every exemption pointed at a key that still needs it', () => {
    const stale = Object.keys(SIMPLIFIED_EXEMPT)
      .filter(k => ![...FLAT[k] ?? ''].some(ch => SIMPLIFIED_ONLY.has(ch)))
    expect(stale, `exemption no longer needed or key gone: ${stale.join(', ')}`).toEqual([])
  })

  it('uses Taiwanese, not converted Mainland, terminology (zh-TW.md §2)', () => {
    const offenders: string[] = []
    for (const [key, value] of Object.entries(FLAT)) {
      for (const [wrong, right] of MAINLAND_WORDING) {
        if (value.includes(wrong)) offenders.push(`${key}: ${wrong} → ${right}`)
      }
      // 租用戶 (tenant) and 用戶端 (client) are correct Taiwanese; bare 用戶 is not.
      if (value.replace(/租用戶|用戶端/g, '').includes('用戶')) offenders.push(`${key}: 用戶 → 使用者`)
    }
    expect(offenders, `Mainland wording in Traditional script: ${offenders.join('; ')}`).toEqual([])
  })
})

describe('zh-TW typography', () => {
  it('quotes with corner brackets, never the curly pair zh-CN uses', () => {
    const offenders = Object.entries(FLAT)
      .filter(([, v]) => /[“”‘’]/.test(v))
      .map(([k]) => k)
    expect(offenders, `curly quotes in the Traditional catalog: ${offenders.join(', ')}`).toEqual([])
  })

  it('balances every corner bracket it opens', () => {
    const count = (v: string, ch: string) => [...v].filter(c => c === ch).length
    const offenders = Object.entries(FLAT)
      .filter(([, v]) => count(v, '「') !== count(v, '」') || count(v, '『') !== count(v, '』'))
      .map(([k]) => k)
    expect(offenders, `unbalanced corner brackets: ${offenders.join(', ')}`).toEqual([])
  })
})
