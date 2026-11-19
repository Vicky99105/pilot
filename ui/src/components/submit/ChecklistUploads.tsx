import { api, type RequestType } from '../../lib/api'
import { docLabel, rupees } from '../../lib/format'
import { useAsync } from '../../hooks/useAsync'
import { ErrorList, Loading } from '../ui/primitives'
import { FilePicker } from './FilePicker'
import type { Channel, UploadValue } from './uploads'

/** Typed upload slots from the request's checklist on the server, or one merged PDF. */
export function ChecklistUploads({
  requestType,
  channel,
  value,
  onChange,
}: {
  requestType: RequestType
  channel: Channel
  value: UploadValue
  onChange: (value: UploadValue) => void
}) {
  const [state] = useAsync(() => api.checklist(requestType), [requestType])

  return (
    <div>
      <div role="radiogroup" aria-label="How to upload" className="mb-3 flex flex-wrap gap-2">
        {(['slots', 'merged'] as const).map((m) => (
          <label key={m} className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name={`mode-${requestType}`}
              checked={value.mode === m}
              onChange={() => onChange({ ...value, mode: m })}
            />
            {m === 'slots' ? 'One upload per document' : 'All documents in one PDF'}
          </label>
        ))}
      </div>
      <p className="mb-3 text-xs text-muted">
        PDF, JPG, PNG, TIFF or HEIC, up to {channel === 'member' ? 10 : 15} MB per file. Missing documents do not stop
        the request; we will ask for them.
      </p>
      {value.mode === 'merged' ? (
        <FilePicker
          label="Merged PDF"
          hint="Every document in one PDF, in any order"
          channel={channel}
          pdfOnly
          multiple={false}
          files={value.merged}
          onChange={(merged) => onChange({ ...value, merged })}
        />
      ) : (
        <>
          {state.kind === 'loading' && <Loading label="Loading the checklist" />}
          {state.kind === 'error' && <ErrorList error={state.error} />}
          {state.kind === 'loaded' && (
            <div className="grid gap-2 sm:grid-cols-2">
              {state.data.map((item) => {
                const from = item.mandatory_from_amount ? `Needed from ${rupees(item.mandatory_from_amount)}` : null
                const hint = [item.notes, from].filter(Boolean).join('. ') || undefined
                return (
                  <FilePicker
                    key={item.doc_type}
                    label={docLabel(item.doc_type)}
                    hint={hint}
                    required={item.required}
                    channel={channel}
                    files={value.slots[item.doc_type] ?? []}
                    onChange={(files) => onChange({ ...value, slots: { ...value.slots, [item.doc_type]: files } })}
                  />
                )
              })}
            </div>
          )}
        </>
      )}
    </div>
  )
}
