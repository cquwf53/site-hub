import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import OverviewPage from './OverviewPage.vue'

const apiMocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() }))

vi.mock('@/api/client', async () => {
  const actual = await vi.importActual<typeof import('@/api/client')>('@/api/client')
  return {
    api: apiMocks,
    ApiError: actual.ApiError,
    apiErrorMessage: (error: unknown) => error instanceof Error ? error.message : 'request failed',
  }
})

const emptyPage = { items: [], page: 1, page_size: 6, total: 0 }
const scheduleWindow = {
  timezone: 'Asia/Shanghai', checkin_window_start: '08:00:00', checkin_window_end: '22:00:00',
  product_interval_minutes: 60,
}
// 2026-08-05T07:56:53Z is 15:56:53 in Asia/Shanghai.
const todayBatch = {
  id: 'batch-today', planned_at: '2026-08-05T07:56:53.000Z', status: 'running', trigger_reason: 'normal',
  items: [{ id: 'a', status: 'succeeded' }, { id: 'b', status: 'skipped' }, { id: 'c', status: 'queued' }],
}

function installResponses(batch: (() => Promise<unknown>) | null) {
  apiMocks.get.mockImplementation((path: string) => {
    if (path === '/settings/schedules') return Promise.resolve(scheduleWindow)
    if (path === '/schedules/checkin-batch/today') {
      return batch ? batch() : Promise.resolve(todayBatch)
    }
    if (path === '/overview/sites') return Promise.resolve({ frequent: [], recent: [], has_usage: false })
    if (['/sites', '/products', '/events', '/jobs'].includes(path)) return Promise.resolve({ ...emptyPage })
    throw new Error(`unexpected GET ${path}`)
  })
}

async function renderPage() {
  const wrapper = mount(OverviewPage, {
    global: { plugins: [createPinia()], stubs: { RouterLink: { template: '<a><slot /></a>' } } },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  installResponses(null)
})

describe('OverviewPage check-in plan', () => {
  it("shows today's randomized check-in time in the business timezone", async () => {
    const wrapper = await renderPage()
    const plan = wrapper.get('.checkin-plan')

    expect(plan.get('.plan-headline strong').text()).toBe('15:56')
    expect(plan.get('.plan-headline strong').attributes('title')).toContain('15:56:53')
    expect(plan.text()).toContain('08:00–22:00 内随机')
    expect(plan.text()).toContain('Asia/Shanghai')
    expect(plan.get('.plan-progress').text()).toContain('2/3')
    expect(plan.text()).toContain('运行中')
    expect(plan.text()).not.toContain('错过后已顺延')
  })

  it('marks a batch that was deferred by misfire recovery', async () => {
    installResponses(() => Promise.resolve({ ...todayBatch, trigger_reason: 'regenerated' }))
    const wrapper = await renderPage()

    expect(wrapper.get('.checkin-plan').text()).toContain('错过后已顺延')
  })

  it('treats an unplanned day as a normal state instead of a page error', async () => {
    const { ApiError } = await vi.importActual<typeof import('@/api/client')>('@/api/client')
    installResponses(() => Promise.reject(new ApiError(404, { error: { code: 'NOT_FOUND', message: 'no plan' } })))
    const wrapper = await renderPage()

    expect(wrapper.find('.error-state').exists()).toBe(false)
    expect(wrapper.get('.plan-headline strong').text()).toBe('—')
    expect(wrapper.get('.checkin-plan').text()).toContain('今日尚未安排')
    expect(wrapper.find('.plan-progress').exists()).toBe(false)
  })

  it('keeps the rest of the overview usable when the batch request fails', async () => {
    installResponses(() => Promise.reject(new Error('批次接口暂不可用')))
    const wrapper = await renderPage()

    expect(wrapper.find('.error-state').exists()).toBe(false)
    expect(wrapper.get('.checkin-plan').text()).toContain('今日批次读取失败：批次接口暂不可用')
    expect(wrapper.find('.recent-products').exists()).toBe(true)
  })
})
