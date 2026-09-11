import { describe, it, expect, vi } from 'vitest'
import { buildBody } from '../components/JobForm'
import type { CronPrefill } from '../utils/schedulePresets'

vi.mock('../api/client', () => ({ api: { createCron: vi.fn(), updateCron: vi.fn(), models: vi.fn() } }))

// The create body carries `source_preset` (the clicked preset id)
// AND `source_template_prompt` (the template's prompt AT PICK TIME). The
// snapshot is what makes the later "template updated" hint attributable, so it
// must ride the create body. Both are create-only.

const baseF = {
  name: 'Error Digest', message: 'msg', agent: '', model: '', channel: '',
  approvalMode: '', silent: false, strictSchedule: false, hideInChat: false,
  minimalContext: false, jobKind: 'message' as const,
  schedMode: 'interval' as const, intVal: 6, intUnit: 'hours' as const,
  weekDays: [] as number[], weekTime: '09:00', cronExpr: '',
}

const prefill = (over: Partial<CronPrefill> = {}): CronPrefill => ({
  name: 'Error Digest', message: 'Summarize errors.', schedMode: 'interval',
  sourcePreset: 'error-digest', sourceTemplatePrompt: 'Summarize errors.', ...over,
})

describe('buildBody template provenance', () => {
  it('emits both provenance fields on create from the prefill', () => {
    let error = ''
    const body = buildBody(baseF, 'UTC', e => { error = e }, false, prefill())
    expect(error).toBe('')
    expect(body).not.toBeNull()
    expect(body!.source_preset).toBe('error-digest')
    expect(body!.source_template_prompt).toBe('Summarize errors.')
  })

  it('captures the TEMPLATE prompt, not the edited message', () => {
    // The user edited the Message field (baseF.message = 'msg'), but the
    // snapshot must be the template's own prompt from pick time.
    const body = buildBody(baseF, 'UTC', () => {}, false, prefill({ sourceTemplatePrompt: 'The original template prompt.' }))
    expect(body!.source_template_prompt).toBe('The original template prompt.')
  })

  it('omits provenance on create when there is no prefill', () => {
    const body = buildBody(baseF, 'UTC', () => {}, false, undefined)
    expect(body).not.toBeNull()
    expect('source_preset' in body!).toBe(false)
    expect('source_template_prompt' in body!).toBe(false)
  })

  it('never emits provenance on edit, even with a prefill', () => {
    const body = buildBody(baseF, 'UTC', () => {}, true, prefill())
    expect(body).not.toBeNull()
    expect('source_preset' in body!).toBe(false)
    expect('source_template_prompt' in body!).toBe(false)
  })
})
