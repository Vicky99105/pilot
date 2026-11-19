import { useEffect, useRef, useState } from 'react'

export type AsyncState<T> =
  | { kind: 'loading' }
  | { kind: 'loaded'; data: T }
  | { kind: 'error'; error: unknown }

/** Runs load() when a key in deps changes; reload() runs it again. Stale responses are dropped. */
export function useAsync<T>(
  load: () => Promise<T>,
  deps: readonly unknown[],
): [AsyncState<T>, () => void] {
  const [state, setState] = useState<AsyncState<T>>({ kind: 'loading' })
  const [tick, setTick] = useState(0)
  const loadRef = useRef(load)
  useEffect(() => {
    loadRef.current = load
  })
  const key = JSON.stringify(deps)
  const shownKey = useRef(key)

  useEffect(() => {
    const run = { live: true }
    if (shownKey.current !== key) {
      shownKey.current = key
      setState({ kind: 'loading' }) // never show the previous key's data as this key's
    }
    loadRef
      .current()
      .then((data) => run.live && setState({ kind: 'loaded', data }))
      .catch((error: unknown) => run.live && setState({ kind: 'error', error }))
    return () => {
      run.live = false
    }
  }, [key, tick])

  return [state, () => setTick((t) => t + 1)]
}
