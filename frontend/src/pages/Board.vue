<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api.js'

const waybills = ref([])
const selected = ref(null)
const detail = ref(null)
const overview = ref(null)
const err = ref('')

const pickedHop = ref(null)
const form = ref({ new_location_code: '', new_event_code: '', reason: '' })
const busy = ref(false)
const opMsg = ref('')
const opErr = ref('')
const attestation = ref(null)

async function loadList() {
  overview.value = await api.overview()
  waybills.value = await api.waybills()
  if (!selected.value && waybills.value.length) {
    selected.value = waybills.value[0].id
  }
}

async function loadDetail(id) {
  if (!id) return
  detail.value = await api.timeline(id)
  if (pickedHop.value) {
    pickedHop.value = (detail.value.hops || []).find((h) => h.id === pickedHop.value.id) || null
    if (pickedHop.value) prefill(pickedHop.value)
  }
}

onMounted(async () => {
  try {
    await loadList()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

watch(selected, (id) => {
  pickedHop.value = null
  attestation.value = null
  opMsg.value = ''
  opErr.value = ''
  loadDetail(id).catch((e) => { err.value = String(e.message || e) })
})

const hops = computed(() => detail.value?.hops || [])
const proof = computed(() => detail.value?.proof)
const consistency = computed(() => detail.value?.consistency)

function prefill(h) {
  form.value = { new_location_code: h.eff_location_code, new_event_code: h.eff_event_code, reason: '' }
}

function pick(h) {
  pickedHop.value = h
  prefill(h)
  opMsg.value = ''
  opErr.value = ''
  loadAttestation(h)
}

async function loadAttestation(h) {
  attestation.value = null
  if (!h?.overlay_id) return
  try {
    attestation.value = await api.correctionAttestation(h.overlay_id)
  } catch (e) {
    attestation.value = { torn: true, tear_reasons: [String(e.message || e)] }
  }
}

async function refresh() {
  await loadList()
  await loadDetail(selected.value)
}

async function applyCorrection() {
  if (!pickedHop.value || busy.value) return
  busy.value = true
  opMsg.value = ''
  opErr.value = ''
  try {
    const body = { reason: form.value.reason.trim() }
    const loc = form.value.new_location_code.trim()
    const code = form.value.new_event_code.trim()
    if (loc && loc !== pickedHop.value.eff_location_code) body.new_location_code = loc
    if (code && code !== pickedHop.value.eff_event_code) body.new_event_code = code
    const p = await api.applyCorrection(selected.value, pickedHop.value.id, body)
    const ev = p.events[p.events.length - 1]
    opMsg.value = `更正已生效 ${p.overlay_id} · 投影 ${ev.proj_before} → ${ev.proj_after}`
    await refresh()
    const refreshed = (detail.value.hops || []).find((h) => h.id === pickedHop.value.id)
    if (refreshed) await loadAttestation(refreshed)
  } catch (e) {
    opErr.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function revokeCorrection() {
  if (!pickedHop.value?.corrected || busy.value) return
  busy.value = true
  opMsg.value = ''
  opErr.value = ''
  try {
    const body = { reason: form.value.reason.trim() || '现场撤销更正' }
    const p = await api.revokeCorrection(selected.value, pickedHop.value.id, body)
    const ev = p.events[p.events.length - 1]
    opMsg.value = `已撤销 ${p.overlay_id} · 合成视图回到原点 · 投影 ${ev.proj_before} → ${ev.proj_after}`
    const revokedId = pickedHop.value.id
    await refresh()
    const refreshed = (detail.value.hops || []).find((h) => h.id === revokedId)
    attestation.value = null
    if (refreshed?.overlay_id) await loadAttestation(refreshed)
  } catch (e) {
    opErr.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <!-- plant-ui -->

  <div>
    <p v-if="err" class="muted">{{ err }}</p>
    <div class="stat-row" v-if="overview">
      <div class="stat"><b>{{ overview.waybills }}</b><span>运单</span></div>
      <div class="stat"><b>{{ overview.hops_primary }}</b><span>正式 hop</span></div>
      <div class="stat"><b>{{ overview.hops_sidecar }}</b><span>旁路</span></div>
      <div class="stat"><b>{{ overview.quarantine }}</b><span>隔离</span></div>
      <div class="stat"><b>{{ overview.overlays_effective }}</b><span>有效覆盖</span></div>
      <div class="stat"><b>{{ overview.correction_events }}</b><span>更正事件</span></div>
      <div class="stat"><b>{{ overview.correction_attestations ?? 0 }}</b><span>撕裂核证</span></div>
    </div>

    <div class="tri">
      <aside class="pane waybill-pane">
        <h2>运单索引</h2>
        <button
          v-for="w in waybills"
          :key="w.id"
          class="wb-item"
          :class="{ active: selected === w.id }"
          @click="selected = w.id"
        >
          <span class="id">{{ w.id }}</span>
          <span class="title">{{ w.title }}</span>
          <span class="st">{{ w.status_proj }}</span>
        </button>
      </aside>

      <section class="pane axis-pane">
        <div class="axis-head">
          <h1>{{ detail?.waybill?.title || '时间线' }}</h1>
          <span>{{ selected }}</span>
        </div>
        <div class="v-axis" v-if="hops.length">
          <div
            class="hop-node"
            v-for="h in hops"
            :key="h.id"
            :class="{ corrected: h.corrected, picked: pickedHop?.id === h.id }"
            @click="pick(h)"
          >
            <div class="ts">{{ h.event_ts }}</div>
            <div class="code">
              {{ h.eff_event_code }}
              <span v-if="h.corrected" class="badge-corr">已更正·覆盖#{{ h.overlay_seq }}</span>
            </div>
            <div class="loc">{{ h.eff_location_code }} · {{ h.id }}</div>
            <div class="orig" v-if="h.corrected">原点：{{ h.event_code }} @ {{ h.location_code }}</div>
          </div>
        </div>
        <p v-else class="muted">无正式轨迹点</p>
      </section>

      <aside class="pane inspect-pane">
        <h2>检查器</h2>
        <dl class="kv" v-if="detail?.waybill">
          <dt>运单</dt><dd>{{ detail.waybill.id }}</dd>
          <dt>投影状态</dt><dd>{{ detail.waybill.status_proj }}</dd>
          <dt>创建</dt><dd>{{ detail.waybill.created_at }}</dd>
        </dl>
        <div v-if="consistency" class="consist" :class="consistency.consistent ? 'ok' : 'bad'">
          合成轴⇄投影：{{ consistency.consistent ? '一致' : '不一致' }}
          <span class="consist-detail">
            投影 {{ consistency.proj }} / 合成轴期望 {{ consistency.expected_proj ?? '—' }}
          </span>
        </div>

        <h2>更正入口</h2>
        <div v-if="pickedHop" class="corr-panel">
          <dl class="kv">
            <dt>hop</dt><dd>{{ pickedHop.id }}</dd>
            <dt>原点</dt><dd>{{ pickedHop.event_code }} @ {{ pickedHop.location_code }}</dd>
            <dt>有效值</dt><dd>{{ pickedHop.eff_event_code }} @ {{ pickedHop.eff_location_code }}</dd>
            <template v-if="pickedHop.corrected">
              <dt>覆盖</dt><dd>{{ pickedHop.overlay_id }} · seq {{ pickedHop.overlay_seq }}</dd>
              <dt>覆盖原因</dt><dd>{{ pickedHop.overlay_reason }}</dd>
              <dt>覆盖时间</dt><dd>{{ pickedHop.overlay_at }}</dd>
            </template>
          </dl>
          <label class="fld">新地点
            <input v-model="form.new_location_code" :placeholder="pickedHop.eff_location_code" />
          </label>
          <label class="fld">新事件码
            <input v-model="form.new_event_code" :placeholder="pickedHop.eff_event_code" />
          </label>
          <label class="fld">原因（必填，留痕）
            <input v-model="form.reason" placeholder="现场更正原因" />
          </label>
          <div class="corr-actions">
            <button class="btn" :disabled="busy" @click="applyCorrection">
              {{ busy ? '提交中…' : '提交更正' }}
            </button>
            <button class="btn ghost" v-if="pickedHop.corrected" :disabled="busy" @click="revokeCorrection">
              撤销更正
            </button>
          </div>
          <p v-if="opMsg" class="okmsg">{{ opMsg }}</p>
          <p v-if="opErr" class="errmsg">{{ opErr }}</p>
        </div>
        <p v-else class="muted">点击竖轴上的 hop 节点，在此更正或撤销。</p>

        <template v-if="attestation">
          <h2>撕裂核证（只读）</h2>
          <div class="attest" :class="attestation.torn ? 'torn' : 'intact'">
            <div class="attest-state">
              {{ attestation.torn ? '已撕裂' : '未撕裂' }} ·
              覆盖{{ attestation.overlay_queryable ? '可查' : '不可查' }}
            </div>
            <dl class="kv">
              <dt>事实点</dt><dd>{{ attestation.hop_id }}</dd>
              <dt>覆盖</dt><dd>{{ attestation.overlay_id }}（{{ attestation.overlay_status }}）</dd>
              <dt>指纹</dt><dd class="fp">{{ attestation.origin_fingerprint?.slice(0, 16) }}…</dd>
              <dt>核证条数</dt><dd>{{ attestation.attestations.length }}</dd>
              <dt>撤销事件</dt><dd>{{ attestation.revoke_events.length }}</dd>
            </dl>
            <ul v-if="attestation.tear_reasons.length" class="tear-list">
              <li v-for="(reason, i) in attestation.tear_reasons" :key="i">{{ reason }}</li>
            </ul>
            <div
              v-for="a in attestation.attestations"
              :key="a.attestation_id"
              class="attest-row"
            >
              <span class="tag" :class="a.action">{{ a.action === 'apply' ? '更正' : '撤销' }}</span>
              <span>{{ a.composed.eff_event_code }}@{{ a.composed.eff_location_code }}</span>
              <span class="muted">投影 {{ a.status_proj }}</span>
            </div>
          </div>
        </template>

        <h2>状态证明</h2>
        <dl class="kv" v-if="proof">
          <dt>末 hop</dt><dd>{{ proof.last_hop_id }}</dd>
          <dt>映射码</dt><dd>{{ proof.mapped_code }}</dd>
          <dt>投影值</dt><dd>{{ proof.proj_value }}</dd>
          <dt>写入</dt><dd>{{ proof.created_at }}</dd>
        </dl>
        <p v-else class="muted">暂无证明</p>
      </aside>

      <footer class="pane dot-rail">
        <span class="label">HOP</span>
        <template v-for="(h, i) in hops" :key="h.id">
          <span class="dot on" :class="{ corr: h.corrected }"></span>
          <span v-if="i < hops.length - 1" class="dot-line"></span>
        </template>
        <template v-if="!hops.length">
          <span class="dot"></span>
          <span class="dot-line"></span>
          <span class="dot"></span>
          <span class="dot-line"></span>
          <span class="dot"></span>
        </template>
      </footer>
    </div>
  </div>
</template>
