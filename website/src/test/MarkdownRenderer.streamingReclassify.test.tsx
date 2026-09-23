import { describe, it, expect } from 'vitest'
import { fireEvent, render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * A streaming tick that reclassifies an EARLIER line must not re-create the
 * blocks below it. See kirodotdev/KiroCrew#10893.
 *
 * Asserted on DOM node identity rather than pixels: jsdom has no layout engine,
 * and node identity across a re-render is engine-independent React behaviour, so
 * a re-created node is what a real browser would then lay out afresh.
 *
 * The waits matter. `useBlockAssembler` throttles re-parsing while streaming, so
 * a synchronous rerender changes nothing at all and every assertion below would
 * pass against a DOM that never saw the second tick.
 */
const THROTTLE_GRACE_MS = 160

const settle = () => new Promise((resolve) => setTimeout(resolve, THROTTLE_GRACE_MS))

const blocks = (container: HTMLElement) =>
  Array.from(container.querySelectorAll('p,h1,h2,h3,li'))

const find = (container: HTMLElement, needle: string) =>
  blocks(container).find((node) => node.textContent?.includes(needle))

const INTRO = 'Intro line'
const BODY = 'Body paragraph that has already settled.'
const SETTLED = 'already settled'

describe('streaming markdown keeps settled blocks', () => {
  it('keeps the paragraph below a line a later tick turns into a setext heading', async () => {
    const { container, rerender } = render(
      <MarkdownRenderer content={`${INTRO}\n\n${BODY}`} streaming />,
    )
    await settle()
    const bodyBefore = find(container, SETTLED)
    expect(bodyBefore, 'the body paragraph renders on the first tick').toBeTruthy()

    rerender(<MarkdownRenderer content={`${INTRO}\n===\n\n${BODY}`} streaming />)
    await settle()

    // The reclassified line becoming a heading is correct and expected.
    expect(container.querySelector('h1')?.textContent).toContain(INTRO)
    expect(find(container, SETTLED)).toBe(bodyBefore)
  })

  it('keeps it when the later tick is a setext dash underline instead', async () => {
    const { container, rerender } = render(
      <MarkdownRenderer content={`${INTRO}\n\n${BODY}`} streaming />,
    )
    await settle()
    const bodyBefore = find(container, SETTLED)
    expect(bodyBefore).toBeTruthy()

    rerender(<MarkdownRenderer content={`${INTRO}\n---\n\n${BODY}`} streaming />)
    await settle()

    expect(container.querySelector('h2')?.textContent).toContain(INTRO)
    expect(find(container, SETTLED)).toBe(bodyBefore)
  })

  it('keeps every settled block when a heading is inserted above two of them', async () => {
    const first = 'First settled paragraph.'
    const second = 'Second settled paragraph.'
    const { container, rerender } = render(
      <MarkdownRenderer content={`${INTRO}\n\n${first}\n\n${second}`} streaming />,
    )
    await settle()
    const firstBefore = find(container, first)
    const secondBefore = find(container, second)
    expect(firstBefore).toBeTruthy()
    expect(secondBefore).toBeTruthy()

    rerender(<MarkdownRenderer content={`${INTRO}\n===\n\n${first}\n\n${second}`} streaming />)
    await settle()

    expect(find(container, first)).toBe(firstBefore)
    expect(find(container, second)).toBe(secondBefore)
  })

  it('still keeps settled blocks when the tail simply grows (control)', async () => {
    const { container, rerender } = render(
      <MarkdownRenderer content={`${INTRO}\n\n${BODY}`} streaming />,
    )
    await settle()
    const introBefore = find(container, INTRO)
    const bodyBefore = find(container, SETTLED)

    rerender(<MarkdownRenderer content={`${INTRO}\n\n${BODY}\n\n- item`} streaming />)
    await settle()

    expect(find(container, INTRO)).toBe(introBefore)
    expect(find(container, SETTLED)).toBe(bodyBefore)
  })

  it('leaves the rendered text and heading levels correct', async () => {
    const { container } = render(
      <MarkdownRenderer content={`${INTRO}\n===\n\n${BODY}`} streaming />,
    )
    await settle()
    expect(container.querySelector('h1')?.textContent).toContain(INTRO)
    expect(container.textContent).toContain(BODY)
    // The wrapper that stabilises keys wraps the block itself and adds no layout box.
    const wrapper = container.querySelector('h1')?.parentElement
    expect(wrapper?.tagName).toBe('DIV')
    expect(wrapper?.getAttribute('style')).toContain('display: contents')
  })

  /**
   * Keying the wrappers by position is what makes a settled block keep its node,
   * but it also means a block arriving ABOVE shifts every slot below it. React
   * then hands the instance at that slot a different `src` instead of remounting
   * it, because the element type is unchanged. Any state describing the previous
   * image's load therefore has to be cleared, or a good image renders as broken
   * with nothing short of a full remount to fix it.
   */
  it('does not carry one image failure onto a different image in a reused slot', async () => {
    const ALPHA = '![alpha](/alpha.png)'
    const BETA = '![beta](/beta.png)'
    const { container, rerender } = render(
      <MarkdownRenderer content={`${ALPHA}\n\n${BETA}`} streaming />,
    )
    await settle()

    // Fail the SECOND image, so the second slot is the one carrying a failure.
    const beta = container.querySelector('img[alt="beta"]')
    expect(beta, 'both images render on the first tick').toBeTruthy()
    fireEvent.error(beta!)
    expect(
      container.querySelector('img[alt="beta"]'),
      'the failed image gives way to the broken-image chip',
    ).toBeNull()

    // A paragraph arriving above shifts alpha down into the slot beta occupied.
    rerender(<MarkdownRenderer content={`${INTRO}\n\n${ALPHA}\n\n${BETA}`} streaming />)
    await settle()

    expect(
      container.querySelector('img[alt="alpha"]'),
      'alpha loads fine and must not inherit the failure recorded against beta',
    ).toBeTruthy()
  })
})
