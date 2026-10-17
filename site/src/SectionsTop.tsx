import { useState, useEffect, useRef } from 'react';
import { motion, useScroll, useTransform, useInView, AnimatePresence } from 'framer-motion';
import { FadeUp, Counter, useScrollProgress } from './animations';
import { TERMINAL_LINES } from './data';
import { X, Check, Sun, Moon } from 'lucide-react';
import { useTheme } from './ThemeContext';
import { AppPreview } from './AppPreview';

export function ScrollProgress() {
  const scaleX = useScrollProgress();
  return <motion.div className="fixed top-0 left-0 right-0 h-[2px] bg-amber-500 origin-left z-[200]" style={{ scaleX }} />;
}

export function Particles() {
  return (
    <div className="fixed inset-0 z-0 pointer-events-none overflow-hidden">
      {Array.from({ length: 25 }, (_, i) => (
        <div key={i} className="absolute bottom-0 w-[2px] h-[2px] rounded-full bg-amber-400 dark:bg-amber-400 opacity-40 dark:opacity-100 animate-rise"
          style={{ left: `${Math.random() * 100}%`, animationDelay: `${Math.random() * 20}s`, animationDuration: `${15 + Math.random() * 20}s` }} />
      ))}
    </div>
  );
}

export function Nav() {
  const [scrolled, setScrolled] = useState(false);
  const { theme, toggle } = useTheme();
  useEffect(() => {
    const fn = () => setScrolled(window.scrollY > 60);
    window.addEventListener('scroll', fn, { passive: true });
    return () => window.removeEventListener('scroll', fn);
  }, []);
  return (
    <motion.nav initial={{ y: -20, opacity: 0 }} animate={{ y: 0, opacity: 1 }} transition={{ duration: 0.5 }}
      className={`fixed top-0 left-0 right-0 z-[100] flex items-center justify-between px-6 md:px-10 py-4 max-w-[1200px] mx-auto transition-all duration-300 ${scrolled ? 'bg-white/85 dark:bg-[#06080f]/85 backdrop-blur-xl border-b border-amber-500/10' : ''}`}>
      <a href={import.meta.env.BASE_URL} className="flex items-center gap-2 no-underline">
        <img src={`${import.meta.env.BASE_URL}kirocrew-logo.png`} alt="Kiro Crew" className="w-8 h-8 rounded-lg" />
        <span className="text-xl font-bold text-slate-900 dark:text-white font-space tracking-tight">
          <span className="text-amber-500">Kiro</span> Crew
        </span>
      </a>
      <div className="flex gap-1 items-center">
        {[['#features', 'Features'], ['#in-action', 'Demo'], ['#how-it-works', 'Setup']].map(([href, label]) => (
          <a key={href} href={href} className="hidden md:block px-4 py-2 rounded-lg text-sm font-medium text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white hover:bg-black/5 dark:hover:bg-white/5 no-underline transition-all">{label}</a>
        ))}
        <a href="https://github.com/kirodotdev/KiroCrew" target="_blank" rel="noopener noreferrer" className="hidden md:block px-4 py-2 rounded-lg text-sm font-medium text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white hover:bg-black/5 dark:hover:bg-white/5 no-underline transition-all">Source</a>
        <button onClick={toggle} className="p-2 rounded-lg text-slate-600 dark:text-slate-400 hover:bg-black/5 dark:hover:bg-white/5 transition-all" aria-label="Toggle theme">
          {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
        </button>
        <a href="#how-it-works" className="px-4 py-2 rounded-lg text-sm font-medium bg-amber-500 text-white hover:bg-amber-400 no-underline transition-all">Get Started</a>
      </div>
    </motion.nav>
  );
}

export function Hero() {
  const ref = useRef(null);
  const { scrollYProgress } = useScroll({ target: ref, offset: ['start start', 'end start'] });
  const y = useTransform(scrollYProgress, [0, 1], [0, 200]);
  const opacity = useTransform(scrollYProgress, [0, 0.6], [1, 0]);
  return (
    <>
    <motion.section ref={ref} style={{ y, opacity }} className="text-center pt-36 md:pt-40 pb-16 px-6 max-w-[900px] mx-auto">
      <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.2 }} className="flex gap-3 justify-center mb-8">
        <span className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full text-xs font-medium bg-green-500/8 text-green-600 dark:text-green-400 border border-green-500/20">
          <span className="w-1.5 h-1.5 rounded-full bg-green-500 dark:bg-green-400 animate-pulse-dot" /> Gateway Online
        </span>
        <span className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full text-xs font-medium bg-amber-500/8 text-amber-600 dark:text-amber-400 border border-amber-500/20">Open Source</span>
      </motion.div>
      <motion.h1 initial={{ opacity: 0, y: 30 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.35, duration: 0.7 }}
        className="text-5xl md:text-7xl lg:text-8xl font-bold leading-[1.05] mb-6 animate-shimmer font-space">
        Your AI copilot<br />that never forgets
      </motion.h1>
      <motion.p initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.5 }}
        className="text-lg md:text-xl text-slate-600 dark:text-slate-400 max-w-[640px] mx-auto mb-10 leading-relaxed font-space">
        Persistent, self-learning AI agent that runs on your machine. Remembers across sessions, learns from corrections, runs 24/7 — accessible through Slack and a web dashboard.
      </motion.p>
      <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.65 }} className="flex gap-3 justify-center flex-wrap">
        <a href="#how-it-works" className="inline-flex items-center gap-2 px-7 py-3.5 rounded-xl text-[15px] font-semibold bg-gradient-to-r from-amber-500 to-orange-600 text-white shadow-[0_0_24px_rgba(245,158,11,0.35),0_4px_16px_rgba(0,0,0,0.4)] hover:-translate-y-0.5 hover:shadow-[0_0_40px_rgba(245,158,11,0.4)] transition-all no-underline font-space">Install in 3 Minutes</a>
        <a href="https://github.com/kirodotdev/KiroCrew" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-2 px-7 py-3.5 rounded-xl text-[15px] font-semibold bg-slate-100 dark:bg-white/5 text-slate-800 dark:text-white border border-amber-500/15 hover:bg-slate-200 dark:hover:bg-white/8 hover:border-amber-500/30 hover:-translate-y-0.5 transition-all no-underline font-space">View Source</a>
      </motion.div>
    </motion.section>
    <AppPreview />
    </>
  );
}

export function TerminalDemo() {
  const ref = useRef(null);
  const inView = useInView(ref, { once: true, margin: '-100px' });
  const [lines, setLines] = useState(0);
  useEffect(() => {
    if (!inView) return;
    let i = 0;
    const iv = setInterval(() => { i++; setLines(i); if (i >= TERMINAL_LINES.length) clearInterval(iv); }, 400);
    return () => clearInterval(iv);
  }, [inView]);

  return (
    <FadeUp className="max-w-[720px] mx-auto mb-20 px-6">
      <div ref={ref} className="bg-slate-900 dark:bg-[#111827] border border-amber-500/12 rounded-2xl overflow-hidden shadow-lg dark:shadow-[0_24px_80px_rgba(0,0,0,0.5),0_0_60px_rgba(245,158,11,0.08)]">
        <div className="flex items-center gap-2 px-4 py-3 bg-black/20 dark:bg-black/40 border-b border-amber-500/12">
          <div className="w-3 h-3 rounded-full bg-red-500" /><div className="w-3 h-3 rounded-full bg-amber-500" /><div className="w-3 h-3 rounded-full bg-green-500" />
          <span className="flex-1 text-center text-xs text-slate-400 font-mono">kirocrew — gateway</span>
        </div>
        <div className="p-6 font-mono text-[13.5px] leading-[1.8] min-h-[220px]">
          <AnimatePresence>
            {TERMINAL_LINES.slice(0, lines).map((l, i) => (
              <motion.div key={i} initial={{ opacity: 0, x: -10 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: 0.3 }}>
                {l.prompt && <><span className="text-green-400">$ </span><span className="text-slate-100">{l.text}</span></>}
                {!l.prompt && !l.comment && <><span className="text-amber-400">{l.text}</span>{l.hl && <span className="text-amber-400">{l.hl}</span>}</>}
                {l.comment && <span className="text-slate-500">{l.comment}</span>}
              </motion.div>
            ))}
          </AnimatePresence>
          {lines > 0 && lines < TERMINAL_LINES.length && <span className="text-green-400 animate-blink">|</span>}
        </div>
      </div>
    </FadeUp>
  );
}

export function Stats() {
  const ref = useRef(null);
  const inView = useInView(ref, { once: true });
  const items = [
    { end: 100, suffix: '+', label: 'MCP Tools' },
    { end: 22, suffix: '', label: 'Themes' },
    { end: 48, suffix: '', label: 'Backend Modules' },
    { end: 17, suffix: '+', label: 'Agent Types' },
  ];
  return (
    <div ref={ref} className="flex justify-center gap-14 flex-wrap px-6 pt-8 pb-6">
      {items.map((s, i) => (
        <motion.div key={s.label} initial={{ opacity: 0, y: 20 }} animate={inView ? { opacity: 1, y: 0 } : {}} transition={{ delay: i * 0.1 }} className="text-center">
          <div className="text-4xl md:text-5xl font-bold gradient-text"><Counter end={s.end} suffix={s.suffix} visible={inView} /></div>
          <div className="text-xs text-slate-500 mt-1 uppercase tracking-[2px]">{s.label}</div>
        </motion.div>
      ))}
    </div>
  );
}

export function SocialProof() {
  return (
    <FadeUp className="text-center px-6 pb-12 text-sm text-slate-500">
      <span>Built for engineers who live in the terminal and Slack</span>
    </FadeUp>
  );
}

export function ProblemSolution() {
  const BEFORE = [
    'Re-explain context every new chat session',
    'Manually check pipelines, oncall, tickets',
    'Copy-paste between 5+ tools',
    'Forget the fix you found last month',
    'One task at a time, waiting for each step',
  ];
  const AFTER = [
    'Remembers everything across sessions',
    'Cron jobs brief you every morning',
    '100+ tools wired via MCP',
    'Lessons persist forever, never repeats',
    'Parallel subagents fan out work',
  ];

  return (
    <div className="max-w-[900px] mx-auto px-6 pb-20">
      <FadeUp><h2 className="text-center text-4xl md:text-5xl font-bold mb-16 font-space">A better way to work</h2></FadeUp>
      <div className="space-y-3">
        {BEFORE.map((b, i) => (
          <FadeUp key={i} delay={i * 0.08}>
            <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-4 md:gap-6">
              <motion.div initial={{ opacity: 0, x: -30 }} whileInView={{ opacity: 1, x: 0 }} viewport={{ once: true }} transition={{ delay: i * 0.08, duration: 0.5 }}
                className="flex items-center gap-3 justify-end text-right">
                <span className="text-sm text-slate-500 leading-relaxed line-through decoration-rose-500/40">{b}</span>
                <span className="shrink-0 w-6 h-6 rounded-full bg-rose-500/10 border border-rose-500/20 flex items-center justify-center text-rose-400 text-xs font-bold"><X size={12} /></span>
              </motion.div>
              <motion.div initial={{ scaleY: 0 }} whileInView={{ scaleY: 1 }} viewport={{ once: true }} transition={{ delay: i * 0.08 + 0.2, duration: 0.4 }}
                className="w-px h-10 bg-gradient-to-b from-rose-500/40 via-slate-300 dark:via-slate-600 to-green-500/40 origin-top" />
              <motion.div initial={{ opacity: 0, x: 30 }} whileInView={{ opacity: 1, x: 0 }} viewport={{ once: true }} transition={{ delay: i * 0.08 + 0.15, duration: 0.5 }}
                className="flex items-center gap-3">
                <span className="shrink-0 w-6 h-6 rounded-full bg-green-500/10 border border-green-500/20 flex items-center justify-center text-green-400 text-xs font-bold"><Check size={12} /></span>
                <span className="text-sm text-slate-800 dark:text-white leading-relaxed font-medium">{AFTER[i]}</span>
              </motion.div>
            </div>
          </FadeUp>
        ))}
      </div>
    </div>
  );
}
