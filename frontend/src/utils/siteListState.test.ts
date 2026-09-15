import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, describe, expect, it } from 'vitest'
import { installSiteListNavigation, readSiteListState, saveSiteListState } from './siteListState'
afterEach(() => sessionStorage.clear())
describe('website list navigation memory', () => {
  it('restores filters, page, view and scroll when returning from website details', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/sites', component: {} }, { path: '/sites/:id/overview', component: {} }] })
    installSiteListNavigation(router)
    await router.push('/sites?q=sample&folder=tools&type=shop&view=cards&page=3')
    Object.defineProperty(window, 'scrollY', { value: 420, configurable: true })
    await router.push('/sites/example/overview')
    expect(readSiteListState()).toEqual({ fullPath: '/sites?q=sample&folder=tools&type=shop&view=cards&page=3', scrollTop: 420 })
    await router.push('/sites')
    expect(router.currentRoute.value.query).toMatchObject({ q: 'sample', view: 'cards', page: '3' })
  })
  it('lets explicit filter links override previous state and rejects invalid stored routes', async () => {
    saveSiteListState('/sites?view=cards', 500)
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/sites', component: {} }] })
    installSiteListNavigation(router); await router.push('/sites?type=new')
    expect(router.currentRoute.value.query).toEqual({ type: 'new' })
    saveSiteListState('https://external.test', 0)
    expect(readSiteListState()).toBeNull()
  })
})
