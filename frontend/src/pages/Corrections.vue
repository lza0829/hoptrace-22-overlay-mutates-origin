<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const report = ref(null)
const busy = ref(false)
const err = ref('')

async function load() {
  rows.value = await api.corrections()
}

async function runFixture() {
  busy.value = true
  err.value = ''
  try {
    report.value = await api.runCorrectionFixture()
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

onMounted(load)

const events = computed(() =>
  rows.value.flatMap((p) =>
    p.events.map((e) => ({
      ...e,
      overlay_id: p.overlay_id,
      overlay_status: p.status,
      waybill_id: p.waybill_id,
      hop_id: p.hop?.id,
    })),
  ),
)

const fmt = (v) => (v ? `${v.eff_event_code}@${v.eff_location_code}` : '—')
</script>

<template>
  <div class="page-card wide">
    <h1>更正覆盖</h1>
    <p class="lead">
      原点不可变，更正写入覆盖层；竖轴与投影读「原点+有效覆盖」合成视图。
      夹具先落错点（H-7 录成 EXCEPTION@HZ-STATION）再更正对照。
    </p>
    <button class="btn" :disabled="busy" @click="runFixture">
      {{ busy ? '执行中…' : '跑更正对照夹具' }}
    </button>
    <p v-if="err" class="errmsg">{{ err }}</p>
    <pre v-if="report" class="report">{{ report }}</pre>

    <h2 class="sub">更正证明（运单 · 目标 hop · 覆盖前后 · 投影前后）</h2>
    <table class="table">
      <thead>
        <tr>
          <th>时间</th><th>覆盖</th><th>运单</th><th>hop</th><th>动作</th>
          <th>覆盖前</th><th>覆盖后</th><th>投影前 → 后</th><th>原因</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="e in events" :key="e.id">
          <td>{{ e.created_at }}</td>
          <td>{{ e.overlay_id }}<br /><span class="muted">{{ e.overlay_status }}</span></td>
          <td>{{ e.waybill_id }}</td>
          <td>{{ e.hop_id }}</td>
          <td>{{ e.action === 'apply' ? '更正' : '撤销' }}</td>
          <td>{{ fmt(e.before) }}</td>
          <td>{{ fmt(e.after) }}</td>
          <td>{{ e.proj_before }} → {{ e.proj_after }}</td>
          <td>{{ e.reason }}</td>
        </tr>
        <tr v-if="!events.length"><td colspan="9" class="muted">暂无更正记录，可先跑对照夹具。</td></tr>
      </tbody>
    </table>
  </div>
</template>
