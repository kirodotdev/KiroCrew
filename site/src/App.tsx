import './App.css';
import { ScrollProgress, Particles, Nav, Hero, TerminalDemo, Stats, SocialProof, ProblemSolution } from './SectionsTop';
import { Features, InAction, HowItWorks, Architecture, ThemesSection, FaqSection, Cta, Footer } from './SectionsBottom';
import { CaseStudies } from './CaseStudies';
import { ThemeProvider } from './ThemeContext';

function Landing() {
  return (
    <ThemeProvider>
      <div className="min-h-screen font-space overflow-x-hidden bg-white text-slate-800 dark:bg-[#06080f] dark:text-slate-200 transition-colors">
        <ScrollProgress />
        <div className="fixed inset-0 z-0 bg-grid" />
        <div className="fixed z-0 w-[700px] h-[700px] rounded-full blur-[140px] opacity-12 pointer-events-none -top-[300px] -left-[200px] bg-amber-400 dark:bg-amber-500 animate-float" />
        <div className="fixed z-0 w-[700px] h-[700px] rounded-full blur-[140px] opacity-12 pointer-events-none -bottom-[300px] -right-[200px] bg-orange-300 dark:bg-orange-500 animate-float-reverse" />
        <div className="fixed z-0 w-[700px] h-[700px] rounded-full blur-[140px] opacity-[0.06] pointer-events-none top-[40%] left-[50%] bg-rose-500 animate-float-slow" />
        <Particles />
        <div className="relative z-[1]">
          <Nav />
          <Hero />
          <div className="relative z-10 bg-white dark:bg-[#06080f] transition-colors">
            <Stats />
            <SocialProof />
            <TerminalDemo />
            <ProblemSolution />
            <Features />
            <InAction />
            <CaseStudies />
            <HowItWorks />
            <Architecture />
            <ThemesSection />
            <FaqSection />
            <Cta />
            <Footer />
          </div>
        </div>
      </div>
    </ThemeProvider>
  );
}

function App() {
  return <Landing />;
}

export default App;
