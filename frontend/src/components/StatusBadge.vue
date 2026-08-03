<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ status?: string | null; label?: string }>()
const labels: Record<string, string> = {
  ready: '已登录', succeeded: '成功', success: '成功', running: '运行中', leased: '已领取', pending: '等待中', queued: '排队中',
  failed: '失败', error: '异常', cancelled: '已取消', skipped: '已跳过', effect_unknown: '结果未知', expired: '已过期', disabled: '已停用', enabled: '已启用',
  awaiting_user: '等待登录', opening: '正在打开', verifying: '正在验证', busy: '使用中', unverified: '待验证', not_configured: '未配置', active: '正常', paused: '已暂停',
  available: '在售', sold_out: '售罄', missing: '抓取缺失', delisted: '明确下架', unknown: '未知',
}
const tone = computed(() => {
  const status = props.status || ''
  if (['ready', 'succeeded', 'success', 'active', 'enabled', 'available'].includes(status)) return 'positive'
  if (['failed', 'error', 'expired', 'effect_unknown', 'sold_out', 'delisted'].includes(status)) return 'negative'
  if (['running', 'leased', 'opening', 'verifying', 'busy'].includes(status)) return 'accent'
  if (['pending', 'queued', 'awaiting_user', 'missing'].includes(status)) return 'warning'
  return 'neutral'
})
</script>
<template><span class="status-badge" :class="tone"><i />{{ label || labels[status || ''] || status || '未知' }}</span></template>
