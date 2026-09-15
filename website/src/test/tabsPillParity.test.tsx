// Two components render the pill switcher — `ui/tabs.tsx` (Radix, for a tab that
// owns a panel) and `Tablist` (a tablist with no panel, for a shared body). A user
// reading a page cannot see which accessibility shape is underneath, so they must
// not be able to see a difference either.
//
// What is pinned here is that the shared recipe in `ui/tabsPill.ts` is what both
// actually render, rather than each carrying its own copy of the metrics. A test
// comparing two hand-written copies would only prove they agreed on the day it was
// written; this fails the moment either component stops going through the recipe.
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, within } from '@testing-library/react'

import Tablist from '../components/Tablist'
import { Tabs, TabsCount, TabsList, TabsTrigger } from '../components/ui/tabs'
import {
  TABS_INDICATOR_CLASS,
  TABS_SEGMENT_CLASS,
  TABS_TRACK_CLASS,
} from '../components/ui/tabsPill'

type Key = 'one' | 'two'

const TABS = [
  { key: 'one' as Key, label: 'One' },
  { key: 'two' as Key, label: 'Two', count: 3 },
]

afterEach(cleanup)

/** Every class in `recipe` must be present on `el`, in any order. */
function carriesRecipe(el: HTMLElement, recipe: string): string[] {
  const have = new Set(el.className.split(/\s+/).filter(Boolean))
  return recipe.split(/\s+/).filter(Boolean).filter(c => !have.has(c))
}

describe('the pill switcher renders one shared class recipe', () => {
  it('gives both components the same track', () => {
    const { unmount } = render(
      <Tablist<Key> tabs={TABS} value="one" onChange={() => {}} ariaLabel="Rail" />,
    )
    const railTrack = screen.getByRole('tablist')
    expect(carriesRecipe(railTrack, TABS_TRACK_CLASS)).toEqual([])
    unmount()

    render(
      <Tabs value="one">
        <TabsList aria-label="Rail">
          <TabsTrigger value="one">One</TabsTrigger>
          <TabsTrigger value="two">Two</TabsTrigger>
        </TabsList>
      </Tabs>,
    )
    expect(carriesRecipe(screen.getByRole('tablist'), TABS_TRACK_CLASS)).toEqual([])
  })

  it('gives both components the same segment, and mounts the sliding indicator on the selected one', () => {
    const { unmount } = render(
      <Tablist<Key> tabs={TABS} value="two" onChange={() => {}} ariaLabel="Rail" />,
    )
    const railSelected = screen.getByRole('tab', { selected: true })
    expect(carriesRecipe(railSelected, TABS_SEGMENT_CLASS)).toEqual([])
    // The indicator is what slides; without it the selection would swap instantly
    // in one component and glide in the other.
    const railIndicator = railSelected.querySelector(`.${CSS.escape('absolute')}`)
    expect(railIndicator).not.toBeNull()
    expect(carriesRecipe(railIndicator as HTMLElement, TABS_INDICATOR_CLASS)).toEqual([])
    unmount()

    render(
      <Tabs value="two">
        <TabsList aria-label="Rail">
          <TabsTrigger value="one">One</TabsTrigger>
          <TabsTrigger value="two">Two</TabsTrigger>
        </TabsList>
      </Tabs>,
    )
    const radixSelected = screen.getByRole('tab', { selected: true })
    expect(carriesRecipe(radixSelected, TABS_SEGMENT_CLASS)).toEqual([])
    const radixIndicator = radixSelected.querySelector(`.${CSS.escape('absolute')}`)
    expect(radixIndicator).not.toBeNull()
    expect(carriesRecipe(radixIndicator as HTMLElement, TABS_INDICATOR_CLASS)).toEqual([])
  })

  it('hides a zero count in both, and shows a real one', () => {
    const zeroed = [
      { key: 'one' as Key, label: 'One', count: 0 },
      { key: 'two' as Key, label: 'Two', count: 3 },
    ]
    const { unmount } = render(
      <Tablist<Key> tabs={zeroed} value="one" onChange={() => {}} ariaLabel="Rail" />,
    )
    expect(within(screen.getByRole('tab', { name: /One/ })).queryByText('0')).toBeNull()
    expect(within(screen.getByRole('tab', { name: /Two/ })).getByText('3')).toBeTruthy()
    unmount()

    render(
      <Tabs value="one">
        <TabsList aria-label="Rail">
          <TabsTrigger value="one">One<TabsCount value={0} /></TabsTrigger>
          <TabsTrigger value="two">Two<TabsCount value={3} /></TabsTrigger>
        </TabsList>
      </Tabs>,
    )
    expect(within(screen.getByRole('tab', { name: /One/ })).queryByText('0')).toBeNull()
    expect(within(screen.getByRole('tab', { name: /Two/ })).getByText('3')).toBeTruthy()
  })

  it('is the ONE difference between them: only Radix claims a panel', () => {
    // The whole reason both exist. If Tablist ever grew `aria-controls`, it would
    // point at nothing; if Radix ever lost it, its panel would stop being linked.
    const { unmount } = render(
      <Tablist<Key> tabs={TABS} value="one" onChange={() => {}} ariaLabel="Rail" />,
    )
    for (const tab of screen.getAllByRole('tab')) {
      expect(tab.getAttribute('aria-controls')).toBeNull()
    }
    unmount()

    render(
      <Tabs value="one">
        <TabsList aria-label="Rail">
          <TabsTrigger value="one">One</TabsTrigger>
        </TabsList>
      </Tabs>,
    )
    expect(screen.getByRole('tab').getAttribute('aria-controls')).toBeTruthy()
  })
})
