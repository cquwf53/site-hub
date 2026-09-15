<script setup lang="ts">
import { onMounted, ref } from 'vue'
import AsyncState from './AsyncState.vue'
import { api, apiErrorMessage } from '@/api/client'
import type { ProductWatchRule } from '@/api/types'
import { useAppStore } from '@/stores/app'
const props = defineProps<{ productId: string }>()
const emit = defineEmits<{ updated: [rule: ProductWatchRule] }>()
const app = useAppStore()
const rule = ref<ProductWatchRule>({ watched: false, price_drop_amount: null, price_drop_percent: null })
const loading = ref(true); const error = ref(''); const saving = ref(false)
async function load() {
  loading.value = true; error.value = ''
  try { rule.value = await api.get<ProductWatchRule>(`/products/${props.productId}/watch-rule`) }
  catch (value) { error.value = apiErrorMessage(value) }
  finally { loading.value = false }
}
async function save() {
  saving.value = true
  try {
    rule.value = await api.put<ProductWatchRule>(`/products/${props.productId}/watch-rule`, rule.value)
    emit('updated', rule.value); app.toast('商品关注规则已保存', 'success')
  } catch (value) { app.toast(apiErrorMessage(value), 'error') }
  finally { saving.value = false }
}
function numberValue(event: Event) { const value = (event.target as HTMLInputElement).value; return value === '' ? null : Number(value) }
onMounted(load)
</script>
<template>
  <section class="detail-section">
    <h4>重点关注与降价提醒</h4>
    <AsyncState :loading="loading" :error="error" @retry="load">
      <form class="form-grid" @submit.prevent="save">
        <label class="span-2"><input v-model="rule.watched" type="checkbox" /> 重点关注此商品</label>
        <div class="field"><label for="watch-amount">最低降价金额</label><input id="watch-amount" :value="rule.price_drop_amount ?? ''" type="number" min="0" step="0.01" placeholder="继承网站规则" @input="rule.price_drop_amount = numberValue($event)" /></div>
        <div class="field"><label for="watch-percent">最低降价比例（%）</label><input id="watch-percent" :value="rule.price_drop_percent ?? ''" type="number" min="0" max="100" step="0.1" placeholder="继承网站规则" @input="rule.price_drop_percent = numberValue($event)" /></div>
        <p class="field-help span-2">留空继承网站规则；填 0 表示此项不限制。</p>
        <div class="span-2"><button class="button small" :disabled="saving">保存关注规则</button></div>
      </form>
    </AsyncState>
  </section>
</template>
