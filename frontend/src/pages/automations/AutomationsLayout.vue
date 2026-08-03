<script setup lang="ts">
import PageHeader from '@/components/PageHeader.vue'
</script>
<template><PageHeader title="自动化" description="管理签到、商品同步等后台任务，并检查每次运行。" /><nav class="tabs automation-tabs"><RouterLink to="/automations/jobs">任务</RouterLink><RouterLink to="/automations/runs">运行记录</RouterLink></nav><RouterView /></template>
<style scoped>.automation-tabs { margin-bottom: 18px; }</style>
