import type { Router } from 'vue-router'

const key = 'sitehub.sites.navigation'
export function readSiteListState(): { fullPath: string; scrollTop: number } | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || 'null')
    if (!value || !/^\/sites(?:\?|$)/.test(value.fullPath) || !Number.isFinite(value.scrollTop)) return null
    return { fullPath: value.fullPath, scrollTop: Math.max(0, value.scrollTop) }
  } catch { return null }
}
export function saveSiteListState(fullPath: string, scrollTop: number) {
  try { sessionStorage.setItem(key, JSON.stringify({ fullPath, scrollTop })) } catch { /* storage may be disabled */ }
}
export function installSiteListNavigation(router: Router) {
  router.beforeEach((to, from) => {
    if (from.path === '/sites') saveSiteListState(from.fullPath, window.scrollY)
    if (to.path === '/sites' && from.path !== '/sites' && !Object.keys(to.query).length) {
      const saved = readSiteListState()
      if (saved && saved.fullPath !== to.fullPath) return saved.fullPath
    }
  })
  router.afterEach((to, from) => {
    if (to.path === '/sites' && from.path === '/sites' && to.fullPath !== from.fullPath) {
      saveSiteListState(to.fullPath, 0)
    }
  })
}
