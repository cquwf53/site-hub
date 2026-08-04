import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SiteTypesSettingsPage from './SiteTypesSettingsPage.vue'

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  api: apiMocks,
  apiErrorMessage: (error: unknown) => error instanceof Error ? error.message : 'request failed',
}))

const systemType = {
  id: 'type-shop', name: '商城', slug: 'shop', color: '#d97706', icon: 'shopping-bag', sort_order: 10,
  enabled: true, is_system: true, is_default: false, site_count: 8,
  created_at: '2026-08-04T00:00:00Z', updated_at: '2026-08-04T00:00:00Z',
}
const customType = {
  id: 'type-forum', name: '论坛', slug: 'forum', color: '#6976d9', icon: '◫', sort_order: 20,
  enabled: true, is_system: false, is_default: false, site_count: 2,
  created_at: '2026-08-04T00:00:00Z', updated_at: '2026-08-04T00:00:00Z',
}

async function renderPage() {
  const wrapper = mount(SiteTypesSettingsPage, {
    global: { plugins: [createPinia()], stubs: { Teleport: true } },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  apiMocks.get.mockResolvedValue([{ ...systemType }, { ...customType }])
  apiMocks.post.mockResolvedValue({ ...customType })
  apiMocks.patch.mockResolvedValue({ ...systemType })
  apiMocks.delete.mockResolvedValue(undefined)
})

describe('SiteTypesSettingsPage', () => {
  it('renders classification metadata and maps internal icon codes to readable glyphs', async () => {
    const wrapper = await renderPage()

    expect(wrapper.text()).toContain('商城')
    expect(wrapper.text()).toContain('shop · 8 个网站 · 排序 10')
    expect(wrapper.text()).toContain('▣')
    expect(wrapper.text()).not.toContain('shopping-bag')
    expect(wrapper.get('[data-testid="delete-site-type-type-shop"]').attributes('disabled')).toBeDefined()
  })

  it('creates a type without sending an invalid empty icon', async () => {
    const wrapper = await renderPage()
    await wrapper.get('[data-testid="add-site-type"]').trigger('click')
    await wrapper.get('#site-type-name').setValue('社区')
    await wrapper.get('#site-type-slug').setValue('community')
    await wrapper.get('#site-type-form').trigger('submit')
    await flushPromises()

    expect(apiMocks.post).toHaveBeenCalledWith('/site-types', {
      name: '社区',
      slug: 'community',
      color: '#6976d9',
      sort_order: 30,
      enabled: true,
    })
  })

  it('keeps protected system fields and the existing icon when they are not editable', async () => {
    const wrapper = await renderPage()
    await wrapper.get('[data-testid="edit-site-type-type-shop"]').trigger('click')
    await wrapper.get('#site-type-name').setValue('购物商城')
    await wrapper.get('#site-type-icon').setValue('')
    await wrapper.get('#site-type-form').trigger('submit')
    await flushPromises()

    expect(apiMocks.patch).toHaveBeenCalledWith('/site-types/type-shop', {
      name: '购物商城',
      color: '#d97706',
      sort_order: 10,
    })
  })

  it('keeps a visible conflict error when deleting a type that is still in use', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    apiMocks.delete.mockRejectedValueOnce(new Error('仍有网站使用此类型，请先重新分类'))
    const wrapper = await renderPage()

    await wrapper.get('[data-testid="delete-site-type-type-forum"]').trigger('click')
    await flushPromises()

    expect(apiMocks.delete).toHaveBeenCalledWith('/site-types/type-forum')
    expect(wrapper.get('[role="alert"]').text()).toContain('仍有网站使用此类型')
  })
})
