import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import DeveloperPage from '../DeveloperPage'
import { isKasEngineHighlight, KAS_ENGINE_PATH } from './kasEngineLink'

/** The engine switch's id while it sat in Developer > Feature previews. */
const OLD_ID = 'developer.run-kas-on-kiro-crew-s-own-engine'

function Where() {
  const { pathname, search } = useLocation()
  return <div data-testid="where">{`${pathname}${search}`}</div>
}

describe('engine choice links', () => {
  it.each([
    [OLD_ID, true],
    ['agent.engine', true],
    ['key:agent.kas_bundle', true],
    ['developer.chat-on-a-crew', false],
    [null, false],
  ])('reads %s as the engine choice: %s', (highlight, expected) => {
    expect(isKasEngineHighlight(highlight)).toBe(expected)
  })

  it.each([OLD_ID, 'key:agent.kas_bundle'])(
    'forwards an old Feature previews link for %s to the KAS row on Agent Harness',
    async highlight => {
      render(
        <MemoryRouter initialEntries={[`/developer?tab=feature-previews&highlight=${encodeURIComponent(highlight)}`]}>
          <Routes>
            <Route path="/developer" element={<DeveloperPage />} />
            <Route path="*" element={<Where />} />
          </Routes>
        </MemoryRouter>,
      )
      await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(KAS_ENGINE_PATH))
    },
  )
})
