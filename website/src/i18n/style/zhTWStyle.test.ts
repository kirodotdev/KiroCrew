/**
 * Traditional Chinese (Taiwan) house-style gates for `locales/zh-TW.json`.
 *
 * The failure mode this file exists to catch is specific, and it is NOT the one
 * a parity or placeholder check catches. A zh-TW catalog produced by running
 * `zh-CN.json` through a Simplified→Traditional character converter passes every
 * structural gate in this directory: identical keys, identical placeholders,
 * identical plural categories, no empty values, no stray English. It is also
 * unusable, because the words are Mainland words wearing Traditional glyphs —
 * 設置 instead of 設定, 軟件 instead of 軟體, 服務器 instead of 伺服器. A Taiwanese
 * reader recognises that instantly and reads the product as foreign software.
 *
 * So there are two checks, aimed at the two halves of that failure:
 *
 *  1. **Unconverted paste** (§2 below) — Simplified-only characters that survive
 *     when someone copies a zh-CN value across and forgets to convert it at all.
 *  2. **Converted paste** (§3) — the `zh-TW.md` §2.1 wordlist, which is the
 *     likelier and more damaging case, because conversion makes it invisible.
 *
 * And one typographic gate (§4): Taiwan quotes with corner brackets 「」, per
 * 教育部《重訂標點符號手冊》. `zh-CN` takes the curly pair “”; a curly quote around
 * a filename reads as Mainland typesetting.
 *
 * **What these gates do not prove.** Neither list is a classifier. §2's denylist
 * is a fixed set of high-frequency Simplified-only characters, derived from the
 * characters `zh-CN.json` actually uses, with every character that is ALSO a
 * legitimate Traditional character removed by hand (后, 松, 里, 台, 制, 只, 干 and
 * friends are deliberately absent — flagging them would produce false positives
 * on correct Taiwanese prose). A rare Simplified form outside the list passes.
 * §3 catches the wordlist, not every Mainland turn of phrase. Both are tripwires
 * sized to catch a bulk paste; the terminology judgements in `zh-TW.md` §1 and §4
 * still need a native reviewer, and this file does not claim otherwise.
 */

import { describe, it, expect } from 'vitest'

import zhTW from '../locales/zh-TW.json'

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

const FLAT = flatten(zhTW)

/**
 * §2 — Simplified-only characters.
 *
 * Every character here is a Simplified form whose Traditional counterpart is a
 * different code point, AND which is not itself standard Traditional usage.
 * That second condition is what keeps the gate honest: 后 (皇后), 松 (松樹),
 * 里 (公里), 台 (台北), 制 (制度), 只 (只有), 干 (干擾), 表 (表格), 面 (面板),
 * 系 (系統), 布, 才, 折, 出, 板, 采, 云, 范, 於→于 and 幾→几 all appear in correct
 * Taiwanese copy and are therefore NOT listed, even though each is also the
 * Simplified form of something.
 */
const SIMPLIFIED_ONLY = new Set(
  (
    '会个无开动时话发请关这载启选并复设运没务为标败录内项过记试认对显库机审网连问闭读'
    + '评实装签该条夹从将览添检来题处点图现预创编页轮数仓删进择论换调与态当码结写计证辑'
    + '间确状暂输绝产则仅还据导视击规链户称测牌变权报续队语匹区筛议浏执单验断键许员刷备'
    + '频档决远边构给组钥隐访忆经继径终错类销盘么词凭转树识储贴们统让询级获强误志随栏钟'
    + '册顶带义侧独线粘响仪盖静渠环屏扫笔额样缩详长联弃归说别风费头达监绪总两触丢脚络观'
    + '着围叠赖画阶维局阅离团越简颜优传译资声较'
    // Not reachable from the derivation above, because the derivation subtracts
    // characters this catalog already uses and these four are used — by the two
    // exempt keys below. Listing them is what makes that exemption visible.
    + '应体账号'
    // Common enough in Chinese UI copy to belong here even though `zh-CN.json`
    // happens not to use them.
    + '儿双严万亿龙岁书买卖习亲众华医丽举乐农劳卫厂历压县参叹'
  ).split(''),
)

/**
 * Keys allowed to contain Simplified characters, with the reason.
 *
 * The bar is narrow on purpose: a Simplified string is permitted only when it is
 * a LITERAL the user has to match against something on their screen, not prose.
 * Both entries name UI in Tencent's WeCom admin console, which exists only in
 * Simplified — translating the menu path would send a Taiwanese admin looking
 * for a menu item that is not there. The English source carries them verbatim
 * for the same reason.
 */
const SIMPLIFIED_EXEMPT: Record<string, string> = {
  'pages.settings.weComPanel.guide_body':
    'quotes the WeCom console menu path 应用管理 → AI 智能体 verbatim, inside <mono>, '
    + 'because that is the label the admin has to find on screen',
  'pages.settings.weComPanel.allowlist_description':
    'names the WeCom field 账号 verbatim, as the English source does',
}

/**
 * §3 — `zh-TW.md` §2.1. Left column is what a character conversion of
 * `zh-CN.json` produces: already Traditional script, still Mainland wording.
 *
 * **Only unambiguous forms belong here.** Several §2.1 entries are homographs of
 * correct Taiwanese words and are review-only in the style guide rather than
 * gated, because a substring match on them is wrong more often than it is right:
 *
 *  - `代碼` is Mainland for *source code* (Taiwan: 程式碼) but standard Taiwanese
 *    for an *identifier* — 錯誤代碼, 語言代碼, a pairing code. Only the compound
 *    `源代碼` is unambiguous, so that is what is listed.
 *  - `用戶` is Mainland for *user* (Taiwan: 使用者), but `租用戶` is "tenant" and
 *    `用戶端` is "client", both standard in Taiwan. Matched with those two
 *    excluded, below.
 *  - `文件` means "document" in Taiwan, so it is wrong only where English said
 *    "file" — not decidable from the value alone, so it stays review-only.
 *
 * Getting this wrong in the permissive direction costs a missed defect once.
 * Getting it wrong in the strict direction makes the gate lie about correct
 * copy, and the next author deletes the gate.
 */
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
  ['保存', '儲存'],
  ['運行', '執行'],
  ['隊列', '佇列'],
  ['鏈接', '連結'],
  ['視頻', '影片'],
  ['音頻', '音訊'],
  ['數據', '資料'],
  ['日誌', '記錄檔'],
  ['賬號', '帳號'],
  ['賬戶', '帳戶'],
  ['屏幕', '螢幕'],
  ['打印', '列印'],
  ['端口', '連接埠'],
  ['線程', '執行緒'],
  ['緩存', '快取'],
  ['優化', '最佳化'],
  ['激活', '啟用'],
  ['禁用', '停用'],
  // `信息` is the Mainland word for both "message" and "information"; Taiwan
  // splits them into 訊息 and 資訊, so the bare form is always wrong here.
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
    expect(offenders,
      'Simplified characters in the Traditional catalog — a zh-CN value was pasted '
      + `without conversion, or a new key was written in the wrong script: ${offenders.join(', ')}`)
      .toEqual([])
  })

  it('keeps every exemption pointed at a key that still exists', () => {
    // An exemption for a deleted key is a licence nobody is using and the next
    // author would inherit without the context that justified it.
    const stale = Object.keys(SIMPLIFIED_EXEMPT).filter(k => FLAT[k] === undefined)
    expect(stale, `exemption names a missing key: ${stale.join(', ')}`).toEqual([])
  })

  it('keeps every exemption in use', () => {
    // The mirror: an exempt key that no longer contains Simplified text should
    // lose its exemption, or the gate quietly stops covering it.
    const unused = Object.keys(SIMPLIFIED_EXEMPT)
      .filter(k => ![...FLAT[k] ?? ''].some(ch => SIMPLIFIED_ONLY.has(ch)))
    expect(unused, `exemption no longer needed: ${unused.join(', ')}`).toEqual([])
  })

  it('uses Taiwanese, not converted Mainland, terminology (zh-TW.md §2.1)', () => {
    const offenders: string[] = []
    for (const [key, value] of Object.entries(FLAT)) {
      for (const [wrong, right] of MAINLAND_WORDING) {
        if (value.includes(wrong)) offenders.push(`${key}: ${wrong} → ${right}`)
      }
      // `用戶` needs its two legitimate compounds excluded before matching, so it
      // is checked here rather than in the table above. Blanking them first is
      // simpler than a lookaround and reads the same at the call site.
      if (value.replace(/租用戶/g, '').replace(/用戶端/g, '').includes('用戶')) {
        offenders.push(`${key}: 用戶 → 使用者`)
      }
    }
    expect(offenders,
      'Mainland wording in Traditional script — converting the glyphs does not '
      + `change the word: ${offenders.join('; ')}`)
      .toEqual([])
  })
})

describe('zh-TW typography', () => {
  it('quotes with corner brackets, never the curly pair zh-CN uses', () => {
    // 教育部《重訂標點符號手冊》 makes 「」 the primary pair and 『』 the nested one.
    // The curly pair is the zh-CN convention (see OPERAND_QUOTE_PAIRS in
    // scripts/lib/qa-checks.mjs, which pins the same glyphs for destructive
    // confirms); a curly quote here means a zh-CN value came across.
    const offenders = Object.entries(FLAT)
      .filter(([, v]) => /[“”‘’]/.test(v))
      .map(([k, v]) => `${k} :: ${v.slice(0, 60)}`)
    expect(offenders,
      `curly quotes in a Traditional catalog: ${offenders.join(' | ')}`)
      .toEqual([])
  })

  it('balances every corner bracket it opens', () => {
    // An unbalanced 「 is the visible half of a truncated or half-edited value,
    // and the operand-quoting gate in destructiveConfirm.test.ts only checks the
    // keys it pins — this covers the rest of the catalog.
    const offenders = Object.entries(FLAT)
      .filter(([, v]) => {
        const count = (ch: string) => [...v].filter(c => c === ch).length
        return count('「') !== count('」') || count('『') !== count('』')
      })
      .map(([k, v]) => `${k} :: ${v.slice(0, 60)}`)
    expect(offenders,
      `unbalanced corner brackets: ${offenders.join(' | ')}`)
      .toEqual([])
  })
})
