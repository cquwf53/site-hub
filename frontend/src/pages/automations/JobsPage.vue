<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AsyncState from '@/components/AsyncState.vue'
import PaginationBar from '@/components/PaginationBar.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { Job, Page, TaskRef } from '@/api/types'
import { useAppStore } from '@/stores/app'
import { useTaskStore } from '@/stores/tasks'
import { runGuidance } from '@/utils/runGuidance'
import { formatDate } from '@/utils/format'

const route = useRoute(); const router = useRouter(); const app = useAppStore(); const tasks = useTaskStore()
const data = ref<Page<Job>>({ items: [], page: 1, page_size: 25, total: 0 }); const loading = ref(true); const error = ref(''); const status = ref(String(route.query.status || 'active'))
let loadVersion = 0
async function load() { const version = ++loadVersion; loading.value = true; error.value = ''; try { const result = await api.get<Page<Job>>('/jobs', { page: route.query.page || 1, page_size: 25, status: status.value === 'active' ? ['queued', 'leased', 'running'] : status.value || undefined }); if (version === loadVersion) data.value = result } catch (value) { if (version === loadVersion) error.value = apiErrorMessage(value) } finally { if (version === loadVersion) loading.value = false } }
function filter() { void router.replace({ query: { status: status.value || 'all' } }) }
function trackIfTask(value: TaskRef | Job, title: string) { if ('jobId' in value) { tasks.track(value as TaskRef, title); app.taskDrawerOpen = true } }
async function action(job: Job, name: 'cancel' | 'retry') { try { const result = await api.post<TaskRef | Job>(`/jobs/${job.id}/${name}`); trackIfTask(result, `${name === 'retry' ? '重试' : '取消'}任务`); app.toast(name === 'retry' ? '重试已安排' : '任务已取消', 'success'); await load() } catch (value) { app.toast(apiErrorMessage(value), 'error') } }
watch(() => route.query, () => { status.value = route.query.status === 'all' ? '' : String(route.query.status || 'active'); void load() }, { deep: true, immediate: true })
</script>
<template>
  <div class="toolbar"><select v-model="status" class="status-select" @change="filter"><option value="active">当前任务</option><option value="">全部状态</option><option value="succeeded">成功</option><option value="failed">失败</option><option value="cancelled">已取消</option></select><span class="toolbar-note">调度由本地 Worker 执行，页面关闭后仍会继续。</span></div>
  <section class="card"><AsyncState :loading="loading" :error="error" :empty="!data.items.length" empty-title="暂无任务" empty-text="添加签到账号或启用商品追踪后，系统会自动创建任务。" @retry="load">
<div class="table-wrap"><table class="data-table"><thead><tr><th>任务</th><th>计划时间</th><th>状态</th><th>尝试</th><th>说明</th><th /></tr></thead><tbody><tr v-for="job in data.items" :key="job.id"><td><div class="job-name"><strong>{{ job.site_name || job.kind || '自动化任务' }}</strong><small>{{ job.account_label || job.kind }}</small></div></td><td>{{ formatDate(job.scheduled_for) }}</td><td><StatusBadge :status="job.status" /></td><td>{{ job.attempt ?? 0 }} / {{ job.max_attempts ?? '—' }}</td><td class="message-cell"><p v-if="job.result?.catalog_anomaly" role="alert">商品目录异常缩水，已保留原目录。请检查网站适配器后再同步。</p><span>{{ job.error_message || job.message || '—' }}</span><p v-if="runGuidance(job.status, job.error_kind)">{{ runGuidance(job.status, job.error_kind) }}</p><RouterLink v-if="job.site_id && runGuidance(job.status, job.error_kind)" class="text-button" :to="`/sites/${job.site_id}/${job.kind === 'checkin' || job.kind === 'login_session' ? 'accounts' : 'settings'}`">打开网站配置</RouterLink></td><td class="actions"><button v-if="['queued','leased','running'].includes(job.status)" class="text-button" @click="action(job, 'cancel')">取消</button><button v-if="['failed','cancelled','effect_unknown'].includes(job.status)" class="text-button" @click="action(job, 'retry')">重试</button></td></tr></tbody></table></div><PaginationBar :page="data.page" :page-size="data.page_size" :total="data.total" @change="router.replace({ query: { ...route.query, page: String($event) } })" />
  </AsyncState></section>
</template>
<style scoped>.status-select { width: 135px; }.toolbar-note { color: var(--subtle); font-size: 11px; }.job-name { display: flex; min-width: 160px; flex-direction: column; gap: 3px; }.job-name small { color: var(--muted); }.message-cell { max-width: 300px; overflow: hidden; color: var(--muted); overflow-wrap: anywhere; white-space: normal; }.message-cell p { margin: 5px 0; font-size: 11px; }</style>
