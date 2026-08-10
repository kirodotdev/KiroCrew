/**
 * PersonalShopperPage — life-problem advisor with shopping capability.
 */

import { useState } from 'react'
import { ShoppingBag } from 'lucide-react'
import { PageHeader } from '../../components/ui'
import SegmentedControl from '../../components/SegmentedControl'
import { PreferencesTab } from './PreferencesTab'
import { HistoryTab } from './HistoryTab'
import { SitesTab } from './SitesTab'

import { i18nT } from '../../i18n/t'
type Tab = 'chat' | 'preferences' | 'history' | 'sites'

// Catalog keys, not resolved labels: this array is built at import time, so a
// translated string here would freeze whichever language was active then.
// Resolved at render instead -- same shape as `FILTER_LABEL_KEY` in ChatSidebar.
const SEGMENTS: { key: Tab; labelKey: string }[] = [
  { key: 'chat', labelKey: 'apps.personalShopper.personalShopperPage.tab_chat' },
  { key: 'preferences', labelKey: 'apps.personalShopper.personalShopperPage.tab_preferences' },
  { key: 'history', labelKey: 'apps.personalShopper.personalShopperPage.tab_history' },
  { key: 'sites', labelKey: 'apps.personalShopper.personalShopperPage.tab_sites' },
]

export default function PersonalShopperPage() {
  const [activeTab, setActiveTab] = useState<Tab>('preferences')

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title={i18nT('apps.personalShopper.personalShopperPage.personal_shopper')}
        subtitle={i18nT('apps.personalShopper.personalShopperPage.your_life_problem_advisor_solves_first_shops_sec')}
      />

      <div className="px-4 pb-2">
        <SegmentedControl
          value={activeTab}
          onChange={(v) => setActiveTab(v as Tab)}
          segments={SEGMENTS.map((s) => ({ key: s.key, label: i18nT(s.labelKey) }))}
        />
      </div>

      <div className="flex-1 overflow-y-auto px-4 pb-4">
        {activeTab === 'chat' && <ChatTab />}
        {activeTab === 'preferences' && <PreferencesTab />}
        {activeTab === 'history' && <HistoryTab />}
        {activeTab === 'sites' && <SitesTab />}
      </div>
    </div>
  )
}

function ChatTab() {
  return (
    <div className="flex flex-col items-center justify-center h-64 text-center">
      <ShoppingBag size={32} className="text-muted mb-3" />
      <p className="text-sm text-text">
        {i18nT('apps.personalShopper.personalShopperPage.start_a_conversation_with_your_personal_advisor')}
      </p>
      <p className="text-xs text-muted mt-2">
        {i18nT('apps.personalShopper.personalShopperPage.say_something_like_help_me_find_running_shoes_or')}
      </p>
    </div>
  )
}
