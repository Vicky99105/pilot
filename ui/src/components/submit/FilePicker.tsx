import { useId, useState } from 'react'
import { fileSize } from '../../lib/format'
import { ACCEPT_ATTR, fileProblem, type Channel } from './uploads'

/** Picks files for one slot. Files that fail the client checks are refused with the reason. */
export function FilePicker({
  label,
  hint,
  required,
  files,
  onChange,
  channel,
  pdfOnly = false,
  multiple = true,
}: {
  label: string
  hint?: string
  required?: boolean
  files: File[]
  onChange: (files: File[]) => void
  channel: Channel
  pdfOnly?: boolean
  multiple?: boolean
}) {
  const id = useId()
  const [problems, setProblems] = useState<string[]>([])

  const add = (list: FileList | null) => {
    const picked = Array.from(list ?? [])
    const bad = picked.map((f) => fileProblem(f, channel, pdfOnly)).filter((p): p is string => !!p)
    const good = picked.filter((f) => !fileProblem(f, channel, pdfOnly))
    setProblems(bad)
    onChange(multiple ? [...files, ...good] : good.slice(0, 1))
  }

  return (
    <div className="rounded-md border border-line p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-sm font-medium">
            {label}
            {required && <span className="ml-1 text-xs font-normal text-muted">(required)</span>}
          </p>
          {hint && <p className="text-xs text-muted">{hint}</p>}
        </div>
        <label
          htmlFor={id}
          className="cursor-pointer rounded-md border border-line bg-surface px-2.5 py-1 text-xs font-medium hover:bg-surface-2 has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-accent"
        >
          {files.length ? 'Add file' : 'Choose file'}
          <input
            id={id}
            type="file"
            className="sr-only"
            accept={pdfOnly ? '.pdf' : ACCEPT_ATTR}
            multiple={multiple}
            onChange={(e) => {
              add(e.target.files)
              e.target.value = ''
            }}
          />
        </label>
      </div>
      {files.length > 0 && (
        <ul className="mt-2 space-y-1">
          {files.map((f, i) => (
            <li key={`${f.name}-${i}`} className="flex items-center justify-between gap-2 text-xs">
              <span className="truncate">
                {f.name} <span className="num text-muted">({fileSize(f.size)})</span>
              </span>
              <button
                type="button"
                className="text-danger hover:underline"
                onClick={() => onChange(files.filter((_, j) => j !== i))}
                aria-label={`Remove ${f.name}`}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
      {problems.map((p) => (
        <p key={p} className="mt-1 text-xs text-danger" role="alert">
          {p}
        </p>
      ))}
    </div>
  )
}
