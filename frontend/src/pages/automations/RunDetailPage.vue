<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AsyncState from '@/components/AsyncState.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { Run } from '@/api/types'
import { formatDate, formatDuration } from '@/utils/format'
import { runGuidance } from '@/utils/runGuidance'
const route = useRoute(); const run = ref<Run | null>(null); const loading = ref(true); const error = ref('')
async function load() { loading.value = true; error.value = ''; try { run.value = await api.get<Run>(`/runs/${String(route.params.runId)}`) } catch (value) { error.value = apiErrorMessage(value) } finally { loading.value = false } }
watch(() => route.params.runId, load, { immediate: true })
</script>
<template><div class="detail-back"><RouterLink class="text-button" to="/automations/runs">← 返回运行记录</RouterLink></div><AsyncState :loading="loading" :error="error" @retry="load"><template v-if="run"><div class="row-between run-head"><div><h2>{{ run.site_name || run.kind || '运行详情' }}</h2><p>{{ run.account_label || run.kind }}</p></div><StatusBadge :status="run.status" /></div><div class="grid three run-metrics"><article class="card card-body"><span>开始时间</span><strong>{{ formatDate(run.started_at) }}</strong></article><article class="card card-body"><span>运行耗时</span><strong>{{ formatDuration(run.duration_ms) }}</strong></article><article class="card card-body"><span>原因代码</span><strong>{{ run.reason_code || '—' }}</strong></article></div><section class="card"><div class="card-header"><h2>运行说明</h2></div><div class="card-body"><div v-if="runGuidance(run.status, run.reason_code)" class="card card-body"><strong>下一步</strong><p>{{ runGuidance(run.status, run.reason_code) }}</p><RouterLink v-if="run.site_id" class="text-button" :to="`/sites/${run.site_id}/${run.kind?.includes('checkin') ? 'accounts' : 'settings'}`">打开网站配置</RouterLink></div><p v-if="run.result_json?.catalog_anomaly" role="alert">商品目录异常缩水，已保留原目录。请检查抓取器和站点页面，确认目录完整后再同步。</p><p class="run-message">{{ run.message || '本次运行没有附加说明。' }}</p><pre v-if="run.result_json">{{ JSON.stringify(run.result_json, null, 2) }}</pre></div></section></template></AsyncState></template>
<style scoped>.detail-back { margin-bottom: 17px; }.run-head { margin-bottom: 15px; }.run-head h2 { margin-bottom: 3px; }.run-head p { margin: 0; }.run-metrics { margin-bottom: 16px; }.run-metrics article { display: flex; flex-direction: column; gap: 7px; }.run-metrics span { color: var(--muted); font-size: 11px; }.run-message { white-space: pre-wrap; }pre { overflow: auto; margin: 18px 0 0; padding: 15px; border-radius: 9px; background: #f4f3f0; color: #46484f; font: 12px/1.55 Consolas, monospace; }</style>
