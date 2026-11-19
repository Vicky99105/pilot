import { useEffect, useState } from 'react'
import { fetchFileBlob } from '../lib/api'

/** An object URL for a permission-checked file, revoked when the key changes. */
export function useFileUrl(key: string | null): { url: string | null; error: string | null } {
  const [state, setState] = useState<{ key: string | null; url: string | null; error: string | null }>(
    { key: null, url: null, error: null },
  )

  useEffect(() => {
    if (!key) return
    const run = { live: true }
    let created: string | null = null
    fetchFileBlob(key)
      .then((blob) => {
        created = URL.createObjectURL(blob)
        if (run.live) setState({ key, url: created, error: null })
      })
      .catch((e: unknown) => run.live && setState({ key, url: null, error: String(e) }))
    return () => {
      run.live = false
      if (created) URL.revokeObjectURL(created)
    }
  }, [key])

  return state.key === key ? { url: state.url, error: state.error } : { url: null, error: null }
}

/** Downloads a permission-checked file under a filename. */
export async function downloadFile(key: string, filename: string): Promise<void> {
  const url = URL.createObjectURL(await fetchFileBlob(key))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
