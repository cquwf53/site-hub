import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { NotificationChannel } from '@/api/types'
import NotificationsSettingsPage from './NotificationsSettingsPage.vue'

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

const settings = {
  enabled: true,
  price_drop: true,
  back_in_stock: true,
  checkin_failed: true,
  checkin_success: false,
  product_sync_failed: true,
  quiet_hours_enabled: false,
  quiet_hours_start: '23:00',
  quiet_hours_end: '08:00',
}

const channel: NotificationChannel = {
  id: 'channel-1',
  name: 'Home webhook',
  channel_type: 'webhook',
  enabled: true,
  endpoint_configured: true,
  config: { event_types: ['price_drop'], min_severity: 'warning' },
  delivery: { pending: 1, sent: 8, failed: 2 },
  created_at: '2026-08-03T00:00:00Z',
  updated_at: '2026-08-03T00:00:00Z',
}

function installResponses() {
  apiMocks.get.mockImplementation((path: string) => {
    if (path === '/settings/notifications') return Promise.resolve({ ...settings })
    if (path === '/notification-channels') return Promise.resolve({ items: [{ ...channel }], total: 1 })
    throw new Error(`unexpected GET ${path}`)
  })
  apiMocks.post.mockResolvedValue({ outbox_id: 'outbox-1', status: 'pending' })
  apiMocks.put.mockImplementation((_path: string, payload: unknown) => Promise.resolve(payload))
  apiMocks.patch.mockResolvedValue({ ...channel })
  apiMocks.delete.mockResolvedValue(undefined)
}

async function renderPage() {
  const wrapper = mount(NotificationsSettingsPage, {
    global: { plugins: [createPinia()], stubs: { Teleport: true } },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  installResponses()
})

describe('NotificationsSettingsPage', () => {
  it('shows channel metadata without ever rendering or pre-filling its endpoint', async () => {
    const wrapper = await renderPage()
    expect(wrapper.text()).toContain('Home webhook')
    expect(wrapper.text()).toContain('端点已配置')
    expect(wrapper.text()).toContain('待发送 1 · 已发送 8 · 失败 2')

    await wrapper.get('[data-testid="edit-channel-1"]').trigger('click')
    const endpoint = wrapper.get('#channel-endpoint')
    expect((endpoint.element as HTMLInputElement).value).toBe('')
    expect(endpoint.attributes('placeholder')).toContain('已配置')
  })

  it('updates a channel and only submits a replacement endpoint when one is entered', async () => {
    const wrapper = await renderPage()
    await wrapper.get('[data-testid="edit-channel-1"]').trigger('click')
    await wrapper.get('#channel-name').setValue('Renamed webhook')
    await wrapper.get('#channel-endpoint').setValue('https://hooks.example/new?token=new-secret')
    await wrapper.get('#channel-form').trigger('submit')
    await flushPromises()

    expect(apiMocks.patch).toHaveBeenCalledWith('/notification-channels/channel-1', {
      name: 'Renamed webhook',
      enabled: true,
      endpoint: 'https://hooks.example/new?token=new-secret',
      config: { event_types: ['price_drop'], min_severity: 'warning' },
    })
  })

  it('keeps the configured endpoint unchanged when an edit leaves it blank', async () => {
    const wrapper = await renderPage()
    await wrapper.get('[data-testid="edit-channel-1"]').trigger('click')
    await wrapper.get('#channel-name').setValue('Metadata only')
    await wrapper.get('#channel-form').trigger('submit')
    await flushPromises()

    expect(apiMocks.patch).toHaveBeenCalledWith('/notification-channels/channel-1', {
      name: 'Metadata only',
      enabled: true,
      config: { event_types: ['price_drop'], min_severity: 'warning' },
    })
    expect(apiMocks.patch.mock.calls[0]?.[1]).not.toHaveProperty('endpoint')
  })

  it('creates a webhook with explicit filters and preserves global event settings', async () => {
    const wrapper = await renderPage()
    await wrapper.get('form.card-body').trigger('submit')
    await flushPromises()
    expect(apiMocks.put).toHaveBeenCalledWith('/settings/notifications', settings)

    await wrapper.get('[data-testid="add-channel"]').trigger('click')
    await wrapper.get('#channel-name').setValue('Automation')
    await wrapper.get('#channel-endpoint').setValue('https://hooks.example/automation')
    await wrapper.get('.event-options input').setValue(true)
    await wrapper.get('#channel-severity').setValue('danger')
    await wrapper.get('#channel-form').trigger('submit')
    await flushPromises()

    expect(apiMocks.post).toHaveBeenCalledWith('/notification-channels', {
      name: 'Automation',
      channel_type: 'webhook',
      endpoint: 'https://hooks.example/automation',
      enabled: true,
      config: { event_types: ['price_drop'], min_severity: 'danger' },
    })
  })

  it('tests, disables, and requires a separate confirmation before deleting', async () => {
    const wrapper = await renderPage()
    await wrapper.get('[data-testid="test-channel-1"]').trigger('click')
    await flushPromises()
    expect(apiMocks.post).toHaveBeenCalledWith('/notification-channels/channel-1/test')

    await wrapper.get('[data-testid="toggle-channel-1"]').trigger('click')
    await flushPromises()
    expect(apiMocks.patch).toHaveBeenCalledWith('/notification-channels/channel-1', { enabled: false })

    await wrapper.get('[data-testid="delete-channel-1"]').trigger('click')
    expect(apiMocks.delete).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('此操作不能撤销')
    await wrapper.get('[data-testid="confirm-delete"]').trigger('click')
    await flushPromises()
    expect(apiMocks.delete).toHaveBeenCalledWith('/notification-channels/channel-1')
  })
})
