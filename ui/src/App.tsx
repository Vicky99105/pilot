import { useEffect, useState } from 'react'

type ComponentStatus = { status: 'ok' | 'down'; detail: string | null }
type Health = { status: 'ok' | 'degraded'; components: Record<string, ComponentStatus> }

type LoadState =
  | { kind: 'loading' }
  | { kind: 'loaded'; health: Health }
  | { kind: 'error'; message: string }

async function fetchHealth(): Promise<Health> {
  // /health answers 503 with the same JSON body when a component is down.
  const response = await fetch('/health')
  return (await response.json()) as Health
}

function StatusBadge({ ok, label }: { ok: boolean; label: string }) {
  const color = ok ? 'bg-emerald-100 text-emerald-800' : 'bg-red-100 text-red-800'
  return <span className={`rounded-full px-3 py-1 text-sm font-medium ${color}`}>{label}</span>
}

export default function App() {
  const [state, setState] = useState<LoadState>({ kind: 'loading' })

  const refresh = () => {
    fetchHealth()
      .then((health) => setState({ kind: 'loaded', health }))
      .catch((error: unknown) => setState({ kind: 'error', message: String(error) }))
  }

  useEffect(refresh, [])

  return (
    <main className="min-h-screen bg-slate-50 px-4 py-12 text-slate-900">
      <div className="mx-auto max-w-xl">
        <h1 className="text-2xl font-semibold">ConsintIQ Claims Pilot</h1>
        <p className="mt-1 text-sm text-slate-500">System health</p>

        <section className="mt-8 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          {state.kind === 'loading' && <p className="text-slate-500">Checking…</p>}
          {state.kind === 'error' && (
            <p className="text-red-700">Could not reach the API: {state.message}</p>
          )}
          {state.kind === 'loaded' && (
            <>
              <div className="flex items-center justify-between">
                <span className="font-medium">Overall</span>
                <StatusBadge ok={state.health.status === 'ok'} label={state.health.status} />
              </div>
              <ul className="mt-4 divide-y divide-slate-100">
                {Object.entries(state.health.components).map(([name, component]) => (
                  <li key={name} className="py-3">
                    <div className="flex items-center justify-between">
                      <span className="capitalize">{name}</span>
                      <StatusBadge ok={component.status === 'ok'} label={component.status} />
                    </div>
                    {component.detail && (
                      <p className="mt-1 break-words font-mono text-xs text-slate-500">
                        {component.detail}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            </>
          )}
          <button
            type="button"
            onClick={() => {
              setState({ kind: 'loading' })
              refresh()
            }}
            className="mt-6 rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
          >
            Refresh
          </button>
        </section>
      </div>
    </main>
  )
}
