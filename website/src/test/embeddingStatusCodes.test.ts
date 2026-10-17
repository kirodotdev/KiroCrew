import { afterEach, describe, expect, it } from 'vitest'
import { i18next, initI18n } from '../i18n/all'
import { CATALOGS } from '../i18n/catalogs'
import {
  embeddingSetupStepLabel, embeddingSetupError, embeddingSetupErrorPointer, embeddingSetupWarning, embeddingRepairMessage, embeddingSetupDiagnostic, isModelPathErrorCode,
} from '../pages/overview/embeddingStatusText'
import { reembedBar, reembedBusy } from '../pages/overview/EmbeddingModelCard'

afterEach(async () => { await i18next.changeLanguage('en') })

const RAW = 'memory.embed_model_path could not be read: [Errno 5] Input/output error'

describe('embedding status codes', () => {
  it('keeps old and unknown backend English messages unchanged', () => {
    expect(embeddingSetupWarning({ setup_warning: 'Old warning' })).toBe('Old warning')
    expect(embeddingSetupWarning({ setup_warning: 'New warning', setup_warning_code: 'new_code' })).toBe('New warning')
    expect(embeddingSetupError({ setup_error: 'Old error' })).toBe('Old error')
    expect(embeddingSetupError({ setup_error: 'New error', setup_error_code: 'new_code' })).toBe('New error')
    expect(embeddingSetupError(null)).toBe('')
    // An unknown code renders its prose as the body, so there is no second copy to fold.
    expect(embeddingSetupDiagnostic({ setup_error: 'New error', setup_error_code: 'new_code' })).toBe('')
    expect(embeddingSetupDiagnostic(null)).toBe('')
  })

  it('translates every known code in every shipped language and preserves parameters', async () => {
    await initI18n()
    for (const language of Object.keys(CATALOGS).filter(code => code !== 'en-XA')) {
      await i18next.changeLanguage(language)
      const warning = embeddingSetupWarning({ setup_warning_code: 'legacy_embedding_vectors', setup_warning: 'backend fallback' })
      expect(warning).not.toContain('backend fallback')
      expect(warning).not.toContain('pages.overview')
      // With the model file missing, the warning must not tell the user to reapply the missing path.
      const pathWarning = embeddingSetupWarning({ setup_warning_code: 'legacy_embedding_vectors', setup_warning: 'backend fallback', setup_error_code: 'model_path_not_found' })
      expect(pathWarning).not.toContain('backend fallback')
      expect(pathWarning).not.toContain('pages.overview')
      expect(pathWarning).not.toBe(warning)
      for (const code of ['model_path_not_absolute', 'model_path_not_found', 'model_path_not_a_file', 'model_path_too_small', 'model_path_protected', 'model_path_unreadable', 'model_verification_failed']) {
        const error = embeddingSetupError({ setup_error_code: code, setup_error: 'backend fallback', setup_error_params: { path: '/models/文件.gguf', error: RAW } })
        expect(error).toContain('/models/文件.gguf')
        expect(error).not.toContain('pages.overview')
        expect(error).not.toContain('{{')
        // Known codes are fully localized: the backend's English exception never lands in the body.
        expect(error).not.toContain(RAW)
        expect(error).not.toContain('Errno')
      }
      expect(embeddingSetupError({ setup_error_code: 'model_identity_unverified' })).not.toContain('pages.overview')
      const download = embeddingSetupError({ setup_error_code: 'model_download_failed', setup_error_params: { error: RAW } })
      expect(download).not.toContain(RAW)
      expect(download).not.toContain('pages.overview')
      expect(download).not.toContain('{{')
      const repair = embeddingRepairMessage({ repair: { generation: 'request', pending_invalidation: 3, pending_vectors: 17, deferred_stores: 9 } })
      expect(repair).toContain('17')
      expect(repair).toContain('9')
      expect(repair).toContain('3')
      expect(repair).not.toContain('{{')
      // Not a raw field echo: none of the API's field names leak into the copy.
      for (const jargon of ['pending_invalidation', 'pending_vectors', 'deferred_stores', 'invalidation:', 'Invalidation:']) {
        expect(repair).not.toContain(jargon)
      }
    }
  })

  it('keeps the raw exception reachable through the diagnostic, not the body', () => {
    const status = { setup_error_code: 'model_verification_failed', setup_error: RAW, setup_error_params: { path: '/models/a.gguf', error: RAW } }
    expect(embeddingSetupError(status)).not.toContain(RAW)
    expect(embeddingSetupDiagnostic(status)).toBe(RAW)
    const download = { setup_error_code: 'model_download_failed', setup_error: 'HTTP 503', setup_error_params: { error: 'HTTP 503' } }
    expect(embeddingSetupError(download)).not.toContain('503')
    expect(embeddingSetupDiagnostic(download)).toBe('HTTP 503')
    // Path codes say everything the raw text says, so no diagnostic is offered.
    expect(embeddingSetupDiagnostic({ setup_error_code: 'model_path_not_found', setup_error: 'no file', setup_error_params: { path: '/x', error: 'no file' } })).toBe('')
    // Falls back to setup_error when the params object omits the text.
    expect(embeddingSetupDiagnostic({ setup_error_code: 'model_download_failed', setup_error: 'HTTP 503' })).toBe('HTTP 503')
  })

  it('recognises exactly the model path codes', () => {
    for (const code of ['model_path_not_absolute', 'model_path_not_found', 'model_path_not_a_file', 'model_path_too_small', 'model_path_protected', 'model_path_unreadable']) {
      expect(isModelPathErrorCode(code)).toBe(true)
    }
    expect(isModelPathErrorCode('model_verification_failed')).toBe(false)
    expect(isModelPathErrorCode('model_download_failed')).toBe(false)
    expect(isModelPathErrorCode('')).toBe(false)
    expect(isModelPathErrorCode(undefined)).toBe(false)
  })

  it('does not interpret English text as a code', async () => {
    await i18next.changeLanguage('zh-CN')
    expect(embeddingSetupError({ setup_error: 'model_path_not_found' })).toBe('model_path_not_found')
    expect(embeddingSetupWarning({ setup_warning_code: 'legacy_embedding_vectors' })).toContain('向量')
    expect(embeddingSetupWarning({ setup_warning_code: 'legacy_embedding_vectors', setup_error_code: 'model_path_not_found' })).toContain('先修正模型路径')
    expect(embeddingSetupError({ setup_error_code: 'model_identity_unverified' })).toContain('校验')
  })

  it('offers a short pointer only for a path code, in every shipped language, without the message or path', async () => {
    await initI18n()
    const status = { setup_error_code: 'model_path_not_found', setup_error: 'no file', setup_error_params: { path: '/models/文件.gguf', error: 'no file' } }
    for (const language of Object.keys(CATALOGS).filter(code => code !== 'en-XA')) {
      await i18next.changeLanguage(language)
      const pointer = embeddingSetupErrorPointer(status)
      expect(pointer).not.toBe('')
      expect(pointer).not.toContain('pages.overview')
      expect(pointer).not.toContain('{{')
      expect(pointer).not.toContain('/models/文件.gguf')
      expect(pointer).not.toContain('no file')
      expect(pointer).not.toBe(embeddingSetupError(status))
    }
    await i18next.changeLanguage('en')
    expect(embeddingSetupErrorPointer({ setup_error_code: 'model_verification_failed', setup_error_params: { path: '/x', error: RAW } })).toBe('')
    expect(embeddingSetupErrorPointer({ setup_error_code: 'model_download_failed' })).toBe('')
    expect(embeddingSetupErrorPointer({ setup_error: 'model_path_not_found' })).toBe('')
    expect(embeddingSetupErrorPointer(null)).toBe('')
  })

  it('keeps unknown repair scope and deferred progress distinct from completion', () => {
    expect(embeddingRepairMessage({ repair: { generation: 'r', unknown_scope: true } })).toContain('not confirmed')
    expect(embeddingRepairMessage({ repair: { generation: 'r' } })).toBe('')
    expect(embeddingRepairMessage(null)).toBe('')
    expect(reembedBar({ step: 'deferred' })).toEqual({ widthPct: 0, indeterminate: true })
    expect(reembedBusy({ step: 'deferred' })).toBe(false)
  })

  it('never sums the store count into the vector count, and shows an invalidation-only state', () => {
    // 2 open stores still to clear, 0 vectors, 0 deferred: still pending, still shown.
    const invalidationOnly = embeddingRepairMessage({ repair: { generation: 'r', pending_invalidation: 2, pending_vectors: 0, deferred_stores: 0 } })
    expect(invalidationOnly).not.toBe('')
    expect(invalidationOnly).toContain('2')
    // 2 stores + 5 vectors must not read as 7 of anything.
    const both = embeddingRepairMessage({ repair: { generation: 'r', pending_invalidation: 2, pending_vectors: 5, deferred_stores: 0 } })
    expect(both).toContain('2')
    expect(both).toContain('5')
    expect(both).not.toMatch(/\b7\b/)
  })
})

describe('independent repair plurals and setup labels', () => {
  it.each([
    [1, 2, 3, ['1 memory still needs', '2 open stores still hold', '3 closed or unavailable stores'], []],
    [2, 1, 0, ['2 memories still need', '1 open store still holds'], ['closed or unavailable']],
    [0, 0, 1, ['1 closed or unavailable store will be rebuilt when it next opens'], ['memories still need', 'open stores still hold']],
  ])('selects each unit independently and names only the non-zero ones (%s, %s, %s)', (vectors, invalidation, deferred, present, absent) => {
    const message = embeddingRepairMessage({ repair: { generation: 'r', pending_vectors: vectors, pending_invalidation: invalidation, deferred_stores: deferred } })
    for (const clause of present) expect(message).toContain(clause)
    for (const clause of absent) expect(message).not.toContain(clause)
    // A zero unit is dropped, never printed as "0 …" and never left as a dangling separator.
    expect(message).not.toMatch(/\b0 /)
    expect(message).not.toMatch(/,\s*(and\s*)?\./)
    expect(message).not.toContain('{{')
  })

  it('joins the surviving clauses with the locale list format, not a fixed three-slot sentence', async () => {
    await initI18n()
    await i18next.changeLanguage('en')
    const three = embeddingRepairMessage({ repair: { generation: 'r', pending_vectors: 5, pending_invalidation: 2, deferred_stores: 1 } })
    expect(three).toBe('The rebuild after the model change is still in progress across all memory stores: 5 memories still need a new vector, 2 open stores still hold old vectors to clear, and 1 closed or unavailable store will be rebuilt when it next opens.')
    const two = embeddingRepairMessage({ repair: { generation: 'r', pending_vectors: 5, pending_invalidation: 0, deferred_stores: 1 } })
    expect(two).toBe('The rebuild after the model change is still in progress across all memory stores: 5 memories still need a new vector and 1 closed or unavailable store will be rebuilt when it next opens.')
    const one = embeddingRepairMessage({ repair: { generation: 'r', pending_vectors: 0, pending_invalidation: 2, deferred_stores: 0 } })
    expect(one).toBe('The rebuild after the model change is still in progress across all memory stores: 2 open stores still hold old vectors to clear.')
    // Chinese joins with its own separators and no Latin conjunction.
    await i18next.changeLanguage('zh-CN')
    const zh = embeddingRepairMessage({ repair: { generation: 'r', pending_vectors: 5, pending_invalidation: 2, deferred_stores: 1 } })
    expect(zh).not.toContain('and')
    expect(zh).not.toContain(', ')
    expect(zh).toContain('、')
    expect(zh).toContain('和')
    expect(zh).not.toContain('{{')
  })

  it('renders a lone Japanese or Korean clause as a finished sentence, and lists them without a connective ending', async () => {
    // These two catalogs used to end the vectors/invalidation clauses with a
    // connective (…待っており / …があり, …있고 / …있으며) that only read correctly
    // as the first two of three fixed slots. A lone clause must now stand on
    // its own. Expected text is spelled out here, not read back from the catalog.
    await initI18n()
    await i18next.changeLanguage('ja')
    expect(embeddingRepairMessage({ repair: { generation: 'r', pending_vectors: 3, pending_invalidation: 0, deferred_stores: 0 } }))
      .toBe('モデル変更後の再構築がすべてのメモリストアで進行中です：3 件のメモリが新しいベクトルを待っています。')
    expect(embeddingRepairMessage({ repair: { generation: 'r', pending_vectors: 0, pending_invalidation: 2, deferred_stores: 0 } }))
      .toBe('モデル変更後の再構築がすべてのメモリストアで進行中です：2 件の開いているストアにはまだ消去待ちの古いベクトルがあります。')
    const jaAll = embeddingRepairMessage({ repair: { generation: 'r', pending_vectors: 3, pending_invalidation: 2, deferred_stores: 1 } })
    expect(jaAll).toContain('待っています')
    expect(jaAll).not.toMatch(/待っており|があり[、。]/)
    await i18next.changeLanguage('ko')
    expect(embeddingRepairMessage({ repair: { generation: 'r', pending_vectors: 3, pending_invalidation: 0, deferred_stores: 0 } }))
      .toBe('모델 변경 후 재생성이 모든 메모리 저장소에서 아직 진행 중입니다: 3개의 메모리가 새 벡터를 기다리고 있습니다.')
    expect(embeddingRepairMessage({ repair: { generation: 'r', pending_vectors: 0, pending_invalidation: 2, deferred_stores: 0 } }))
      .toBe('모델 변경 후 재생성이 모든 메모리 저장소에서 아직 진행 중입니다: 2개의 열린 저장소에 아직 지워야 할 이전 벡터가 있습니다.')
    const koAll = embeddingRepairMessage({ repair: { generation: 'r', pending_vectors: 3, pending_invalidation: 2, deferred_stores: 1 } })
    expect(koAll).toContain('기다리고 있습니다')
    expect(koAll).not.toMatch(/있고[,.]|있으며[,.]/)
  })

  it('keeps zero-only and unknown states apart from a pending one', () => {
    // All three zero: nothing pending, nothing rendered (the card hides the line).
    expect(embeddingRepairMessage({ repair: { generation: 'r', pending_vectors: 0, pending_invalidation: 0, deferred_stores: 0 } })).toBe('')
    // Missing counts read as zero, not as an unknown scope.
    expect(embeddingRepairMessage({ repair: { generation: 'r', pending_vectors: 0 } })).toBe('')
    // Unknown scope wins over any counts, including all-zero ones.
    const unknown = embeddingRepairMessage({ repair: { generation: 'r', unknown_scope: true, pending_vectors: 0, pending_invalidation: 0, deferred_stores: 0 } })
    expect(unknown).toContain('not confirmed')
    expect(unknown).not.toContain('still in progress')
    // No generation means no standing rebuild, whatever the counts say.
    expect(embeddingRepairMessage({ repair: { pending_vectors: 3, pending_invalidation: 1, deferred_stores: 1 } })).toBe('')
  })

  it('uses each locale plural category for each independent unit', async () => {
    await initI18n()
    for (const language of Object.keys(CATALOGS).filter(code => code !== 'en-XA')) {
      await i18next.changeLanguage(language)
      const catalog = i18next.getResourceBundle(language, 'translation').pages.overview.vectorMemoryCard
      for (const count of [0, 1, 2, 5, 21, 1000000]) {
        const category = new Intl.PluralRules(language).select(count)
        const message = embeddingRepairMessage({ repair: { generation: 'r', pending_vectors: count, pending_invalidation: 1, deferred_stores: 2 } })
        const vectorsClause = catalog[`repair_vectors_${category}`].replace('{{count}}', String(count))
        if (count === 0) expect(message).not.toContain(vectorsClause)
        else expect(message).toContain(vectorsClause)
        expect(message).toContain(catalog[`repair_invalidation_${new Intl.PluralRules(language).select(1)}`].replace('{{count}}', '1'))
        expect(message).toContain(catalog[`repair_deferred_${new Intl.PluralRules(language).select(2)}`].replace('{{count}}', '2'))
        expect(message).not.toContain('{{')
      }
      // A single surviving clause needs no list glue in any language.
      const single = embeddingRepairMessage({ repair: { generation: 'r', pending_vectors: 0, pending_invalidation: 0, deferred_stores: 2 } })
      expect(single).toBe(catalog.repair_pending.replace('{{items}}', catalog[`repair_deferred_${new Intl.PluralRules(language).select(2)}`].replace('{{count}}', '2')))
      for (const step of ['idle', 'checking', 'downloading', 'installing_faiss', 'verifying', 'waiting_retry', 'ready', 'done', 'error', 'failed', 'future_step']) {
        const label = embeddingSetupStepLabel(step, 3)
        expect(label).not.toBe(step)
        expect(label).not.toContain('pages.overview')
        expect(label).not.toContain('{{')
        expect(label).not.toBe('')
      }
    }
  })
})
