import { useCallback, useMemo, useState, type ComponentType } from 'react'
import { api, ApiError, type ClaimPages, type PageRef, type Task, type User } from '../../lib/api'
import { rupees } from '../../lib/format'
import { href, navigate } from '../../lib/router'
import { ROLE_LABELS, SYSTEM_LABELS } from '../../lib/users'
import { useAsync } from '../../hooks/useAsync'
import { useNow } from '../../hooks/useNow'
import { ClaimOverrides } from '../overrides/OverrideTable'
import { Button, Card, Chip, ErrorList, Loading, Notice } from '../ui/primitives'
import { PageViewer } from './PageViewer'
import { UrgencyChip } from './Queue'
import type { KindProps, Outcome } from './kinds/common'
import { MedicalFindings, Investigate } from './kinds/ClinicalKinds'
import { Approve, Crc, QueryNoReply, ReleasePayment } from './kinds/DecisionKinds'
import { FixFields, FixLabels } from './kinds/DocumentKinds'
import { ConfirmDeductions, ConfirmRegistration } from './kinds/ReviewKinds'

type AnyKind = ComponentType<KindProps<any>>

const KINDS: Record<string, AnyKind> = {
  fix_labels: FixLabels,
  fix_fields: FixFields,
  confirm_registration: ConfirmRegistration,
  confirm_deductions: ConfirmDeductions,
  medical_findings: MedicalFindings,
  investigate: Investigate,
  approve: Approve,
  crc: Crc,
  release_payment: ReleasePayment,
  query_no_reply: QueryNoReply,
}

async function loadPages(claimId: string): Promise<ClaimPages | null> {
  try {
    return await api.claimPages(claimId)
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null // not classified yet
    throw e
  }
}

export function TaskScreen({ taskId, user }: { taskId: string; user: User }) {
  const [state] = useAsync(() => api.task(taskId), [taskId, user.id])
  if (state.kind === 'loading') return <Loading />
  if (state.kind === 'error') {
    return (
      <div className="space-y-3">
        <ErrorList error={state.error} />
        <a className="text-sm text-accent hover:underline" href={href('/review')}>
          Back to the queue
        </a>
      </div>
    )
  }
  return <TaskLoaded task={state.data} />
}

function TaskLoaded({ task }: { task: Task }) {
  const [pagesState] = useAsync(() => loadPages(task.claim_id), [task.claim_id])
  const loaded = pagesState.kind === 'loaded' ? pagesState.data : null
  const pages = useMemo(() => loaded?.pages ?? [], [loaded])
  const documents = useMemo(() => loaded?.documents ?? [], [loaded])
  const [index, setIndex] = useState(0)
  const [cited, setCited] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const now = useNow(30_000)

  const focusPage = useCallback(
    (pageId: string) => {
      const i = pages.findIndex((p) => p.page_id === pageId)
      if (i >= 0) {
        setIndex(i)
        setCited(pageId)
      }
    },
    [pages],
  )

  const cite = useCallback(
    (ref: PageRef) => {
      const doc = documents.find((d) => d.document_id === ref.document_id)
      const pageId = doc?.page_ids[ref.page - 1]
      if (pageId) focusPage(pageId)
      else setError(new Error(`Page ${ref.page} of that document is not in the current page set`))
    },
    [documents, focusPage],
  )

  const submit = async (outcome: Outcome) => {
    setBusy(true)
    setError(null)
    try {
      await api.complete(task.id, outcome)
      navigate('/review')
    } catch (e) {
      setError(e)
      setBusy(false)
    }
  }

  const Kind = KINDS[task.kind]
  const payload = task.payload as { error?: string; correction_error?: string }
  const refused = payload.error ?? payload.correction_error
  const done = task.status === 'done'

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <a className="text-xs text-accent hover:underline" href={href('/review')}>
            Back to the queue
          </a>
          <h1 className="text-lg font-semibold">{task.title}</h1>
          <p className="text-sm text-muted">
            <a className="hover:underline" href={href(`/claims/${task.claim_id}`)}>
              {task.claim?.claim_number ?? 'Claim not numbered yet'}
            </a>
            {task.claim?.patient_name && <>, {task.claim.patient_name}</>}
            {task.claim?.claimed_amount && (
              <>
                , claimed <span className="num">{rupees(task.claim.claimed_amount)}</span>
              </>
            )}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Chip>{ROLE_LABELS[task.role] ?? task.role}</Chip>
          {task.system_of_medicine && <Chip tone="accent">{SYSTEM_LABELS[task.system_of_medicine]}</Chip>}
          {!done && <UrgencyChip task={task} now={now} />}
          {task.status === 'escalated' && <Chip tone="danger">Escalated</Chip>}
        </div>
      </div>

      {done && <Notice tone="ok">This task is complete.</Notice>}
      {refused && <Notice tone="danger">The last answer on this step was refused: {refused}</Notice>}

      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Card title="Documents" className="lg:sticky lg:top-3 lg:self-start">
          {pagesState.kind === 'loading' && <Loading label="Loading pages" />}
          {pagesState.kind === 'error' && <ErrorList error={pagesState.error} />}
          {pagesState.kind === 'loaded' && (
            <PageViewer
              pages={pages}
              index={index}
              onIndex={(i) => {
                setIndex(i)
                setCited(null)
              }}
              citedPageId={cited}
            />
          )}
        </Card>
        <Card title="Review">
          <div className="space-y-3">
            {Kind && !done ? (
              <Kind payload={task.payload} cite={cite} focusPage={focusPage} submit={submit} busy={busy} />
            ) : (
              <RawTask task={task} submit={submit} busy={busy} done={done} />
            )}
            <ErrorList error={error} />
          </div>
        </Card>
      </div>

      <Card title="Overrides on this claim">
        <ClaimOverrides claimId={task.claim_id} />
      </Card>
    </div>
  )
}

/** A task kind this screen does not know, or a finished task: show the data as it is. */
function RawTask({ task, submit, busy, done }: { task: Task; submit: (o: Outcome) => void; busy: boolean; done: boolean }) {
  const [text, setText] = useState('{"action": "confirm"}')
  const [parseError, setParseError] = useState<string | null>(null)
  return (
    <div className="space-y-3">
      <pre className="max-h-96 overflow-auto rounded bg-surface-2 p-2 text-xs">{JSON.stringify(task.payload, null, 2)}</pre>
      {done && task.outcome && (
        <>
          <p className="text-sm font-medium">Outcome</p>
          <pre className="overflow-auto rounded bg-surface-2 p-2 text-xs">{JSON.stringify(task.outcome, null, 2)}</pre>
        </>
      )}
      {!done && (
        <>
          <label htmlFor="raw-outcome" className="block text-sm font-medium">
            Outcome as JSON (unknown task kind {task.kind})
          </label>
          <textarea
            id="raw-outcome"
            rows={4}
            className="block w-full rounded-md border border-line bg-surface p-2 font-mono text-xs"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          {parseError && <p className="text-xs text-danger">{parseError}</p>}
          <Button
            variant="primary"
            disabled={busy}
            onClick={() => {
              try {
                setParseError(null)
                submit(JSON.parse(text) as Outcome)
              } catch {
                setParseError('That is not valid JSON')
              }
            }}
          >
            Submit outcome
          </Button>
        </>
      )}
    </div>
  )
}
