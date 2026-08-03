import { inject, type Ref } from 'vue'
import type { Site } from '@/api/types'

export interface SiteDetailContext { site: Ref<Site | null>; refresh: () => Promise<void> }
export const siteDetailKey = Symbol('site-detail')
export function useSiteDetail() {
  const value = inject<SiteDetailContext>(siteDetailKey)
  if (!value) throw new Error('Site detail context is missing')
  return value
}
