async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || res.statusText)
  }
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  waybills: () => req('/api/waybills'),
  timeline: (id) => req(`/api/waybills/${encodeURIComponent(id)}/timeline`),
  quarantine: () => req('/api/quarantine'),
  sidecar: () => req('/api/sidecar'),
  ingest: () => req('/api/ingest/run', { method: 'POST' }),
  proofs: (waybillId) =>
    req(waybillId ? `/api/status-proofs?waybill_id=${encodeURIComponent(waybillId)}` : '/api/status-proofs'),
  corrections: (waybillId) =>
    req(waybillId ? `/api/waybills/${encodeURIComponent(waybillId)}/corrections` : '/api/corrections'),
  correctionProof: (overlayId) => req(`/api/corrections/${encodeURIComponent(overlayId)}`),
  applyCorrection: (waybillId, hopId, body) =>
    req(`/api/waybills/${encodeURIComponent(waybillId)}/hops/${encodeURIComponent(hopId)}/corrections`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  revokeCorrection: (waybillId, hopId, body) =>
    req(`/api/waybills/${encodeURIComponent(waybillId)}/hops/${encodeURIComponent(hopId)}/corrections/revoke`, {
      method: 'POST',
      body: JSON.stringify(body || {}),
    }),
  runCorrectionFixture: () => req('/api/corrections/run-fixture', { method: 'POST' }),
}
