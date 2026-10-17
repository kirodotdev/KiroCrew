import { FadeUp } from './animations';
import { Zap, Shield, MessageSquare } from 'lucide-react';

const TIME_SAVINGS = [
  { task: 'Weekly report commentary', before: '2 hours', after: '5 min', ratio: '24x', source: 'Community report' },
  { task: 'Batch data evaluation', before: '45 min', after: '2 min', ratio: '22x', source: 'Community report' },
  { task: 'Oncall root cause investigation', before: '4+ hours', after: '30 min', ratio: '~8x', source: 'Community report' },
  { task: 'Morning oncall triage (10 metrics)', before: '30 min', after: '2 min', ratio: '15x', source: 'Community report' },
  { task: 'Overnight code review backlog', before: 'Next day', after: 'Processed overnight', ratio: 'Async', source: 'Community report' },
];

const ONCALL_CASES = [
  { team: 'A data platform team', desc: 'Cron missions: ticket polling every 5 min, CloudWatch alarms across multiple accounts, daily queue reports, schema drift detection, weekly handover journals.', impact: 'Full ops platform' },
  { team: 'A CI/CD reliability team', desc: 'Pipeline doctor: monitors pipelines, traces failures to specific commits, auto-fixes and raises PRs autonomously.', impact: 'Health improvement' },
  { team: 'A multi-team ops group', desc: 'Skills via Slack: Change Tracker, Pipeline Blocker Investigation, 24/7 Monitoring, Daily Standup Drafts, Oncall Reports.', impact: '8 skills, zero code' },
  { team: 'A DNS infrastructure team', desc: 'Automated ticket triage: polls resolver group every 2 min, posts findings as ticket worklogs.', impact: '2-min polling cycle' },
  { team: 'A content services team', desc: 'Morning cron: ticket queue analysis, pipeline status, over-SLA tickets, recurring ticket trends.', impact: 'Daily auto-triage' },
  { team: 'A hardware platform team', desc: 'Pipeline monitoring agent: monitors all pipelines, posts failure updates, cuts tickets to concerned teams.', impact: 'Auto-ticket creation' },
];

const QUOTES = [
  { text: 'SEV-2 page at 4:40 AM. Sent one Slack message from bed. The agent auto-loaded SOPs, pulled prod logs, cross-referenced a months-old incident, identified root cause. 30 min investigation done in under 10, from bed.', role: 'An on-call engineer' },
  { text: 'Since I started using this, AI fatigue decreased. One cause was re-explaining context every session. Now I often think "yes, you remember! That\'s what we were working on before!" Compared to plain chat where interpretations varied each time.', role: 'A platform engineer' },
  { text: 'I did a showcase in our team all-hands. Definitely blew some minds and got a ton of interest immediately after. I believe this is the future of AI-assisted engineering.', role: 'A database infrastructure engineer' },
  { text: 'Shared my full architecture diagram as primary dev environment. Created onboarding docs for the team. Uses multiple MCP servers and credential profiles seamlessly.', role: 'A backend engineer' },
];

export function ImpactSection() {
  return (
    <>
      <FadeUp>
        <section className="mb-20">
          <div className="flex items-center gap-3 mb-8">
            <h2 className="text-2xl font-bold">Community Impact</h2>
          </div>

          {/* Time Savings */}
          <div className="relative mb-12">
            <div className="absolute -left-3 top-0 bottom-0 w-1 bg-gradient-to-b from-cyan-500 via-cyan-500/50 to-transparent rounded-full" />
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2 pl-4"><Zap size={18} className="text-cyan-400" /> Reported Time Savings</h3>
            <div className="bg-[#111827] border border-cyan-500/12 rounded-xl overflow-hidden ml-4">
              <table className="w-full text-sm">
                <thead><tr className="border-b border-cyan-500/15 text-left bg-cyan-500/5">
                  <th className="px-4 py-3 text-slate-300 font-medium">Task</th>
                  <th className="px-4 py-3 text-slate-300 font-medium">Before</th>
                  <th className="px-4 py-3 text-slate-300 font-medium">After</th>
                  <th className="px-4 py-3 text-slate-300 font-medium">Gain</th>
                </tr></thead>
                <tbody>
                  {TIME_SAVINGS.map(t => (
                    <tr key={t.task} className="border-b border-indigo-500/6 hover:bg-white/[0.02] transition-colors">
                      <td className="px-4 py-3 font-medium">{t.task}</td>
                      <td className="px-4 py-3 text-rose-400 line-through opacity-70">{t.before}</td>
                      <td className="px-4 py-3 text-green-400 font-semibold">{t.after}</td>
                      <td className="px-4 py-3"><span className="px-2 py-0.5 rounded-md text-xs font-bold bg-amber-500/12 text-amber-400">{t.ratio}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Oncall Automation */}
          <div className="relative mb-12">
            <div className="absolute -left-3 top-0 bottom-0 w-1 bg-gradient-to-b from-rose-500 via-rose-500/50 to-transparent rounded-full" />
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2 pl-4"><Shield size={18} className="text-rose-400" /> Oncall & Ops Automation</h3>
            <div className="grid md:grid-cols-2 gap-3 ml-4">
              {ONCALL_CASES.map(c => (
                <div key={c.team} className="bg-[#111827] border border-rose-500/12 rounded-xl p-5 hover:border-rose-500/30 transition-colors">
                  <div className="flex justify-between items-start mb-2">
                    <span className="text-sm font-semibold text-white">{c.team}</span>
                    <span className="px-2 py-0.5 rounded-md text-xs font-bold bg-rose-500/15 text-rose-400 shrink-0 ml-2">{c.impact}</span>
                  </div>
                  <p className="text-xs text-slate-200 leading-relaxed">{c.desc}</p>
                </div>
              ))}
            </div>
          </div>

          {/* User Quotes */}
          <div className="relative mb-12">
            <div className="absolute -left-3 top-0 bottom-0 w-1 bg-gradient-to-b from-purple-500 via-purple-500/50 to-transparent rounded-full" />
            <h3 className="text-lg font-semibold mb-4 flex items-center gap-2 pl-4"><MessageSquare size={18} className="text-purple-400" /> What Users Say</h3>
            <div className="grid md:grid-cols-2 gap-3 ml-4">
              {QUOTES.map((q, i) => (
                <div key={i} className="bg-[#111827] border border-purple-500/12 rounded-xl p-5 relative overflow-hidden hover:border-purple-500/30 transition-colors">
                  <div className="absolute top-3 left-4 text-4xl text-purple-500/20 font-serif leading-none">"</div>
                  <p className="text-sm text-slate-300 italic pl-6 mb-3">{q.text}</p>
                  <div className="text-xs text-purple-400 font-medium pl-6">— {q.role}</div>
                </div>
              ))}
            </div>
          </div>
        </section>
      </FadeUp>
    </>
  );
}
