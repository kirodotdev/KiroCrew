/**
 * PersonalShopperPage — management panel for the Personal Shopper advisor.
 *
 * Three tabs: Preferences, History, Sites. Chat lives in the main chat
 * surface — the "Start conversation" button creates a session pinned to
 * the personal-shopper-advisor agent and navigates there.
 */

import { useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { MessageCirclePlus, ShoppingBag } from 'lucide-react'
import { Btn, PageHeader } from '../../components/ui'
import SegmentedControl from '../../components/SegmentedControl'
import { PreferencesTab } from './PreferencesTab'
import { HistoryTab } from './HistoryTab'
import { SitesTab } from './SitesTab'
import { useAppDispatch } from '../../store'
import { createSlot } from '../../store/chatSlice'

import { i18nT } from '../../i18n/t'

type Tab = 'preferences' | 'history' | 'sites'

const SEGMENTS: { key: Tab; labelKey: string }[] = [
  { key: 'preferences', labelKey: 'apps.personalShopper.personalShopperPage.tab_preferences' },
  { key: 'history', labelKey: 'apps.personalShopper.personalShopperPage.tab_history' },
  { key: 'sites', labelKey: 'apps.personalShopper.personalShopperPage.tab_sites' },
]

const ADVISOR_AGENT = 'personal-shopper-advisor'

export default function PersonalShopperPage() {
  const [activeTab, setActiveTab] = useState<Tab>('preferences')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const dispatch = useAppDispatch()
  const navigate = useNavigate()

  const startAdvisorSession = useCallback(async () => {
    setCreating(true)
    setCreateError(null)
    try {
      await dispatch(createSlot({ agent: ADVISOR_AGENT })).unwrap()
      navigate('/chat')
    } catch (e) {
      // Without this the rejection was unhandled and the button just cleared its
      // spinner, so a failed create looked identical to nothing happening.
      setCreateError(e instanceof Error ? e.message : 'unknown')
    } finally {
      setCreating(false)
    }
  }, [dispatch, navigate])

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title={i18nT('apps.personalShopper.personalShopperPage.personal_shopper')}
        subtitle={i18nT('apps.personalShopper.personalShopperPage.your_life_problem_advisor_solves_first_shops_sec')}
      />

      {/* Hero CTA — prominent start-conversation area */}
      <div className="mx-4 mb-4 p-4 rounded-xl bg-[var(--card)] border border-[var(--border)] flex items-center gap-4">
        <div className="w-10 h-10 rounded-lg bg-[var(--accent-subtle)] flex items-center justify-center flex-shrink-0">
          <ShoppingBag size={20} className="text-[var(--accent)]" />
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium text-[var(--text)]">
            {i18nT('apps.personalShopper.personalShopperPage.start_a_conversation_with_your_personal_advisor')}
          </p>
          <p className="text-xs text-[var(--muted)] mt-0.5">
            {i18nT('apps.personalShopper.personalShopperPage.say_something_like_help_me_find_running_shoes_or')}
          </p>
          {createError && (
            <p role="alert" className="text-xs text-[var(--danger)] mt-1">
              {i18nT('apps.personalShopper.personalShopperPage.start_conversation_failed', { code: createError })}
            </p>
          )}
        </div>
        <Btn onClick={startAdvisorSession} disabled={creating} primary>
          <MessageCirclePlus size={14} />
          {i18nT('apps.personalShopper.personalShopperPage.start_advisor_conversation')}
        </Btn>
      </div>

      <div className="px-4 pb-2">
        <SegmentedControl
          value={activeTab}
          onChange={(v) => setActiveTab(v as Tab)}
          segments={SEGMENTS.map((s) => ({ key: s.key, label: i18nT(s.labelKey) }))}
        />
      </div>

      <div className="flex-1 overflow-y-auto px-4 pb-4">
        {activeTab === 'preferences' && <PreferencesTab />}
        {activeTab === 'history' && <HistoryTab />}
        {activeTab === 'sites' && <SitesTab />}
      </div>
    </div>
  )
}
