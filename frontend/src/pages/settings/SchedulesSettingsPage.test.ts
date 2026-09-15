import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SchedulesSettingsPage from './SchedulesSettingsPage.vue'

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  api: apiMocks,
  apiErrorMessage: (error: unknown) => error instanceof Error ? error.message : 'request failed',
}))

const scheduleItems = [
  {
    id: 'product-schedule', capability: 'product_monitor', site_id: 'site-product', site_name: '商品站',
    account_id: null, account_label: null, enabled: false, policy_type: 'interval', timezone: 'Asia/Shanghai',
    next_run_at: null, misfire_policy: 'recover', policy: { minutes: 15, override: false },
    scraper_type: 'manual', automatic_run_supported: false, automatic_run_reason: 'manual_catalog',
    current_status: 'queued', last_status: 'failed', last_error_message: 'certificate expired',
  },
  {
    id: 'checkin-schedule', capability: 'checkin', site_id: 'site-checkin', site_name: '签到站',
    account_id: 'account-ready', account_label: '主账号', enabled: false, policy_type: 'daily_random_window',
    timezone: 'Asia/Shanghai', next_run_at: null, misfire_policy: 'recover', policy: {},
  },
  {
    id: 'expired-schedule', capability: 'checkin', site_id: 'site-expired', site_name: '失效站',
    account_id: 'account-expired', account_label: '旧账号', enabled: false, policy_type: 'cron',
    timezone: 'Asia/Shanghai', next_run_at: null, misfire_policy: 'recover', policy: { mode: 'fixed_time', time: '09:30' },
  },
]

function installResponses(failingAccountSite?: string) {
  apiMocks.get.mockImplementation((path: string) => {
    if (path === '/settings/schedules') return Promise.resolve({
      timezone: 'Asia/Shanghai', checkin_window_start: '08:00:00', checkin_window_end: '22:00:00', product_interval_minutes: 60,
    })
    if (path === '/schedules') return Promise.resolve({ items: scheduleItems.map((item) => ({ ...item, policy: { ...item.policy } })), page: 1, page_size: 200, total: 3 })
    if (path === `/sites/${failingAccountSite}/accounts`) return Promise.reject(new Error('账号状态接口暂不可用'))
    if (path === '/sites/site-checkin/accounts') return Promise.resolve([{ id: 'account-ready', site_id: 'site-checkin', label: '主账号', enabled: false, auth_state: 'ready', configured: true, configured_secret_fields: [] }])
    if (path === '/sites/site-expired/accounts') return Promise.resolve([{ id: 'account-expired', site_id: 'site-expired', label: '旧账号', enabled: false, auth_state: 'expired', configured: true, configured_secret_fields: [] }])
    throw new Error(`unexpected GET ${path}`)
  })
  apiMocks.put.mockImplementation((path: string, payload: Record<string, unknown>) => {
    if (path === '/settings/schedules') return Promise.resolve(payload)
    return Promise.resolve({ enabled: payload.enabled })
  })
  apiMocks.patch.mockResolvedValue({ enabled: true })
}

async function renderPage() {
  const wrapper = mount(SchedulesSettingsPage, {
    global: {
      plugins: [createPinia()],
      stubs: { RouterLink: { template: '<a><slot /></a>' } },
    },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  installResponses()
})

describe('SchedulesSettingsPage', () => {
  it('renders global settings and readable schedule policies', async () => {
    const wrapper = await renderPage()

    expect((wrapper.get('#checkin-start').element as HTMLInputElement).value).toBe('08:00')
    expect((wrapper.get('#checkin-end').element as HTMLInputElement).value).toBe('22:00')
    expect(wrapper.text()).toContain('每 60 分钟（继承全局）')
    expect(wrapper.text()).toContain('08:00–22:00 内随机（全局）')
    expect(wrapper.text()).toContain('每天 09:30')
    expect(wrapper.text()).toContain('失败')
    expect(wrapper.text()).toContain('当前')
    expect(wrapper.text()).toContain('已排队')
    expect(wrapper.text()).toContain('certificate expired')
    expect(wrapper.text()).toContain('当前为手工商品目录')
    expect(wrapper.text()).not.toContain('每 15 分钟（继承全局）')
  })

  it('normalizes clock values when saving the global policy', async () => {
    const wrapper = await renderPage()
    await wrapper.get('#checkin-start').setValue('09:15')
    await wrapper.get('#checkin-end').setValue('20:45')
    await wrapper.get('#product-interval').setValue(90)
    await wrapper.get('form.strategy-form').trigger('submit')
    await flushPromises()

    expect(apiMocks.put).toHaveBeenCalledWith('/settings/schedules', {
      timezone: 'Asia/Shanghai',
      checkin_window_start: '09:15:00',
      checkin_window_end: '20:45:00',
      product_interval_minutes: 90,
    })
    expect(wrapper.text()).toContain('每 90 分钟（继承全局）')
  })

  it('uses capability owner APIs when enabling schedules', async () => {
    const wrapper = await renderPage()

    await wrapper.get('[data-testid="toggle-schedule-product-schedule"]').trigger('click')
    await wrapper.get('[data-testid="toggle-schedule-checkin-schedule"]').trigger('click')
    await flushPromises()

    expect(apiMocks.put).toHaveBeenCalledWith('/sites/site-product/product-settings', { enabled: true })
    expect(apiMocks.patch).toHaveBeenCalledWith('/accounts/account-ready', { enabled: true })
  })

  it('does not enable a check-in schedule whose account login is expired', async () => {
    const wrapper = await renderPage()
    const toggle = wrapper.get('[data-testid="toggle-schedule-expired-schedule"]')

    expect(toggle.attributes('disabled')).toBeDefined()
    expect(toggle.attributes('title')).toContain('登录状态不是 ready')
    await toggle.trigger('click')
    expect(apiMocks.patch).not.toHaveBeenCalledWith('/accounts/account-expired', { enabled: true })
  })

  it('shows account-state request failures without misreporting them as expired login', async () => {
    installResponses('site-checkin')
    const wrapper = await renderPage()
    const toggle = wrapper.get('[data-testid="toggle-schedule-checkin-schedule"]')

    expect(wrapper.get('[role="alert"]').text()).toContain('部分签到账号状态加载失败')
    expect(toggle.attributes('disabled')).toBeDefined()
    expect(toggle.attributes('title')).toContain('账号状态加载失败')
    expect(toggle.attributes('title')).toContain('账号状态接口暂不可用')
    expect(toggle.attributes('title')).not.toContain('登录状态不是 ready')
    await toggle.trigger('click')
    expect(apiMocks.patch).not.toHaveBeenCalledWith('/accounts/account-ready', { enabled: true })
  })

  it('passes capability and enabled filters to the list endpoint', async () => {
    const wrapper = await renderPage()
    await wrapper.get('select[aria-label="能力类型"]').setValue('product_monitor')
    await wrapper.get('select[aria-label="启用状态"]').setValue('false')
    await flushPromises()

    expect(apiMocks.get).toHaveBeenCalledWith('/schedules', {
      page: 1,
      page_size: 200,
      capability: 'product_monitor',
      enabled: false,
    })
  })
  it('displays the server blocking reason and an actionable configuration link beside the next run', async () => {
    const original = apiMocks.get.getMockImplementation()!
    apiMocks.get.mockImplementation((path: string, ...args: unknown[]) => path === '/schedules'
      ? Promise.resolve({ items: [{ ...scheduleItems[1], enabled: true, next_run_at: '2026-09-16T01:00:00Z', blocked_reason: '账号登录已过期', next_action: { label: '重新登录并验证', href: '/sites/site-checkin/accounts', description: '完成登录后恢复计划' } }], page: 1, page_size: 200, total: 1 })
      : original(path, ...args))
    const wrapper = await renderPage()
    expect(wrapper.text()).toContain('账号登录已过期')
    expect(wrapper.text()).toContain('重新登录并验证')
    expect(wrapper.text()).toContain('解除阻止后')
    wrapper.unmount()
  })

})
