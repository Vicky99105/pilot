import type { Submission } from '../../lib/api'
import { date, dateTime, docLabel, humanise } from '../../lib/format'
import { href } from '../../lib/router'
import { Button, Card, Notice } from '../ui/primitives'

/** What the server said about a submission. Warnings inform; they never block. */
export function SubmissionResult({ submission, onAnother }: { submission: Submission; onAnother?: () => void }) {
  const fileWarnings = submission.files.filter((f) => f.quality_warning)
  return (
    <Card title={`${humanise(submission.request_type)} received`}>
      <div className="space-y-3 text-sm">
        <Notice tone="ok">
          We have your documents ({submission.files.length} file{submission.files.length === 1 ? '' : 's'}).
          {submission.clock_deadline ? (
            <> We aim to respond by {dateTime(submission.clock_deadline)}.</>
          ) : (
            submission.deadline && <> We aim to decide by {date(submission.deadline)}.</>
          )}
        </Notice>
        {submission.duplicate_of && (
          <Notice tone="warn">
            These documents match an earlier submission, so they were added to that claim instead of starting a new one.
          </Notice>
        )}
        {submission.missing_documents.length > 0 && (
          <Notice tone="warn">
            <p className="font-medium">Still needed. The claim goes ahead, and we will ask for these:</p>
            <ul className="mt-1 list-disc pl-5">
              {submission.missing_documents.map((d) => (
                <li key={d}>{docLabel(d)}</li>
              ))}
            </ul>
          </Notice>
        )}
        {submission.warnings.map((w) => (
          <Notice key={w} tone="warn">
            {w}
          </Notice>
        ))}
        {fileWarnings.length > 0 && (
          <Notice tone="warn">
            <p className="font-medium">Some files may be hard to read. Send a clearer copy if you can:</p>
            <ul className="mt-1 list-disc pl-5">
              {fileWarnings.map((f) => (
                <li key={f.file_id}>
                  {f.slot ? docLabel(f.slot) : 'Merged PDF'}
                  {f.filename && <span className="text-muted"> ({f.filename})</span>}: {f.quality_warning}
                </li>
              ))}
            </ul>
          </Notice>
        )}
        {!submission.workflow_id && (
          <Notice tone="warn">Processing has not started yet. It will start automatically.</Notice>
        )}
        <div className="flex flex-wrap gap-2">
          <a
            href={href(`/claims/${submission.claim_id}`)}
            className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-accent-ink"
          >
            Track this claim
          </a>
          {onAnother && <Button onClick={onAnother}>Submit another</Button>}
        </div>
      </div>
    </Card>
  )
}
