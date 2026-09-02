/**
 * The pending-approval row previews what the tool is about to do. When the
 * permission meta carries no `tool_input`, the last resort is the
 * agent-authored purpose line — and that reserved argument reaches us under
 * whatever name the model emitted (the declared `__tool_use_purpose`, its
 * camelCased echo, or a paraphrase like `__purpose`), so matching literals left
 * the preview blank for whole sessions at a time. Read by shape instead; see
 * `utils/toolPurpose`.
 */
import { describe, it, expect } from 'vitest'
import { renderWithProviders } from './helpers'
import CollapsibleToolGroup from '../pages/chat/CollapsibleToolGroup'

/** The preview only renders for a pending approval that can be acted on, so
 *  `hasPermission` + `onApprove` are both required to exercise it. */
const preview = (permissionMeta: Record<string, unknown>): string => {
  const { container } = renderWithProviders(
    <CollapsibleToolGroup count={1} hasPermission permissionMeta={permissionMeta} onApprove={() => {}}>
      <div>child</div>
    </CollapsibleToolGroup>,
  )
  return container.querySelector('pre')?.textContent ?? ''
}

describe('CollapsibleToolGroup purpose preview', () => {
  it('previews the purpose under the snake_case spelling', () => {
    expect(preview({ __tool_use_purpose: 'Check the harness render errors' }))
      .toContain('Check the harness render errors')
  })

  it('previews the purpose under the camelCase spelling', () => {
    expect(preview({ __toolUsePurpose: 'Check the harness render errors' }))
      .toContain('Check the harness render errors')
  })

  it('previews the purpose under a model-paraphrased spelling', () => {
    expect(preview({ __purpose: 'Check the harness render errors' }))
      .toContain('Check the harness render errors')
  })

  it('prefers a real tool_input command over the purpose', () => {
    const text = preview({
      tool_input: { command: 'node kc-shot.mjs' },
      __toolUsePurpose: 'Check the harness render errors',
    })
    expect(text).toContain('node kc-shot.mjs')
    expect(text).not.toContain('Check the harness render errors')
  })

  it('renders no preview when the purpose is blank', () => {
    expect(preview({ __toolUsePurpose: '   ' })).toBe('')
  })
})
