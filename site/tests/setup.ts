import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';
import '@testing-library/jest-dom';

// Polyfills for jsdom
globalThis.IntersectionObserver = class IntersectionObserver {
  constructor(private cb: IntersectionObserverCallback) {}
  observe() { this.cb([{ isIntersecting: true } as IntersectionObserverEntry], this as any); }
  unobserve() {}
  disconnect() {}
} as any;

globalThis.matchMedia = globalThis.matchMedia || ((q: string) => ({ matches: false, media: q, addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false }));

afterEach(() => {
  cleanup();
});