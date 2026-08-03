<script setup lang="ts">
import { ref } from 'vue'
import ProductList from '@/components/ProductList.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { TaskRef } from '@/api/types'
import { useAppStore } from '@/stores/app'
import { useTaskStore } from '@/stores/tasks'
import { useSiteDetail } from './siteContext'
const { site } = useSiteDetail(); const app = useAppStore(); const tasks = useTaskStore(); const syncing = ref(false)
async function sync() { if (!site.value) return; syncing.value = true; try { const task = await api.post<TaskRef>(`/sites/${site.value.id}/sync`); tasks.track(task, `同步 ${site.value.name} 商品`); app.taskDrawerOpen = true } catch (value) { app.toast(apiErrorMessage(value), 'error') } finally { syncing.value = false } }
</script>
<template><div class="row-between page-section-header"><div><h2>商品</h2><p>此网站发现并持续追踪的商品。</p></div><button class="button" :disabled="syncing" @click="sync">{{ syncing ? '正在创建…' : '立即同步' }}</button></div><ProductList v-if="site" :site-id="site.id" /></template>
<style scoped>.page-section-header { margin-bottom: 14px; }.page-section-header h2 { margin-bottom: 3px; }.page-section-header p { margin: 0; font-size: 12px; }</style>
