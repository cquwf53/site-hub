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
  startedAt: string
  updatedAt: string
}

const terminal = new Set<TaskStatus>(['succeeded', 'failed', 'skipped', 'effect_unknown', 'cancelled'])

export const useTaskStore = defineStore('tasks', () => {
  const tasks = ref<TrackedTask[]>([])
  const streamConnected = ref(false)
  let source: EventSource | null = null
  let pollTimer: number | null = null

  const activeCount = computed(() => tasks.value.filter((task) => !terminal.has(task.status)).length)

  function track(taskRef: TaskRef, title?: string) {
    const now = new Date().toISOString()
    const id = taskRef.jobId
    const existing = tasks.value.find((item) => item.id === id)
    if (existing) {
      Object.assign(existing, { status: taskRef.status, href: taskRef.href || `/jobs/${id}`, updatedAt: now })
    } else {
      tasks.value.unshift({
        id,
        title: title || taskRef.kind || '后台任务',
        kind: taskRef.kind || 'job',
        href: taskRef.href || `/jobs/${id}`,
        status: taskRef.status,
        progress: null,
        message: '',
        startedAt: now,
        updatedAt: now,
      })
    }
    connect()
    return taskRef
  }

  function mergeJob(payload: Partial<Job>) {
    const id = payload.id
    if (!id) return
    const item = tasks.value.find((task) => task.id === id)
    if (!item) return
    if (payload.status) item.status = payload.status as TaskStatus
    if (typeof payload.progress === 'number') item.progress = payload.progress
    if (payload.message !== undefined || payload.error_message !== undefined) {
      item.message = payload.message || payload.error_message || ''
    }
    item.updatedAt = payload.updated_at || new Date().toISOString()
  }

  function handleEvent(event: MessageEvent) {
    try { mergeJob(JSON.parse(event.data) as Partial<Job>) } catch { /* ignore malformed heartbeat */ }
  }

  function connect() {
    if (typeof EventSource === 'undefined' || source) {
      if (!source) startPolling()
      return
    }
    source = new EventSource(taskStreamUrl(), { withCredentials: true })
    source.onopen = () => { streamConnected.value = true; stopPolling() }
    source.onmessage = handleEvent
    source.addEventListener('task', handleEvent as EventListener)
    source.onerror = () => {
      streamConnected.value = false
      source?.close()
      source = null
      startPolling()
    }
  }

  async function poll() {
    const pending = tasks.value.filter((task) => !terminal.has(task.status))
    await Promise.all(pending.map(async (task) => {
      try { mergeJob(await api.get<Job>(task.href.replace(/^\/api\/v1/, ''))) }
      catch (error) { task.message = apiErrorMessage(error) }
    }))
    if (!tasks.value.some((task) => !terminal.has(task.status))) stopPolling()
  }

  function startPolling() {
    if (pollTimer !== null || !tasks.value.some((task) => !terminal.has(task.status))) return
    void poll()
    pollTimer = window.setInterval(() => void poll(), 2500)
  }

  function stopPolling() {
    if (pollTimer !== null) window.clearInterval(pollTimer)
    pollTimer = null
  }

  function dismiss(id: string) { tasks.value = tasks.value.filter((task) => task.id !== id) }
  function clearFinished() { tasks.value = tasks.value.filter((task) => !terminal.has(task.status)) }

  onScopeDispose(() => { source?.close(); stopPolling() })
  return { tasks, activeCount, streamConnected, track, connect, dismiss, clearFinished }
})
