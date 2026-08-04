<script setup lang="ts">
import PageHeader from '@/components/PageHeader.vue'
</script>
<template><PageHeader title="设置" description="管理 SiteHub 的偏好、调度、网站分类、通知、适配器与本地数据。" /><div class="settings-layout"><nav class="section-tabs" aria-label="设置分类"><RouterLink to="/settings/general">通用</RouterLink><RouterLink to="/settings/schedules">调度</RouterLink><RouterLink to="/settings/site-types">网站类型</RouterLink><RouterLink to="/settings/tags">目录与标签</RouterLink><RouterLink to="/settings/notifications">通知</RouterLink><RouterLink to="/settings/adapters">适配器</RouterLink><RouterLink to="/settings/data">数据</RouterLink></nav><div class="settings-content"><RouterView /></div></div></template>
