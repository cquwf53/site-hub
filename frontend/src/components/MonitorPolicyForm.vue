<script setup lang="ts">
import { onMounted, ref } from 'vue'
import AsyncState from './AsyncState.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { MonitorPolicy } from '@/api/types'
import { useAppStore } from '@/stores/app'
const props = defineProps<{ siteId: string }>()
const app = useAppStore()
const loading = ref(true)
const error = ref('')
const saving = ref(false)
const policy = ref<MonitorPolicy>({ watched_only: false, price_drop_amount: 0, price_drop_percent: 0, stock_confirmations: 1, notification_cooldown_minutes: 60, catalog_drop_threshold_percent: 70, catalog_minimum_size: 10 })
async function load() {
  loading.value = true; error.value = ''
  try { policy.value = await api.get<MonitorPolicy>(`/sites/${props.siteId}/monitor-policy`) }
  catch (value) { error.value = apiErrorMessage(value) }
  finally { loading.value = false }
}
async function save() {
  saving.value = true
  try { policy.value = await api.put<MonitorPolicy>(`/sites/${props.siteId}/monitor-policy`, policy.value); app.toast('商品提醒规则已保存', 'success') }
  catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { saving.value = false }
}
onMounted(load)
</script>
<template>
  <section class="card">
    <div class="card-header"><div><h2>商品提醒与采集保护</h2><p class="subtle-text">同次同步的商品变化合并发送；单品阈值可在商品详情覆盖。</p></div></div>
    <AsyncState :loading="loading" :error="error" @retry="load">
      <form class="card-body form-grid" @submit.prevent="save">
        <label class="span-2"><input v-model="policy.watched_only" type="checkbox" /> 只提醒重点关注商品</label>
        <div class="field"><label for="policy-amount">最低降价金额</label><input id="policy-amount" v-model.number="policy.price_drop_amount" type="number" min="0" step="0.01" required /></div>
        <div class="field"><label for="policy-percent">最低降价比例（%）</label><input id="policy-percent" v-model.number="policy.price_drop_percent" type="number" min="0" max="100" step="0.1" required /></div>
        <p class="field-help span-2">0 表示不限制；同时设置金额和比例时，须满足两项阈值才提醒。</p>
        <div class="field"><label for="policy-stock">库存变化连续确认次数</label><input id="policy-stock" v-model.number="policy.stock_confirmations" type="number" min="1" max="5" required /><p class="field-help">连续多次抓取到新状态后再确认，减少短暂抖动。</p></div>
        <div class="field"><label for="policy-cooldown">重复提醒间隔（分钟）</label><input id="policy-cooldown" v-model.number="policy.notification_cooldown_minutes" type="number" min="0" max="10080" required /><p class="field-help">0 表示不限制重复提醒。</p></div>
        <div class="field"><label for="policy-drop">目录异常缩水比例（%）</label><input id="policy-drop" v-model.number="policy.catalog_drop_threshold_percent" type="number" min="1" max="100" required /></div>
        <div class="field"><label for="policy-minimum">启动缩水保护的最小商品数</label><input id="policy-minimum" v-model.number="policy.catalog_minimum_size" type="number" min="1" max="10000" required /></div>
        <p class="field-help span-2">商品数异常下降时保留原目录，在任务结果与收件箱提示检查抓取结果。</p>
        <div class="span-2"><button class="button" :disabled="saving">{{ saving ? '保存中…' : '保存提醒规则' }}</button></div>
      </form>
    </AsyncState>
  </section>
</template>
