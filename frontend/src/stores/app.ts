import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { apiErrorMessage, getBootstrap } from '@/api/client'
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

  const workerOnline = computed(() => Boolean(bootstrapData.value?.worker?.connected) || bootstrapData.value?.worker?.status === 'online')

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

  return { bootstrapData, booting, bootError, mobileNavOpen, taskDrawerOpen, toasts, workerOnline, bootstrap, toast, removeToast }
})
