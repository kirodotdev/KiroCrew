import { render, screen, fireEvent } from '@testing-library/react'
import {
  Card, CardTitle, Btn, SendBtn, IconButton, IconButtonGroup, Input, SearchInput,
  Badge, SourceBadge, StatCard, Skeleton, ContentSkeleton, SkeletonToggleRow,
  SkeletonField, SkeletonInfoRow, FormSkeleton, EmptyState, FilteredEmpty,
  PanelSectionHeader, PageHeader, Toggle, Slider, Checkbox,
} from './ui'
import { i18nT } from '../i18n/t'

describe('ui containers', () => {
  it('Card and CardTitle keep their base classes while merging an override', () => {
    render(
      <Card className="mb-0" data-testid="zzq-card">
        <CardTitle className="mb-0" data-testid="zzq-title">zzq-heading</CardTitle>
      </Card>,
    )
    const card = screen.getByTestId('zzq-card')
    expect(card.className).toContain('bg-card')
    // twMerge must drop the base `mb-4`, not append to it.
    expect(card.className).toContain('mb-0')
    expect(card.className).not.toContain('mb-4')
    expect(screen.getByTestId('zzq-title').tagName).toBe('H3')
  })

  it('PageHeader renders the title alone, or with a subtitle and actions', () => {
    const { rerender } = render(<PageHeader title="zzq-page" />)
    expect(screen.getByTestId('page-title')).toHaveTextContent('zzq-page')
    expect(screen.queryByTestId('page-subtitle')).toBeNull()

    rerender(<PageHeader title="zzq-page" subtitle="zzq-sub" actions={<Btn>zzq-act</Btn>} />)
    expect(screen.getByTestId('page-subtitle')).toHaveTextContent('zzq-sub')
    expect(screen.getByRole('button', { name: 'zzq-act' })).toBeInTheDocument()
  })

  it('PanelSectionHeader omits the count token when no count is given', () => {
    const { rerender } = render(<PanelSectionHeader label="zzq-group" />)
    expect(screen.getByTestId('panel-section-header')).toHaveTextContent('zzq-group')
    expect(screen.getByTestId('panel-section-header').textContent).toBe('zzq-group')

    // A zero count is still rendered — only `undefined` hides the token.
    rerender(
      <PanelSectionHeader label="zzq-group" count={0} trailing={<span>zzq-pulse</span>} />,
    )
    expect(screen.getByTestId('panel-section-header')).toHaveTextContent('0')
    expect(screen.getByText('zzq-pulse')).toBeInTheDocument()
  })
})

describe('ui buttons and inputs', () => {
  it('Btn switches its emphasis between primary, danger and plain', () => {
    const { rerender } = render(<Btn primary>zzq</Btn>)
    expect(screen.getByRole('button').className).toContain('bg-accent')

    rerender(<Btn danger>zzq</Btn>)
    // Idle, not hover-gated: a touch viewport never produces hover (#3937).
    expect(screen.getByRole('button').className).toContain('text-danger')

    rerender(<Btn>zzq</Btn>)
    expect(screen.getByRole('button').className).toContain('hover:bg-bg-hover')
  })

  it('Btn forwards its ref and its click', () => {
    const onClick = vi.fn()
    let node: HTMLButtonElement | null = null
    render(<Btn ref={el => { node = el }} onClick={onClick}>zzq</Btn>)
    fireEvent.click(screen.getByRole('button'))
    expect(onClick).toHaveBeenCalledTimes(1)
    expect(node).toBeInstanceOf(HTMLButtonElement)
  })

  it('SendBtn honours disabled and passes style through', () => {
    const onClick = vi.fn()
    render(<SendBtn onClick={onClick} disabled style={{ width: '3px' }}>zzq-send</SendBtn>)
    const btn = screen.getByRole('button', { name: 'zzq-send' })
    expect(btn).toBeDisabled()
    expect(btn.style.width).toBe('3px')
    fireEvent.click(btn)
    expect(onClick).not.toHaveBeenCalled()
  })

  it('IconButton picks the class set for each variant and defaults to type=button', () => {
    const { rerender } = render(<IconButton aria-label="zzq-icon">x</IconButton>)
    const btn = screen.getByRole('button', { name: 'zzq-icon' })
    expect(btn).toHaveAttribute('type', 'button')
    expect(btn.className).toContain('hover:text-text')

    rerender(<IconButton aria-label="zzq-icon" variant="accent">x</IconButton>)
    expect(screen.getByRole('button', { name: 'zzq-icon' }).className)
      .toContain('hover:text-accent')
    rerender(<IconButton aria-label="zzq-icon" variant="danger">x</IconButton>)
    expect(screen.getByRole('button', { name: 'zzq-icon' }).className)
      .toContain('hover:bg-danger-subtle')
    rerender(<IconButton aria-label="zzq-icon" variant="active">x</IconButton>)
    expect(screen.getByRole('button', { name: 'zzq-icon' }).className)
      .toContain('text-accent')
  })

  it('IconButtonGroup only adds the hover-reveal classes when asked', () => {
    const { rerender, container } = render(
      <IconButtonGroup><IconButton aria-label="zzq-icon">x</IconButton></IconButtonGroup>,
    )
    expect(container.firstElementChild!.className).not.toContain('group-hover:opacity-100')

    rerender(
      <IconButtonGroup reveal className="zzq-extra">
        <IconButton aria-label="zzq-icon">x</IconButton>
      </IconButtonGroup>,
    )
    expect(container.firstElementChild!.className).toContain('group-hover:opacity-100')
    expect(container.firstElementChild!.className).toContain('zzq-extra')
  })

  it('Input forwards its ref and its value changes', () => {
    const onChange = vi.fn()
    let node: HTMLInputElement | null = null
    render(<Input ref={el => { node = el }} onChange={onChange} placeholder="zzq-ph" />)
    fireEvent.change(screen.getByPlaceholderText('zzq-ph'), { target: { value: 'zzq-v' } })
    expect(onChange).toHaveBeenCalled()
    expect(node).toBeInstanceOf(HTMLInputElement)
  })

  it('SearchInput wraps the field with the magnifier decoration', () => {
    const { container } = render(<SearchInput className="zzq-wrap" placeholder="zzq-ph" />)
    expect(container.firstElementChild!.className).toContain('zzq-wrap')
    expect(container.querySelector('svg')).not.toBeNull()
    expect(screen.getByPlaceholderText('zzq-ph')).toBeInTheDocument()
  })

  it('Checkbox renders a themed checkbox and merges caller styles', () => {
    const onChange = vi.fn()
    render(<Checkbox checked={false} onChange={onChange} style={{ width: '5px' }} />)
    const box = screen.getByRole('checkbox') as HTMLInputElement
    expect(box.type).toBe('checkbox')
    expect(box.style.width).toBe('5px')
    fireEvent.click(box)
    expect(onChange).toHaveBeenCalled()
  })
})

describe('ui badges', () => {
  it.each([
    ['ok', 'bg-ok-subtle'],
    ['err', 'bg-danger-subtle'],
    ['aim', 'bg-aim-subtle'],
    ['muted', 'var(--bg-hover)'],
    ['warn', 'bg-warn-subtle'],
  ] as const)('Badge variant %s uses its own palette', (variant, cls) => {
    render(<Badge variant={variant}>zzq-badge</Badge>)
    expect(screen.getByText('zzq-badge').className).toContain(cls)
  })

  it.each([
    ['package', 'bg-aim-subtle'],
    ['kirocrew', 'text-muted'],
    ['project', 'text-ok'],
    ['zzq-unknown', 'text-muted'],
  ])('SourceBadge %s renders its own chrome', (source, cls) => {
    render(<SourceBadge source={source} />)
    expect(screen.getByText(source).className).toContain(cls)
  })
})

describe('StatCard', () => {
  it('shows a skeleton until a value arrives', () => {
    const { rerender } = render(<StatCard label="zzq-label" />)
    expect(screen.getByTestId('stat-card-skeleton')).toBeInTheDocument()
    expect(screen.queryByTestId('stat-card-value')).toBeNull()

    rerender(<StatCard label="zzq-label" value={null} />)
    expect(screen.getByTestId('stat-card-skeleton')).toBeInTheDocument()

    // 0 is a value, not a missing one.
    rerender(<StatCard label="zzq-label" value={0} />)
    expect(screen.getByTestId('stat-card-value')).toHaveTextContent('0')
  })

  it('is inert and unfocusable without an onClick', () => {
    render(<StatCard label="zzq-label" value="7" />)
    const card = screen.getByTestId('stat-card')
    expect(card).not.toHaveAttribute('role')
    expect(card).not.toHaveAttribute('tabindex')
  })

  it('becomes a keyboard-operable button when clickable', () => {
    const onClick = vi.fn()
    render(<StatCard label="zzq-label" value="7" onClick={onClick} active accent delay={40} />)
    const card = screen.getByRole('button')
    expect(card).toHaveAttribute('tabindex', '0')
    expect(card.className).toContain('border-accent')
    expect(card.style.animationDelay).toBe('40ms')
    expect(screen.getByTestId('stat-card-value').className).toContain('text-accent')

    fireEvent.click(card)
    fireEvent.keyDown(card, { key: 'Enter' })
    fireEvent.keyDown(card, { key: ' ' })
    expect(onClick).toHaveBeenCalledTimes(3)

    // Any other key is left to the browser.
    fireEvent.keyDown(card, { key: 'a' })
    expect(onClick).toHaveBeenCalledTimes(3)
  })

  it('renders an info tip beside the label when a title is supplied', () => {
    render(<StatCard label="zzq-label" value="7" title="zzq-explainer" colorClass="zzq-hue" />)
    expect(screen.getByTestId('stat-card-label')).toHaveTextContent('zzq-label')
    expect(screen.getByRole('button')).toBeInTheDocument() // the InfoTip trigger
    expect(screen.getByTestId('stat-card-value').className).toContain('zzq-hue')
  })

  it('lets a consumer override the shared test id', () => {
    render(<StatCard label="zzq-label" value="7" data-testid="zzq-own-id" />)
    expect(screen.getByTestId('zzq-own-id')).toBeInTheDocument()
  })
})

describe('ui skeletons', () => {
  it('Skeleton merges a size override onto the pulse box', () => {
    const { container } = render(<Skeleton className="h-2" data-testid="zzq-skel" />)
    expect(container.querySelector('[data-slot="skeleton"]')).not.toBeNull()
    expect(screen.getByTestId('zzq-skel').className).toContain('animate-pulse')
    expect(screen.getByTestId('zzq-skel').className).toContain('h-2')
  })

  it('ContentSkeleton draws one row per requested row plus two header bars', () => {
    const { container } = render(<ContentSkeleton rows={3} />)
    // 2 header bars + 3 rows × 2 boxes.
    expect(container.querySelectorAll('[data-slot="skeleton"]')).toHaveLength(8)
  })

  it('ContentSkeleton defaults to five rows', () => {
    const { container } = render(<ContentSkeleton />)
    expect(container.querySelectorAll('[data-slot="skeleton"]')).toHaveLength(12)
  })

  it.each([
    [SkeletonToggleRow, 'rounded-full'],
    [SkeletonField, 'w-full'],
    [SkeletonInfoRow, 'w-16'],
  ] as const)('each form-row skeleton draws its own pair of boxes', (Row, marker) => {
    const { container } = render(<Row />)
    const boxes = container.querySelectorAll('[data-slot="skeleton"]')
    expect(boxes).toHaveLength(2)
    expect(boxes[1].className).toContain(marker)
  })

  it('FormSkeleton maps every row kind onto its own shape', () => {
    const { container } = render(<FormSkeleton rows={['toggle', 'info', 'field']} />)
    // toggle + info + field = 3 rows, 2 boxes each.
    expect(container.querySelectorAll('[data-slot="skeleton"]')).toHaveLength(6)
    expect(container.querySelectorAll('.rounded-full')).toHaveLength(1)
  })
})

describe('EmptyState and FilteredEmpty', () => {
  it('renders only the parts it was given, under the default test id', () => {
    render(<EmptyState icon={<span>zzq-icon</span>} title="zzq-empty" />)
    expect(screen.getByTestId('empty-state-title')).toHaveTextContent('zzq-empty')
    expect(screen.queryByTestId('empty-state-subtitle')).toBeNull()
    expect(screen.queryByTestId('empty-state-action')).toBeNull()
  })

  it('derives the subtitle and action ids from a caller-supplied base', () => {
    render(
      <EmptyState
        icon={<span>zzq-icon</span>}
        title="zzq-empty"
        subtitle="zzq-sub"
        action={<Btn>zzq-cta</Btn>}
        testId="zzq-base"
      />,
    )
    expect(screen.getByTestId('zzq-base-subtitle')).toHaveTextContent('zzq-sub')
    expect(screen.getByTestId('zzq-base-action')).toBeInTheDocument()
  })

  it('FilteredEmpty echoes the query back and clears the filter', () => {
    const onClear = vi.fn()
    render(<FilteredEmpty query="zzq-q" onClear={onClear} />)
    expect(screen.getByTestId('filtered-empty')).toHaveTextContent('zzq-q')
    expect(screen.getByTestId('filtered-empty')).toHaveTextContent(
      i18nT('components.ui.no_matches_for'),
    )
    fireEvent.click(screen.getByTestId('filtered-empty-clear'))
    expect(onClear).toHaveBeenCalledTimes(1)
  })

  it('FilteredEmpty names the noun when one is supplied', () => {
    render(
      <FilteredEmpty query="zzq-q" onClear={() => {}} noun="zzq-noun" testId="zzq-fe" />,
    )
    expect(screen.getByTestId('zzq-fe')).toHaveTextContent(
      i18nT('components.ui.no_noun_match', { noun: 'zzq-noun' }),
    )
    expect(screen.getByTestId('zzq-fe-clear')).toHaveTextContent(
      i18nT('components.ui.clear_filter'),
    )
  })
})

describe('Toggle', () => {
  it('flips on click and on both activation keys', () => {
    const onChange = vi.fn()
    render(<Toggle checked={false} onChange={onChange} label="zzq-switch" />)
    const sw = screen.getByRole('switch', { name: 'zzq-switch' })
    expect(sw).toHaveAttribute('aria-checked', 'false')
    expect(sw).toHaveAttribute('tabindex', '0')

    fireEvent.click(sw)
    fireEvent.keyDown(sw, { key: ' ' })
    fireEvent.keyDown(sw, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledTimes(3)
    expect(onChange).toHaveBeenLastCalledWith(true)

    // Any other key is left alone.
    fireEvent.keyDown(sw, { key: 'x' })
    expect(onChange).toHaveBeenCalledTimes(3)
  })

  it('announces the disabled state and refuses every activation', () => {
    const onChange = vi.fn()
    render(<Toggle checked onChange={onChange} disabled describedBy="zzq-desc" />)
    const sw = screen.getByRole('switch')
    expect(sw).toHaveAttribute('aria-disabled', 'true')
    expect(sw).toHaveAttribute('aria-describedby', 'zzq-desc')
    expect(sw).toHaveAttribute('tabindex', '-1')

    fireEvent.click(sw)
    fireEvent.keyDown(sw, { key: 'Enter' })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('drops the accent fill in the muted tone', () => {
    const { rerender } = render(<Toggle checked onChange={() => {}} />)
    expect(screen.getByRole('switch').className).toContain('bg-accent')
    rerender(<Toggle checked onChange={() => {}} tone="muted" />)
    expect(screen.getByRole('switch').className).toContain('bg-border-strong')
  })
})

describe('Slider pointer interaction', () => {
  /** The track has no layout in happy-dom, so give it a deterministic box. */
  const TRACK_WIDTH = 218 // 200px of usable travel once the 18px knob is removed
  let rect: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    rect = vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue({
      x: 0, y: 0, left: 0, top: 0, right: TRACK_WIDTH, bottom: 18,
      width: TRACK_WIDTH, height: 18, toJSON: () => ({}),
    } as DOMRect)
  })
  afterEach(() => { rect.mockRestore() })

  /** clientX for a given fraction of the usable travel. */
  const atFrac = (f: number) => f * (TRACK_WIDTH - 18) + 9

  it('click-to-seek snaps to the value under the pointer', () => {
    const onChange = vi.fn()
    render(<Slider value={0} onChange={onChange} min={0} max={100} step={10} />)
    fireEvent.pointerDown(screen.getByRole('slider'), { clientX: atFrac(0.5), pointerId: 1 })
    expect(onChange).toHaveBeenCalledWith(50)
  })

  it('clamps a seek beyond either end of the track', () => {
    const onChange = vi.fn()
    render(<Slider value={50} onChange={onChange} min={0} max={100} step={10} />)
    const track = screen.getByRole('slider')
    fireEvent.pointerDown(track, { clientX: -400, pointerId: 1 })
    expect(onChange).toHaveBeenLastCalledWith(0)
    fireEvent.pointerDown(track, { clientX: 4000, pointerId: 1 })
    expect(onChange).toHaveBeenLastCalledWith(100)
  })

  it('does not re-fire onChange when the seek lands on the current value', () => {
    const onChange = vi.fn()
    render(<Slider value={50} onChange={onChange} min={0} max={100} step={10} />)
    fireEvent.pointerDown(screen.getByRole('slider'), { clientX: atFrac(0.5), pointerId: 1 })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('shows the hover tooltip for the step under the cursor and hides it on leave', () => {
    render(
      <Slider value={0} onChange={() => {}} min={0} max={100} step={25}
        formatValue={v => `zzq-${v}`} />,
    )
    const track = screen.getByRole('slider')
    fireEvent.pointerMove(track, { clientX: atFrac(0.5), pointerId: 1 })
    expect(screen.getByText('zzq-50')).toBeInTheDocument()

    fireEvent.pointerLeave(track)
    expect(screen.queryByText('zzq-50')).toBeNull()
  })

  it('shows the raw value in the tooltip when there is no formatter', () => {
    render(<Slider value={0} onChange={() => {}} min={0} max={100} step={25} />)
    fireEvent.pointerMove(screen.getByRole('slider'), { clientX: atFrac(1), pointerId: 1 })
    expect(screen.getByText('100')).toBeInTheDocument()
  })

  it('a move while held drags the value and keeps the tooltip through the release', () => {
    const onChange = vi.fn()
    const { rerender } = render(
      <Slider value={0} onChange={onChange} min={0} max={100} step={1} />,
    )
    const track = screen.getByRole('slider')
    fireEvent.pointerDown(track, { clientX: atFrac(0), pointerId: 1 })
    fireEvent.pointerMove(track, { clientX: atFrac(0.25), pointerId: 1 })
    expect(onChange).toHaveBeenLastCalledWith(25)

    // A continuous slider jumps to the pointer while dragging.
    rerender(<Slider value={25} onChange={onChange} min={0} max={100} step={1} />)
    fireEvent.pointerMove(track, { clientX: atFrac(0.75), pointerId: 1 })
    expect(onChange).toHaveBeenLastCalledWith(75)

    // Leaving the track mid-drag must NOT drop the tooltip.
    fireEvent.pointerLeave(track)
    expect(screen.getByText('75')).toBeInTheDocument()

    fireEvent.pointerUp(track, { pointerId: 1 })
    fireEvent.pointerLeave(track)
    expect(screen.queryByText('75')).toBeNull()
  })

  it('a stray pointer-up outside a drag is ignored', () => {
    const onChange = vi.fn()
    render(<Slider value={10} onChange={onChange} step={1} />)
    fireEvent.pointerUp(screen.getByRole('slider'), { pointerId: 1 })
    fireEvent.pointerCancel(screen.getByRole('slider'), { pointerId: 1 })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('ignores every pointer gesture while disabled', () => {
    const onChange = vi.fn()
    render(<Slider value={10} onChange={onChange} step={1} disabled />)
    const track = screen.getByRole('slider')
    fireEvent.pointerDown(track, { clientX: atFrac(0.9), pointerId: 1 })
    fireEvent.pointerMove(track, { clientX: atFrac(0.9), pointerId: 1 })
    expect(onChange).not.toHaveBeenCalled()
    expect(track).toHaveAttribute('aria-disabled', 'true')
  })

  it('treats a non-positive step as a continuous range with no snapping', () => {
    const onChange = vi.fn()
    render(
      <Slider value={0} onChange={onChange} min={0} max={100} step={0} ticks={false} />,
    )
    fireEvent.pointerDown(screen.getByRole('slider'), { clientX: atFrac(0.333), pointerId: 1 })
    const got = onChange.mock.calls[0][0] as number
    expect(got).toBeGreaterThan(33)
    expect(got).toBeLessThan(34)
  })

  it('PageDown steps down by ten increments', () => {
    const onChange = vi.fn()
    render(<Slider value={50} onChange={onChange} min={0} max={100} step={2} />)
    fireEvent.keyDown(screen.getByRole('slider'), { key: 'PageDown' })
    expect(onChange).toHaveBeenCalledWith(30)
  })

  it('Shift+arrow uses the ×10 increment', () => {
    const onChange = vi.fn()
    render(<Slider value={50} onChange={onChange} min={0} max={100} step={2} />)
    const track = screen.getByRole('slider')
    fireEvent.keyDown(track, { key: 'ArrowUp', shiftKey: true })
    expect(onChange).toHaveBeenLastCalledWith(70)
    fireEvent.keyDown(track, { key: 'ArrowDown', shiftKey: true })
    expect(onChange).toHaveBeenLastCalledWith(30)
  })

  it('leaves an unhandled key to the browser', () => {
    const onChange = vi.fn()
    render(<Slider value={50} onChange={onChange} step={1} />)
    fireEvent.keyDown(screen.getByRole('slider'), { key: 'Tab' })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('renders the label, the formatted value and the max emphasis together', () => {
    render(
      <Slider value={100} onChange={() => {}} min={0} max={100} step={50}
        label="zzq-vol" showValue formatValue={v => `zzq-${v}%`} emphasizeMax
        className="zzq-cls" aria-label="zzq-aria" />,
    )
    expect(screen.getByText('zzq-vol')).toBeInTheDocument()
    expect(screen.getByText('zzq-100%')).toBeInTheDocument()
    expect(screen.getByRole('slider')).toHaveAttribute('aria-label', 'zzq-aria')
    expect(screen.getByRole('slider')).toHaveAttribute('aria-valuetext', 'zzq-100%')
  })

  it('shows the value alone when no label is given', () => {
    render(<Slider value={5} onChange={() => {}} min={0} max={10} step={1} showValue />)
    expect(screen.getByText('5')).toBeInTheDocument()
  })

  it('a degenerate min/max range still renders and reports its bounds', () => {
    render(<Slider value={7} onChange={() => {}} min={7} max={7} step={1} />)
    const track = screen.getByRole('slider')
    expect(track).toHaveAttribute('aria-valuenow', '7')
    expect(track).toHaveAttribute('aria-valuemin', '7')
  })
})
