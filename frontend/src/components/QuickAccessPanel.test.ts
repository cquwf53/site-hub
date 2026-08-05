import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import QuickAccessPanel from './QuickAccessPanel.vue'

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  api: apiMocks,
  apiErrorMessage: (error: unknown) => error instanceof Error ? error.message : 'request failed',
}))

const siteBase = {
  folder_id: null,
  status: 'active',
  pinned: false,
  overview_hidden: false,
  tags: [],
  capabilities: ['bookmark'],
}

let response: {
  frequent: Array<Record<string, unknown>>
  recent: Array<Record<string, unknown>>
  has_usage: boolean
}

async function renderPanel() {
  const wrapper = mount(QuickAccessPanel, {
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
  response = {
    frequent: [
      { ...siteBase, id: 'site-pinned', name: '置顶站', url: 'https://pinned.example', pinned: true, visit_count: 9, last_visited_at: '2026-08-05T08:00:00Z', recommendation_reason: 'pinned' },
      { ...siteBase, id: 'site-frequent', name: '常用站', url: 'https://frequent.example', visit_count: 3, last_visited_at: '2026-08-04T08:00:00Z', recommendation_reason: 'frequent' },
    ],
    recent: [
      { ...siteBase, id: 'site-recent', name: '最近站', url: 'https://recent.example', visit_count: 1, last_visited_at: '2026-08-06T08:00:00Z', recommendation_reason: 'recent' },
    ],
    has_usage: true,
  }
  apiMocks.get.mockImplementation((path: string, query?: Record<string, unknown>) => {
    if (path !== '/overview/sites') throw new Error(`unexpected GET ${path}`)
    expect(query).toEqual({ limit: 8 })
    return Promise.resolve(structuredClone(response))
  })
  apiMocks.post.mockResolvedValue(undefined)
  apiMocks.patch.mockResolvedValue({})
})

describe('QuickAccessPanel', () => {
  it('opens a website, records the visit locally and keeps the cards in place', async () => {
    const wrapper = await renderPanel()
    const namesBefore = wrapper.findAll('.quick-site-copy strong').map((item) => item.text())

    await wrapper.get('button[aria-label="打开 常用站"]').trigger('click')
    await flushPromises()

    expect(apiMocks.post).toHaveBeenCalledWith('/sites/site-frequent/open')
    expect(apiMocks.get).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('常用 · 4 次')
    expect(wrapper.findAll('.quick-site-copy strong').map((item) => item.text())).toEqual(namesBefore)

    await wrapper.get('#quick-tab-recent').trigger('click')
    expect(wrapper.findAll('.quick-site-copy strong').map((item) => item.text())).toEqual(['常用站', '最近站'])
  })

  it('switches ranking tabs and supports pinning and hiding', async () => {
    const wrapper = await renderPanel()

    const frequentTab = wrapper.get('#quick-tab-frequent')
    const recentTab = wrapper.get('#quick-tab-recent')
    expect(frequentTab.attributes('tabindex')).toBe('0')
    expect(recentTab.attributes('tabindex')).toBe('-1')
    await frequentTab.trigger('keydown', { key: 'ArrowRight' })
    await flushPromises()
    expect(recentTab.attributes('aria-selected')).toBe('true')
    expect(recentTab.attributes('tabindex')).toBe('0')

    expect(wrapper.text()).toContain('最近站')
    expect(wrapper.text()).not.toContain('常用站')

    await wrapper.get('#quick-tab-frequent').trigger('click')
    await wrapper.get('button[aria-label="置顶 常用站"]').trigger('click')
    await flushPromises()
    expect(apiMocks.patch).toHaveBeenCalledWith('/sites/site-frequent', { pinned: true })

    await wrapper.get('button[aria-label="从快捷访问隐藏 常用站"]').trigger('click')
    await flushPromises()
    expect(apiMocks.patch).toHaveBeenCalledWith('/sites/site-frequent', { overview_hidden: true })
  })

  it('explains cold start and requires confirmation before clearing usage', async () => {
    response = {
      frequent: [{ ...siteBase, id: 'site-starter', name: '起步站', url: 'https://starter.example', visit_count: 0, recommendation_reason: 'starter' }],
      recent: [],
      has_usage: false,
    }
    const wrapper = await renderPanel()

    expect(wrapper.get('.learning-note').text()).toContain('自动学习')
    expect(wrapper.find('.reset-usage').exists()).toBe(false)
    await wrapper.get('#quick-tab-recent').trigger('click')
    expect(wrapper.text()).toContain('从 SiteHub 打开网站后，最近访问会显示在这里')
    await wrapper.get('#quick-tab-frequent').trigger('click')

    response.has_usage = true
    await wrapper.get('button[aria-label="打开 起步站"]').trigger('click')
    await flushPromises()
    await wrapper.get('#quick-tab-recent').trigger('click')
    expect(wrapper.text()).toContain('起步站')
    const reset = wrapper.get('.reset-usage')

    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    await reset.trigger('click')
    expect(apiMocks.post).not.toHaveBeenCalledWith('/overview/sites/reset-usage')

    response.has_usage = false
    await reset.trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledTimes(2)
    expect(apiMocks.post).toHaveBeenCalledWith('/overview/sites/reset-usage')
    expect(wrapper.find('.reset-usage').exists()).toBe(false)
  })
})
