import { defineStore } from 'pinia'
import { computed, onScopeDispose, ref } from 'vue'
import { api, apiErrorMessage, taskStreamUrl } from '@/api/client'
import type { Job, TaskRef, TaskStatus } from '@/api/types'

export interface TrackedTask {
  id: string
  title: string
  kind: string
  href: string
  status: TaskStatus
  progress: number | null
  message: string
  result: Record<string, unknown> | null
  syncError: string
  startedAt: string
  updatedAt: string
  lastCheckedAt: string | null
}

function scheduleKey(value?: string | null) {
  const time = Date.parse(value || '')
  if (!Number.isFinite(time)) return null
  // HTTP models use Z while raw SSE can use +00:00. Preserve sub-ms
  // precision so two rapid retries still have distinct server boundaries.
  const fraction = /\.(\d+)/.exec(value || '')?.[1] || ''
  return `${Math.floor(time / 1000)}.${fraction.padEnd(9, '0').slice(0, 9)}`
}

const terminal = new Set<TaskStatus>(['succeeded', 'failed', 'skipped', 'effect_unknown', 'cancelled'])

export const useTaskStore = defineStore('tasks', () => {
  const tasks = ref<TrackedTask[]>([])
  const streamConnected = ref(false)
  let source: EventSource | null = null
  let pollTimer: number | null = null
  let disposed = false
  const inflight = new Map<string, Promise<void>>()
  const revisions = new Map<string, number>()
  const aborters = new Map<string, AbortController>()
  const retryFences = new Map<string, { awaitingSnapshot: boolean; scheduledFor: string | null }>()
  const activeCount = computed(() => tasks.value.filter((task) => !terminal.has(task.status)).length)

  function mergeJob(payload: Partial<Job>) {
    const item = tasks.value.find((task) => task.id === payload.id)
    if (!item) return
    // A late queued/running snapshot must never undo a terminal SSE update.
    if (payload.status && (!terminal.has(item.status) || terminal.has(payload.status as TaskStatus))) {
      item.status = payload.status as TaskStatus
    }
    const percent = payload.progress?.percent
    if (typeof percent === 'number' && Number.isFinite(percent)) item.progress = percent
    if (payload.message !== undefined || payload.error_message !== undefined) {
      item.message = payload.error_message || payload.message || ''
    }
    if (payload.result !== undefined) item.result = payload.result
    item.syncError = ''
    item.updatedAt = payload.updated_at || new Date().toISOString()
    item.lastCheckedAt = new Date().toISOString()
  }

  async function refreshTask(id: string) {
    if (disposed) return
    const pending = inflight.get(id)
    if (pending) return pending
    const task = tasks.value.find((item) => item.id === id)
    if (!task) return
    const revision = revisions.get(id) || 0
    const controller = new AbortController()
    aborters.set(id, controller)
    let timeout = 0
    const request = (async () => {
      try {
        const deadline = new Promise<never>((_resolve, reject) => {
          timeout = window.setTimeout(() => {
            reject(new Error('服务器响应超时，将继续核对'))
            controller.abort()
          }, 10000)
        })
        const job = await Promise.race([
          api.get<Job>(task.href.replace(/^\/api\/v1/, ''), undefined, { signal: controller.signal }),
          deadline,
        ])
        if (!disposed && (revisions.get(id) || 0) === revision) {
          const fence = retryFences.get(id)
          if (fence) { fence.awaitingSnapshot = false; fence.scheduledFor = scheduleKey(job.scheduled_for) }
          mergeJob(job)
        }
      } catch (error) {
        if (!disposed && (revisions.get(id) || 0) === revision) task.syncError = apiErrorMessage(error)
      } finally {
        window.clearTimeout(timeout)
        // An aborted previous attempt must not remove the new attempt's request.
        if (aborters.get(id) === controller) { inflight.delete(id); aborters.delete(id) }
      }
    })()
    inflight.set(id, request)
    return request
  }

  async function refresh() {
    await Promise.all(tasks.value.filter((task) => !terminal.has(task.status)).map((task) => refreshTask(task.id)))
  }

  function stopPolling() {
    if (pollTimer !== null) window.clearTimeout(pollTimer)
    pollTimer = null
  }

  function schedulePoll() {
    stopPolling()
    if (disposed || !activeCount.value) return
    pollTimer = window.setTimeout(async () => {
      pollTimer = null
      await refresh()
      schedulePoll()
    }, streamConnected.value ? 10000 : 2500)
  }

  function handleEvent(event: MessageEvent) {
    try {
      const payload = JSON.parse(event.data) as Partial<Job>
      if (!payload.id) return
      const fence = retryFences.get(payload.id)
      if (fence) {
        if (fence.awaitingSnapshot) return
        // Retry changes scheduled_for on the server. Frames from the old
        // execution may still be buffered even after the retry response.
        if (!scheduleKey(payload.scheduled_for) || !fence.scheduledFor) { void refreshTask(payload.id); return }
        if (scheduleKey(payload.scheduled_for) !== fence.scheduledFor) return
      }
      revisions.set(payload.id, (revisions.get(payload.id) || 0) + 1)
      mergeJob(payload)
      if (!activeCount.value) stopPolling()
    } catch { /* ignore malformed events */ }
  }

  function connect() {
    if (disposed || source || typeof EventSource === 'undefined') return
    source = new EventSource(taskStreamUrl(), { withCredentials: true })
    source.onopen = () => {
      streamConnected.value = true
      void refresh()
      schedulePoll()
    }
    source.onmessage = handleEvent
    source.addEventListener('task', handleEvent as EventListener)
    source.onerror = () => {
      streamConnected.value = false
      // EventSource reconnects automatically; polling remains authoritative.
      void refresh()
      schedulePoll()
    }
  }

  function track(taskRef: TaskRef, title?: string) {
    const now = new Date().toISOString()
    const existing = tasks.value.find((item) => item.id === taskRef.jobId)
    if (existing) {
      if (terminal.has(existing.status) && !terminal.has(taskRef.status)) {
        revisions.set(existing.id, (revisions.get(existing.id) || 0) + 1)
        aborters.get(existing.id)?.abort()
        aborters.delete(existing.id)
        inflight.delete(existing.id)
        retryFences.set(existing.id, { awaitingSnapshot: true, scheduledFor: null })
        Object.assign(existing, { progress: null, message: '', result: null, syncError: '', startedAt: now, updatedAt: now, lastCheckedAt: null })
      }
      existing.status = taskRef.status
      existing.href = taskRef.href || `/jobs/${taskRef.jobId}`
    } else {
      tasks.value.unshift({ id: taskRef.jobId, title: title || taskRef.kind || '后台任务',
        kind: taskRef.kind || 'job', href: taskRef.href || `/jobs/${taskRef.jobId}`,
        status: taskRef.status, progress: null, message: '', result: null, syncError: '',
        startedAt: now, updatedAt: now, lastCheckedAt: null })
    }
    connect()
    void refreshTask(taskRef.jobId)
    schedulePoll()
    return taskRef
  }

  function dismiss(id: string) {
    tasks.value = tasks.value.filter((task) => task.id !== id)
    revisions.delete(id)
    retryFences.delete(id)
    if (!activeCount.value) stopPolling()
  }
  function clearFinished() { tasks.value = tasks.value.filter((task) => !terminal.has(task.status)) }
  onScopeDispose(() => { disposed = true; source?.close(); stopPolling(); aborters.forEach(controller => controller.abort()) })
  return { tasks, activeCount, streamConnected, track, connect, dismiss, clearFinished, refreshTask, refresh }
})
