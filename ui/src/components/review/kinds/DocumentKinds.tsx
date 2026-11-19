import { useState } from 'react'
import { DOC_TYPES, type DocType } from '../../../lib/api'
import { docLabel, humanise } from '../../../lib/format'
import { Button, Chip, inputClass, Notice } from '../../ui/primitives'
import { Actions, CiteButton, Confidence, Section, type KindProps } from './common'
import type { FieldValue, FixFieldsPayload, FixLabelsPayload } from './payloads'

/** fix_labels: outcome {action: "confirm", labels: {page_id: doc_type}} with changed pages only. */
export function FixLabels({ payload, focusPage, submit, busy }: KindProps<FixLabelsPayload>) {
  const [labels, setLabels] = useState<Record<string, DocType>>({})
  const changed = Object.fromEntries(
    Object.entries(labels).filter(([id, t]) => payload.pages.find((p) => p.page_id === id)?.doc_type !== t),
  )

  return (
    <div className="space-y-4">
      {payload.mismatches.length > 0 && (
        <Section title="What does not fit the checklist">
          <ul className="space-y-1 text-sm">
            {payload.mismatches.map((m, i) => (
              <li key={i}>
                <Chip tone="warn">{humanise(m.kind)}</Chip> <span className="ml-1">{m.detail}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}
      <Section title="Page labels">
        <ul className="divide-y divide-line">
          {payload.pages.map((p) => (
            <li key={p.page_id} className="flex flex-wrap items-center gap-2 py-2">
              <button
                type="button"
                className="w-14 text-left text-sm text-accent hover:underline"
                onClick={() => focusPage(p.page_id)}
              >
                Page {p.seq}
              </button>
              <Confidence value={p.confidence} />
              <label className="sr-only" htmlFor={`label-${p.page_id}`}>
                Document type of page {p.seq}
              </label>
              <select
                id={`label-${p.page_id}`}
                className={`${inputClass} w-auto min-w-0 flex-1`}
                value={labels[p.page_id] ?? p.doc_type}
                onFocus={() => focusPage(p.page_id)}
                onChange={(e) => setLabels((l) => ({ ...l, [p.page_id]: e.target.value as DocType }))}
              >
                {DOC_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {docLabel(t)}
                  </option>
                ))}
              </select>
              {p.quality_warning && <span className="w-full text-xs text-warn">{p.quality_warning}</span>}
            </li>
          ))}
        </ul>
      </Section>
      <Actions>
        <Button variant="primary" disabled={busy} onClick={() => submit({ action: 'confirm', labels: changed })}>
          {Object.keys(changed).length ? `Save ${Object.keys(changed).length} label change(s)` : 'Labels are right'}
        </Button>
      </Actions>
    </div>
  )
}

function asText(value: FieldValue['value']): string {
  return value === null || value === undefined ? '' : String(value)
}

/** fix_fields: outcome {action: "confirm", fields: {dotted.path: value}} with changed fields only. */
export function FixFields({ payload, cite, submit, busy }: KindProps<FixFieldsPayload>) {
  const fields = payload.fields ?? {}
  const paths = Object.keys(fields)
  const [edits, setEdits] = useState<Record<string, string>>({})
  const failed = payload.failed_checks.filter((c) => !c.passed)

  const changed = Object.fromEntries(
    Object.entries(edits)
      .filter(([path, v]) => v !== asText(fields[path]?.value))
      .map(([path, v]) => {
        const original = fields[path]?.value
        if (typeof original === 'boolean') return [path, v === 'true']
        return [path, v.trim() === '' ? null : v.trim()]
      }),
  )

  return (
    <div className="space-y-4">
      {failed.length > 0 && (
        <Section title="Checks that failed">
          <ul className="space-y-1 text-sm">
            {failed.map((c) => (
              <li key={c.check_id}>
                <Chip tone="danger">{c.check_id}</Chip> <span className="ml-1">{c.detail}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}
      <Section title="Fields to check">
        {paths.length === 0 && (
          <Notice tone="warn">
            This task does not carry the field values. Flagged: {payload.low_confidence_fields.join(', ') || 'none'}.
          </Notice>
        )}
        <ul className="space-y-3">
          {paths.map((path) => {
            const f = fields[path]
            const id = `field-${path}`
            return (
              <li key={path}>
                <div className="mb-1 flex flex-wrap items-center gap-2">
                  <label htmlFor={id} className="font-mono text-xs">
                    {path}
                  </label>
                  <Confidence value={f.confidence} />
                  {f.handwritten && <Chip>handwritten</Chip>}
                  <CiteButton source={f.source} onCite={cite} />
                </div>
                {typeof f.value === 'boolean' ? (
                  <select
                    id={id}
                    className={inputClass}
                    value={edits[path] ?? asText(f.value)}
                    onFocus={() => f.source && cite(f.source)}
                    onChange={(e) => setEdits((x) => ({ ...x, [path]: e.target.value }))}
                  >
                    <option value="true">Yes</option>
                    <option value="false">No</option>
                  </select>
                ) : (
                  <input
                    id={id}
                    className={inputClass}
                    value={edits[path] ?? asText(f.value)}
                    placeholder="Not read"
                    onFocus={() => f.source && cite(f.source)}
                    onChange={(e) => setEdits((x) => ({ ...x, [path]: e.target.value }))}
                  />
                )}
              </li>
            )
          })}
        </ul>
      </Section>
      <Actions>
        <Button variant="primary" disabled={busy} onClick={() => submit({ action: 'confirm', fields: changed })}>
          {Object.keys(changed).length ? `Save ${Object.keys(changed).length} correction(s)` : 'Values are right'}
        </Button>
      </Actions>
    </div>
  )
}
