import { createPinia, disposePinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAppStore } from './app'
const mocks = vi.hoisted(() => ({ get: vi.fn(), getBootstrap: vi.fn() }))
vi.mock('@/api/client', () => ({ api: { get: mocks.get }, getBootstrap: mocks.getBootstrap, apiErrorMessage: (error: Error) => error.message }))
let pinia: ReturnType<typeof createPinia>
const tick = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }
beforeEach(() => { vi.useFakeTimers(); vi.clearAllMocks(); pinia = createPinia(); setActivePinia(pinia); mocks.get.mockResolvedValue({ status: 'online', connected: true }); mocks.getBootstrap.mockResolvedValue({ worker: { status: 'online', connected: true } }) })
afterEach(() => { disposePinia(pinia); vi.useRealTimers() })
describe('worker presence reconciliation', () => {
  it('refreshes every fifteen seconds instead of permanently trusting the bootstrap snapshot', async () => {
    const store = useAppStore(); await store.bootstrap(); store.startWorkerMonitoring(); await tick()
    expect(store.workerOnline).toBe(true)
    mocks.get.mockResolvedValue({ status: 'offline', connected: false })
    await vi.advanceTimersByTimeAsync(15000)
    expect(store.workerOnline).toBe(false); expect(store.workerStatusLabel).toBe('Worker 未连接')
    store.stopWorkerMonitoring(); const calls = mocks.get.mock.calls.length
    await vi.advanceTimersByTimeAsync(30000); window.dispatchEvent(new Event('focus')); await tick()
    expect(mocks.get).toHaveBeenCalledTimes(calls)
  })
  it('reports uncertainty on network failure and rechecks when the window regains focus', async () => {
    const store = useAppStore(); store.startWorkerMonitoring(); await tick()
    mocks.get.mockRejectedValue(new Error('network offline'))
    window.dispatchEvent(new Event('focus')); await tick()
    expect(store.workerOnline).toBe(false); expect(store.workerStatusLabel).toBe('Worker 状态暂无法确认')
    mocks.get.mockResolvedValue({ status: 'online', connected: true })
    window.dispatchEvent(new Event('focus')); await tick()
    expect(store.workerOnline).toBe(true); expect(store.workerStatusError).toBe('')
  })
})
