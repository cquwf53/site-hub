import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useTaskStore } from './tasks'
const mocks = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/api/client', () => ({ api: mocks, taskStreamUrl: () => '/api/v1/stream', apiErrorMessage: (error: Error) => error.message }))
let stream: { onopen: (() => void) | null; onerror: (() => void) | null; event: EventListener | null }
let pinia: ReturnType<typeof createPinia>
const tick = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }
beforeEach(() => {
  vi.useFakeTimers(); mocks.get.mockReset()
  pinia = createPinia(); setActivePinia(pinia)
  vi.stubGlobal('EventSource', class {
    onopen = null; onerror = null; onmessage = null; event = null
    constructor() { stream = this }
    addEventListener(_type: string, listener: EventListener) { stream.event = listener }
    close() {}
  })
  mocks.get.mockImplementation((path: string) => Promise.resolve({ id: path.split('/').pop(), status: 'running' }))
})
afterEach(() => { disposePinia(pinia); vi.useRealTimers(); vi.unstubAllGlobals() })
describe('task state reconciliation', () => {
  it('fetches a snapshot for a fast job whose final event arrived before tracking', async () => {
    mocks.get.mockResolvedValue({ id: 'job-1', status: 'succeeded' })
    const store = useTaskStore()
    store.track({ kind: 'checkin',  jobId: 'job-1', status: 'queued', deduplicated: false, href: '/api/v1/jobs/job-1' })
    await tick()
    expect(mocks.get).toHaveBeenCalledWith('/jobs/job-1', undefined, expect.objectContaining({ signal: expect.any(AbortSignal) }))
    expect(store.tasks[0]?.status).toBe('succeeded')
    expect(store.activeCount).toBe(0)
  })
  it('reconciles on stream connection and periodically even when SSE stays open', async () => {
    const store = useTaskStore(); store.track({ kind: 'checkin', href: '/api/v1/jobs/job-1',  jobId: 'job-1', status: 'leased', deduplicated: false })
    await tick(); stream.onopen?.(); await tick()
    expect(mocks.get).toHaveBeenCalledTimes(2)
    mocks.get.mockResolvedValue({ id: 'job-1', status: 'failed', error_message: 'timeout' })
    await vi.advanceTimersByTimeAsync(10000)
    expect(store.tasks[0]).toMatchObject({ status: 'failed', message: 'timeout', syncError: '' })
    expect(store.activeCount).toBe(0)
  })
  it('does not let an old HTTP response overwrite a newer terminal event', async () => {
    let resolve!: (value: unknown) => void
    mocks.get.mockReturnValue(new Promise((done) => { resolve = done }))
    const store = useTaskStore(); store.track({ kind: 'checkin', href: '/api/v1/jobs/job-1',  jobId: 'job-1', status: 'queued', deduplicated: false })
    stream.event?.(new MessageEvent('task', { data: JSON.stringify({ id: 'job-1', status: 'failed', error_message: 'Profile expired' }) }))
    resolve({ id: 'job-1', status: 'running' }); await tick()
    expect(store.tasks[0]).toMatchObject({ status: 'failed', message: 'Profile expired' })
  })
  it('keeps execution state while a failed poll is visibly uncertain, then recovers', async () => {
    mocks.get.mockRejectedValue(new Error('offline'))
    const store = useTaskStore(); store.track({ kind: 'checkin', href: '/api/v1/jobs/job-1',  jobId: 'job-1', status: 'running', deduplicated: false })
    await tick()
    expect(store.tasks[0]).toMatchObject({ status: 'running', syncError: 'offline' })
    mocks.get.mockResolvedValue({ id: 'job-1', status: 'effect_unknown' })
    await vi.advanceTimersByTimeAsync(2500)
    expect(store.tasks[0]).toMatchObject({ status: 'effect_unknown', syncError: '' })
    expect(store.activeCount).toBe(0)
  })
  it('deduplicates TaskRefs and does not overlap snapshots for one job', async () => {
    mocks.get.mockReturnValue(new Promise(() => {}))
    const store = useTaskStore()
    store.track({ kind: 'checkin', href: '/api/v1/jobs/job-1',  jobId: 'job-1', status: 'queued', deduplicated: false }, '同步网站')
    store.track({ kind: 'checkin', href: '/api/v1/jobs/job-1',  jobId: 'job-1', status: 'running', deduplicated: true })
    expect(store.tasks).toHaveLength(1); expect(mocks.get).toHaveBeenCalledTimes(1)
  })
  it('times out a hanging snapshot, aborts it and resumes polling', async () => {
    mocks.get.mockReturnValueOnce(new Promise(() => {}))
    const store = useTaskStore()
    store.track({ kind: 'checkin', href: '/jobs/job-1', jobId: 'job-1', status: 'running', deduplicated: false })
    await vi.advanceTimersByTimeAsync(10000)
    expect(mocks.get.mock.calls[0]?.[2].signal.aborted).toBe(true)
    expect(store.tasks[0]?.syncError).toContain('超时')
    mocks.get.mockResolvedValue({ id: 'job-1', status: 'succeeded' })
    await vi.advanceTimersByTimeAsync(2500)
    expect(store.tasks[0]).toMatchObject({ status: 'succeeded', syncError: '' })
  })

  it('tracks an explicit same-id retry and isolates the previous attempt HTTP and SSE responses', async () => {
    let resolveOld!: (value: unknown) => void
    let resolveNew!: (value: unknown) => void
    mocks.get.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve }))
    const store = useTaskStore()
    const reference = { kind: 'checkin' as const, href: '/jobs/job-1', jobId: 'job-1', deduplicated: false }
    const previousSchedule = '2026-09-15T01:00:00Z'
    const newSchedule = '2026-09-15T02:00:00Z'
    store.track({ ...reference, status: 'running' })
    stream.event?.(new MessageEvent('task', { data: JSON.stringify({ id: 'job-1', status: 'failed', scheduled_for: previousSchedule, error_message: 'old failure' }) }))
    expect(store.activeCount).toBe(0)
    mocks.get.mockReturnValueOnce(new Promise(resolve => { resolveNew = resolve }))
    store.track({ ...reference, status: 'queued' }, '重试签到')
    expect(store.activeCount).toBe(1)
    expect(store.tasks[0]).toMatchObject({ status: 'queued', message: '', progress: null })
    expect(mocks.get.mock.calls[0]?.[2].signal.aborted).toBe(true)
    stream.event?.(new MessageEvent('task', { data: JSON.stringify({ id: 'job-1', status: 'failed', scheduled_for: previousSchedule }) }))
    resolveOld({ id: 'job-1', status: 'failed', scheduled_for: previousSchedule, error_message: 'late old HTTP' })
    await tick()
    void store.refreshTask('job-1')
    expect(mocks.get).toHaveBeenCalledTimes(2)
    expect(store.tasks[0]?.status).toBe('queued')
    resolveNew({ id: 'job-1', status: 'running', scheduled_for: newSchedule }); await tick()
    stream.event?.(new MessageEvent('task', { data: JSON.stringify({ id: 'job-1', status: 'failed', scheduled_for: previousSchedule, error_message: 'late old SSE' }) }))
    expect(store.tasks[0]).toMatchObject({ status: 'running', message: '' })
    expect(store.activeCount).toBe(1)
    stream.event?.(new MessageEvent('task', { data: JSON.stringify({ id: 'job-1', status: 'succeeded', scheduled_for: newSchedule.replace('Z', '+00:00') }) }))
    expect(store.tasks[0]?.status).toBe('succeeded'); expect(store.activeCount).toBe(0)
  })

})
