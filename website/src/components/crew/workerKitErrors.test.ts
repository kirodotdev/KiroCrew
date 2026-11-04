/**
 * workerKitErrors — every code the worker-kit routes refuse with reads as a
 * sentence, never as the code; anything else keeps the transport's message.
 */
import { describe, it, expect } from 'vitest'
import { ApiError } from '../../api/apiError'
import { i18nT } from '../../i18n/t'
import { workerKitErrorText } from './workerKitErrors'

const refusal = (code: string) => new ApiError(409, code, JSON.stringify({ error: code, code }))

describe('workerKitErrorText', () => {
  it.each([
    ['stale_revision', 'crewWorkerKit.error_stale_revision'],
    ['revision_required', 'crewWorkerKit.error_revision_required'],
    ['template_required', 'crewWorkerKit.error_template_required'],
    ['template_unreadable', 'crewWorkerKit.error_template_unreadable'],
    ['template_restrictions_uncarried', 'crewWorkerKit.error_template_restrictions_uncarried'],
    ['invalid_spec', 'crewWorkerKit.error_invalid_spec'],
    ['not_a_mate_file', 'crewWorkerKit.error_not_a_mate_file'],
    ['invalid_json', 'crewWorkerKit.error_invalid_request'],
    ['invalid_request', 'crewWorkerKit.error_invalid_request'],
  ])('reads %s as its sentence', (code, key) => {
    const text = workerKitErrorText(refusal(code))
    expect(text).toBe(i18nT(key))
    expect(text).not.toContain(code)
  })

  const named = (code: string, detail: string) =>
    new ApiError(409, code, JSON.stringify({ error: code, code, detail }))

  it.each([
    ['template_restrictions_uncarried', 'toolsSettings.execute_bash', 'crewWorkerKit.error_template_restrictions_uncarried_named', 'fields'],
    ['template_policy_backend_unsupported', 'hooks, permissions', 'crewWorkerKit.error_template_policy_backend', 'fields'],
  ])('names the field a %s refusal is about', (code, detail, key, slot) => {
    const text = workerKitErrorText(named(code, detail))
    expect(text).toBe(i18nT(key, { [slot]: detail }))
    expect(text).toContain(detail)
  })

  it('calls a mate a mate, never a crewmate', () => {
    const uncarried = workerKitErrorText(named('template_restrictions_uncarried', 'toolsSettings.execute_bash'))
    const backend = workerKitErrorText(named('template_policy_backend_unsupported', 'hooks, permissions'))
    expect(uncarried).toContain('this mate')
    expect(uncarried).not.toContain('crewmate')
    expect(backend).toContain("this mate's backend")
    expect(backend).toContain('Run the mate')
    expect(backend).not.toContain('crewmate')
  })

  it('points at the preview on screen, not at a button', () => {
    expect(workerKitErrorText(refusal('stale_revision'))).toContain('The changes below are up to date')
    expect(workerKitErrorText(refusal('revision_required'))).toBe('Check the changes below first, then add them.')
  })

  it('keeps the message of a failure that names no known code', () => {
    expect(workerKitErrorText(new ApiError(403, 'Only the owner can do this.', '{"error":"Only the owner can do this."}')))
      .toBe('Only the owner can do this.')
    // An inherited property name is not a code.
    expect(workerKitErrorText(refusal('toString'))).toBe('toString')
    expect(workerKitErrorText(new Error('network down'))).toBe('network down')
    expect(workerKitErrorText('plain')).toBe('plain')
  })

  it('has nothing to say without an error', () => {
    expect(workerKitErrorText(null)).toBeNull()
    expect(workerKitErrorText(undefined)).toBeNull()
  })
})
