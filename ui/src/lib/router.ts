import { useSyncExternalStore } from 'react'

// A small hash router. Hash URLs keep deep links working behind the backend's static
// file mount, which has no fallback to index.html.

function subscribe(onChange: () => void): () => void {
  window.addEventListener('hashchange', onChange)
  return () => window.removeEventListener('hashchange', onChange)
}

function currentPath(): string {
  return window.location.hash.replace(/^#/, '') || '/'
}

export function usePath(): string {
  return useSyncExternalStore(subscribe, currentPath, () => '/')
}

export function navigate(path: string): void {
  window.location.hash = path
}

export function href(path: string): string {
  return `#${path}`
}

/** Matches "/claims/:id" against a path; returns the params or null. */
export function match(pattern: string, path: string): Record<string, string> | null {
  const want = pattern.split('/').filter(Boolean)
  const got = path.split('?')[0].split('/').filter(Boolean)
  if (want.length !== got.length) return null
  const params: Record<string, string> = {}
  for (let i = 0; i < want.length; i++) {
    if (want[i].startsWith(':')) params[want[i].slice(1)] = decodeURIComponent(got[i])
    else if (want[i] !== got[i]) return null
  }
  return params
}
