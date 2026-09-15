import { defineStore } from 'pinia'
import { computed, onScopeDispose, ref } from 'vue'
import { api, apiErrorMessage, getBootstrap } from '@/api/client'
import type { Bootstrap } from '@/api/types'

export interface Toast {
  id: number
  tone: 'success' | 'error' | 'info'
  message: string
}

export const useAppStore = defineStore('app', () => {
  const bootstrapData = ref<Bootstrap | null>(null)
  const booting = ref(false)
  const bootError = ref('')
  const mobileNavOpen = ref(false)
  const taskDrawerOpen = ref(false)
  const toasts = ref<Toast[]>([])
  let toastId = 0
  const workerState = ref<Bootstrap['worker'] | null>(null)
  const workerStatusError = ref('')
  let workerTimer: number | null = null
  let workerRequest: Promise<void> | null = null
  let workerController: AbortController | null = null

  const workerOnline = computed(() => {
    const state = workerState.value || bootstrapData.value?.worker
    return !workerStatusError.value && (Boolean(state?.connected) || state?.status === 'online')
  })
  const workerStatusLabel = computed(() => workerStatusError.value ? 'Worker 状态暂无法确认' : workerOnline.value ? 'Worker 已连接' : 'Worker 未连接')

  async function refreshWorker() {
    if (workerRequest) return workerRequest
    const controller = new AbortController()
    workerController = controller
    const timeout = window.setTimeout(() => controller.abort(), 10000)
    workerRequest = (async () => {
      try {
        workerState.value = await api.get<Bootstrap['worker']>('/worker/status', undefined, { signal: controller.signal })
        workerStatusError.value = ''
      } catch (error) { workerStatusError.value = apiErrorMessage(error) }
      finally { window.clearTimeout(timeout); workerRequest = null; workerController = null }
    })()
    return workerRequest
  }
  function onWindowFocus() { void refreshWorker() }
  function startWorkerMonitoring() {
    if (workerTimer !== null) return
    void refreshWorker()
    workerTimer = window.setInterval(() => void refreshWorker(), 15000)
    window.addEventListener('focus', onWindowFocus)
  }
  function stopWorkerMonitoring() {
    if (workerTimer !== null) window.clearInterval(workerTimer)
    workerTimer = null
    window.removeEventListener('focus', onWindowFocus)
    workerController?.abort()
  }
  onScopeDispose(stopWorkerMonitoring)

  async function bootstrap(force = false) {
    booting.value = true
    bootError.value = ''
    try { bootstrapData.value = await getBootstrap(force) }
    catch (error) { bootError.value = apiErrorMessage(error) }
    finally { booting.value = false }
  }

  function toast(message: string, tone: Toast['tone'] = 'info') {
    const id = ++toastId
    toasts.value.push({ id, tone, message })
    window.setTimeout(() => removeToast(id), 4200)
  }

  function removeToast(id: number) {
    toasts.value = toasts.value.filter((item) => item.id !== id)
  }

  return { bootstrapData, booting, bootError, mobileNavOpen, taskDrawerOpen, toasts, workerOnline, workerStatusLabel, workerStatusError, refreshWorker, startWorkerMonitoring, stopWorkerMonitoring, bootstrap, toast, removeToast }
})
