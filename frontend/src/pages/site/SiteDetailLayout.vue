<script setup lang="ts">
import { provide, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AsyncState from '@/components/AsyncState.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { Site } from '@/api/types'
import { hostname } from '@/utils/format'
import { siteTypeIcon } from '@/utils/siteType'
import { useAppStore } from '@/stores/app'
import { siteDetailKey } from './siteContext'

const route = useRoute()
const app = useAppStore()
const site = ref<Site | null>(null)
const loading = ref(true)
const error = ref('')
async function load() {
  loading.value = true; error.value = ''
  try { site.value = await api.get<Site>(`/sites/${String(route.params.siteId)}`) }
  catch (value) { error.value = apiErrorMessage(value) }
  finally { loading.value = false }
}
async function openSite() {
  if (!site.value) return
  try {
    await api.post<void>(`/sites/${site.value.id}/open`)
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
}
provide(siteDetailKey, { site, refresh: load })
watch(() => route.params.siteId, load, { immediate: true })
</script>

<template>
  <AsyncState :loading="loading" :error="error" @retry="load">
    <template v-if="site">
      <header class="site-detail-header">
        <div class="site-identity"><span class="site-icon large">{{ site.name.slice(0, 1).toUpperCase() }}</span><div><div class="row"><h1>{{ site.name }}</h1><StatusBadge :status="site.status" /><span v-if="site.site_type" class="site-type" :style="{ color: site.site_type.color || '#6e7078', borderColor: `${site.site_type.color || '#6e7078'}55`, background: `${site.site_type.color || '#6e7078'}12` }"><span v-if="site.site_type.icon">{{ siteTypeIcon(site.site_type.icon) }}</span>{{ site.site_type.name }}</span></div><p>{{ hostname(site.url) }}</p></div></div>
        <div class="page-actions"><button v-if="site.url" class="button secondary" @click="openSite">访问网站 ↗</button></div>
      </header>
      <nav class="tabs site-tabs" aria-label="网站详情">
        <RouterLink :to="`/sites/${site.id}/overview`">概览</RouterLink><RouterLink :to="`/sites/${site.id}/products`">商品</RouterLink><RouterLink :to="`/sites/${site.id}/accounts`">签到账号</RouterLink><RouterLink :to="`/sites/${site.id}/activity`">活动</RouterLink><RouterLink :to="`/sites/${site.id}/settings`">设置</RouterLink>
      </nav>
      <div class="site-detail-content"><RouterView /></div>
    </template>
  </AsyncState>
</template>

<style scoped>
.site-detail-header { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 22px; }.site-identity { display: flex; min-width: 0; align-items: center; gap: 14px; }.site-identity .row { flex-wrap: wrap; }.site-icon.large { width: 48px; height: 48px; border-radius: 11px; font-size: 19px; }.site-identity h1 { margin: 0; }.site-identity p { margin: 4px 0 0; font-size: 13px; }.site-type { display: inline-flex; min-height: 24px; align-items: center; gap: 5px; padding: 0 8px; border: 1px solid; border-radius: 7px; font-size: 11px; font-weight: 600; white-space: nowrap; }.site-tabs { margin-bottom: 22px; }.site-detail-content { min-height: 300px; }
@media (max-width: 600px) { .site-detail-header { align-items: flex-start; }.site-detail-header .button { flex: 0; font-size: 0; }.site-detail-header .button::after { content: '↗'; font-size: 14px; } }
</style>
