import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useTaskStore } from './tasks'

describe('task store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.stubGlobal('EventSource', class {
      onopen = null
      onmessage = null
      onerror = null
      addEventListener() {}
      close() {}
    })
  })

  it('tracks the final TaskRef contract by jobId and deduplicates updates', () => {
    const store = useTaskStore()
    store.track({ jobId: 'job-1', status: 'succeeded', deduplicated: false }, '同步网站')
    store.track({ jobId: 'job-1', status: 'failed', deduplicated: true }, '重复任务')
    expect(store.tasks).toHaveLength(1)
    expect(store.tasks[0]).toMatchObject({ id: 'job-1', title: '同步网站', status: 'failed', href: '/jobs/job-1' })
    expect(store.activeCount).toBe(0)
  })

  it('treats leased as active and effect_unknown as terminal', () => {
    const store = useTaskStore()
    store.track({ jobId: 'leased-job', status: 'leased', deduplicated: false })
    store.track({ jobId: 'unknown-job', status: 'effect_unknown', deduplicated: false })
    expect(store.activeCount).toBe(1)
  })
})
