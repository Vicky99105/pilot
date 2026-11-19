import { useEffect } from 'react'
import type { ClaimPage } from '../../lib/api'
import { docLabel, percent } from '../../lib/format'
import { useFileUrl } from '../../hooks/useFileUrl'
import { Button, Chip, Empty } from '../ui/primitives'

/** Document page images with a page switcher. The cited page is outlined. */
export function PageViewer({
  pages,
  index,
  onIndex,
  citedPageId,
}: {
  pages: ClaimPage[]
  index: number
  onIndex: (i: number) => void
  citedPageId: string | null
}) {
  const page = pages[index] ?? null
  const { url, error } = useFileUrl(page?.image_key ?? null)
  const cited = page !== null && page.page_id === citedPageId

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null
      if (target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return
      if (e.key === '[' && index > 0) onIndex(index - 1)
      if (e.key === ']' && index < pages.length - 1) onIndex(index + 1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [index, onIndex, pages.length])

  if (pages.length === 0) return <Empty>No document pages yet.</Empty>

  return (
    <div className="flex h-full flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1">
          <Button aria-label="Previous page" disabled={index === 0} onClick={() => onIndex(index - 1)}>
            Prev
          </Button>
          <label className="sr-only" htmlFor="page-select">
            Page
          </label>
          <select
            id="page-select"
            className="rounded-md border border-line bg-surface px-2 py-1 text-sm"
            value={index}
            onChange={(e) => onIndex(Number(e.target.value))}
          >
            {pages.map((p, i) => (
              <option key={p.page_id} value={i}>
                {p.seq}. {docLabel(p.doc_type)}
                {p.page_in_document ? ` p${p.page_in_document}` : ''}
              </option>
            ))}
          </select>
          <Button aria-label="Next page" disabled={index >= pages.length - 1} onClick={() => onIndex(index + 1)}>
            Next
          </Button>
        </div>
        {page && (
          <div className="flex items-center gap-1">
            {cited && <Chip tone="warn">Cited page</Chip>}
            <Chip tone={page.confidence < 0.8 ? 'warn' : 'neutral'}>label {percent(page.confidence)}</Chip>
          </div>
        )}
      </div>
      {page?.quality_warning && <p className="text-xs text-warn">{page.quality_warning}</p>}
      <div
        className={`min-h-[20rem] flex-1 overflow-auto rounded-md border-2 bg-surface-2 ${
          cited ? 'border-highlight' : 'border-line'
        }`}
      >
        {error && <p className="p-4 text-sm text-danger">{error}</p>}
        {!error && !url && <p className="p-4 text-sm text-muted">Loading page...</p>}
        {url && page && (
          <img
            src={url}
            alt={`Page ${page.seq}: ${docLabel(page.doc_type)}`}
            className="mx-auto block h-auto max-w-full"
          />
        )}
      </div>
      <p className="text-xs text-muted">Keys: [ and ] switch pages.</p>
    </div>
  )
}
