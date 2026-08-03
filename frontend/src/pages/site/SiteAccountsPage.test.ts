import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import SiteAccountsPage from './SiteAccountsPage.vue'

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

vi.mock('./siteContext', async () => {
  const { ref } = await import('vue')
  return {
    useSiteDetail: () => ({
      site: ref({ id: 'site-1', name: 'Example', url: 'https://example.test' }),
    }),
  }
})

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  apiMocks.get.mockImplementation((path: string) => {
    if (path === '/sites/site-1/accounts') return Promise.resolve([])
    if (path === '/adapters') {
      return Promise.resolve([{
        id: 'classic',
        name: 'VShop Classic',
        version: '1.0.0',
        capabilities: ['product_monitor'],
        secret_fields: [],
      }])
    }
    throw new Error(`unexpected GET ${path}`)
  })
})

describe('SiteAccountsPage', () => {
  it('does not fall back to a product adapter when no check-in adapter is available', async () => {
    const wrapper = mount(SiteAccountsPage, {
      global: { plugins: [createPinia()], stubs: { Teleport: true } },
    })
    await flushPromises()

    const add = wrapper.findAll('button').find((button) => button.text().includes('添加账号'))
    expect(add).toBeDefined()
    await add!.trigger('click')

    const select = wrapper.get('#account-adapter')
    expect((select.element as HTMLSelectElement).value).toBe('')
    expect(wrapper.text()).toContain('签到 Worker 尚未提供可用适配器')
    expect(wrapper.get('[form="account-editor"]').attributes('disabled')).toBeDefined()

    await wrapper.get('#account-editor').trigger('submit')
    await flushPromises()
    expect(apiMocks.post).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
