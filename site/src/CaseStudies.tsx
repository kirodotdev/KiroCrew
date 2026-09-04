import { useRef, useLayoutEffect } from 'react';
import { FadeUp } from './animations';
import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';

const CASES = [
  {
    tag: 'Automation', org: 'Retail', title: 'Mission Control', author: 'Community',
    desc: 'Multi-agent system automating operational work. Domain skills, specialist agents, cron-based proactive monitoring.',
    metrics: [{ before: '2 hours', after: '5 min', label: 'Report commentary' }, { before: '45 min', after: '2 min', label: 'Batch evaluation' }],
    features: ['Subagents', 'Skills', 'Cron', 'MCP Tools'],
    detail: 'Marketplace-agnostic design — config file change to deploy anywhere.',
  },
  {
    tag: 'Oncall', org: 'Cross-org', title: 'Oncall Triage Assistant', author: 'Community',
    desc: 'Automated ticket triage, alarm correlation, pipeline health checks, and shift handoff generation. Runs as a cron job every morning.',
    metrics: [{ before: '30 min', after: '2 min', label: 'Morning triage' }, { before: 'Manual', after: 'Auto', label: 'Shift handoff' }],
    features: ['Cron', 'Heartbeat', 'Memory', 'Tickets'],
    detail: 'Correlates alarms with recent deployments. Generates structured handoff docs with open tickets, pending PRs, and active incidents.',
  },
  {
    tag: 'Development', org: 'Cross-org', title: 'Autonomous Code Tasks', author: 'Community',
    desc: 'Give it a spec, walk away. Git-isolated execution with independent code review by a separate reviewer agent.',
    metrics: [{ before: 'Hours', after: 'Unattended', label: 'Task execution' }, { before: 'Self-report', after: 'Git diff', label: 'Code review' }],
    features: ['Task Runner', 'Git Worktree', 'Checkpoint', 'Learn'],
    detail: 'Checkpoint resume on crash. 3 logic retries per step, 2 replans. Failed steps produce lessons for future tasks.',
  },
  {
    tag: 'Monitoring', org: 'Cross-org', title: 'Pipeline & Deployment Watch', author: 'Community',
    desc: 'Heartbeat-driven monitoring of pipelines, PRs, and deployments. Proactive alerts before humans notice issues.',
    metrics: [{ before: 'Check manually', after: '60s loop', label: 'Detection time' }, { before: '5+ dashboards', after: '1 Slack DM', label: 'Visibility' }],
    features: ['Heartbeat', 'Cron', 'Slack', 'Subagents'],
    detail: 'Self-healing loop survives gateway restarts. Monitors PRs for automated review comments, fixes them, and pushes new revisions automatically.',
  },
  {
    tag: 'Knowledge', org: 'Cross-org', title: 'Self-Learning Memory', author: 'Built-in',
    desc: 'Every correction becomes a permanent lesson. Project context tracked automatically. Preferences injected into every session.',
    metrics: [{ before: 'Repeat mistakes', after: 'Never again', label: 'Corrections' }, { before: 'Lost context', after: '90-day recall', label: 'Memory' }],
    features: ['Lessons', 'Vector DB', 'Preferences', 'Projects'],
    detail: 'Episodic memory with natural decay: 3 days full detail, 30 days summary, 90 days markers. Optional vector search via local embeddings.',
  },
];

const TAG_COLORS: Record<string, string> = {
  Automation: 'bg-amber-500/12 text-amber-400',
  Oncall: 'bg-rose-500/12 text-rose-400',
  Development: 'bg-amber-500/12 text-amber-400',
  Monitoring: 'bg-green-500/12 text-green-400',
  Knowledge: 'bg-amber-300/12 text-amber-300',
};

export function CaseStudies() {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const wrapper = wrapperRef.current;
    const track = trackRef.current;
    if (!wrapper || !track) return;

    const ctx = gsap.context(() => {
      gsap.registerPlugin(ScrollTrigger);

      gsap.to(track, {
        x: () => -(track.scrollWidth - window.innerWidth),
        ease: 'none',
        scrollTrigger: {
          trigger: wrapper,
          pin: true,
          scrub: 0.5,
          start: 'center center',
          end: () => `+=${track.scrollWidth - window.innerWidth}`,
          invalidateOnRefresh: true,
        },
      });
    }, wrapperRef);

    return () => ctx.revert();
  }, []);

  return (
    <section id="case-studies" className="pt-24">
      <div ref={wrapperRef} className="overflow-hidden">
        <FadeUp className="px-6 mb-12">
          <h2 className="text-center text-4xl md:text-5xl font-bold font-space">Built for real workflows</h2>
          <p className="text-center text-slate-500 dark:text-slate-400 text-lg mt-3 font-space">Scroll to explore use cases from the community</p>
        </FadeUp>
        <div ref={trackRef} className="flex gap-6 pl-[max(1.5rem,calc((100vw-1200px)/2+1.5rem))] pr-[calc(50vw-210px)] will-change-transform">
          {CASES.map((c) => (
            <div key={c.title}
              className="case-card shrink-0 w-[380px] md:w-[420px] bg-slate-100 dark:bg-[#111827] border border-amber-500/12 rounded-2xl p-8 relative overflow-hidden group hover:-translate-y-1.5 hover:border-amber-500/40 transition-all">
              <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-amber-500 via-orange-500 to-amber-300 opacity-0 group-hover:opacity-100 transition-opacity" />

              <div className="flex items-center gap-2 mb-4">
                <span className={`px-2.5 py-0.5 rounded-md text-[11px] font-semibold uppercase tracking-wider ${TAG_COLORS[c.tag]}`}>{c.tag}</span>
                <span className="text-[11px] text-slate-500">{c.org}</span>
              </div>

              <h3 className="text-lg font-bold mb-2 font-space">{c.title}</h3>
              <p className="text-slate-500 dark:text-slate-400 text-sm leading-relaxed mb-5">{c.desc}</p>

              <div className="flex flex-col gap-2 mb-5">
                {c.metrics.map(m => (
                  <div key={m.label} className="flex items-center gap-2 text-sm">
                    <span className="text-rose-400 line-through opacity-60 w-24 text-right shrink-0">{m.before}</span>
                    <span className="text-slate-600">&rarr;</span>
                    <span className="text-green-400 font-semibold">{m.after}</span>
                    <span className="text-slate-500 text-xs ml-1">{m.label}</span>
                  </div>
                ))}
              </div>

              <div className="flex flex-wrap gap-1.5 mb-4">
                {c.features.map(f => (
                  <span key={f} className="px-2 py-0.5 rounded-md text-[10px] font-medium bg-slate-200/60 dark:bg-white/5 text-slate-500 dark:text-slate-400 border border-slate-300/50 dark:border-white/5">{f}</span>
                ))}
              </div>

              <p className="text-xs text-slate-500 dark:text-slate-500 leading-relaxed">{c.detail}</p>
              <p className="text-[11px] text-slate-500 dark:text-slate-600 mt-3 italic">— {c.author}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
