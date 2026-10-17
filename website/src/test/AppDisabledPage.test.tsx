import type { ReactElement } from 'react'
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import AppDisabledPage, { AppDisabledNotice } from '../components/AppDisabledPage'
import { i18nT } from '../i18n/t'

const renderAt = (page: ReactElement) =>
  render(
    <MemoryRouter initialEntries={['/zzq-page']}>
      <Routes>
        <Route path="/zzq-page" element={page} />
        <Route path="/apps" element={<div data-testid="zzq-apps-route" />} />
      </Routes>
    </MemoryRouter>,
  )

const askAgent = () => new RegExp(i18nT('components.askAgent.ask_the_agent'), 'i')
const disabledText = () => i18nT('components.appHost.this_app_is_disabled')
const enableText = () => i18nT('components.appHost.enable_this_app_from_apps_to_use_it')
const appsButton = () => screen.getByRole('button', { name: new RegExp(i18nT('components.appHost.apps')) })

describe('AppDisabledPage', () => {
  it('names the page it replaces, says the app is disabled and offers the Apps page', () => {
    renderAt(<AppDisabledPage title="Zzq Page" />)

    expect(screen.getByTestId('page-title')).toHaveTextContent('Zzq Page')
    expect(screen.getByTestId('page-subtitle')).toHaveTextContent(disabledText())
    expect(screen.getByText(enableText())).toBeInTheDocument()
    // AppHost's form: the app's own record says it is off, so nothing failed and nothing is an error.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    fireEvent.click(appsButton())
    expect(screen.getByTestId('zzq-apps-route')).toBeInTheDocument()
  })
})

describe('AppDisabledNotice', () => {
  it('is the plain line and the way to Apps, with no header of its own, when nothing failed', () => {
    renderAt(<AppDisabledNotice />)

    expect(screen.getByText(enableText())).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByTestId('page-title')).not.toBeInTheDocument()
    expect(screen.queryByTestId('page-subtitle')).not.toBeInTheDocument()

    fireEvent.click(appsButton())
    expect(screen.getByTestId('zzq-apps-route')).toBeInTheDocument()
  })

  it('renders a refused call through ErrorNotice with the agent hand-off, and says the app is disabled itself', () => {
    renderAt(<AppDisabledNotice refusal={new Error("app 'zzq' is not enabled")} />)

    const notice = screen.getByRole('alert')
    // The page header around it says something else, so the notice carries the app's state.
    expect(notice).toHaveTextContent(disabledText())
    expect(notice).toHaveTextContent(enableText())
    expect(within(notice).getByRole('button', { name: askAgent() })).toBeInTheDocument()
    expect(screen.queryByTestId('page-title')).not.toBeInTheDocument()
    expect(appsButton()).toBeInTheDocument()
  })
})
