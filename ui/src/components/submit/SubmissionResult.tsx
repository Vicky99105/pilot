import type { Submission } from '../../lib/api'
import { date, docLabel } from '../../lib/format'
import { href } from '../../lib/router'
import { Button, Card, Notice } from '../ui/primitives'

/** What the server said about a submission. Warnings inform; they never block. */
export function SubmissionResult({ submission, onAnother }: { submission: Submission; onAnother?: () => void }) {
  const fileWarnings = submission.files.map((f) => f.quality_warning).filter((w): w is string => !!w)
  return (
    <Card title="Received">
      <div className="space-y-3 text-sm">
        <Notice tone="ok">
          We have your documents ({submission.files.length} file{submission.files.length === 1 ? '' : 's'}).
          {submission.deadline && <> We aim to decide by {date(submission.deadline)}.</>}
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
        {[...submission.warnings, ...fileWarnings].map((w) => (
          <Notice key={w} tone="warn">
            {w}
          </Notice>
        ))}
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
