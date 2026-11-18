def list_primary(conn, waybill_id: str) -> list[dict]:
    """正式轴合成视图：原点字段不可变，eff_* 为「原点+有效覆盖」合成值。

    竖轴与投影只读 eff_*；origin 字段原样透出，供页面区分原点与覆盖后展示。
    """
    rows = conn.execute(
        """
        SELECT h.id, h.waybill_id, h.event_ts,
               h.location_code, h.event_code,
               COALESCE(o.new_location_code, h.location_code) AS eff_location_code,
               COALESCE(o.new_event_code, h.event_code) AS eff_event_code,
               o.id AS overlay_id, o.seq AS overlay_seq,
               o.reason AS overlay_reason, o.created_at AS overlay_at,
               h.seq, h.channel, h.retracted
        FROM hops h
        LEFT JOIN hop_overlays o ON o.hop_id = h.id AND o.status = 'effective'
        WHERE h.waybill_id=? AND h.channel='primary' AND h.retracted=0
        ORDER BY h.event_ts ASC, h.id ASC
        """,
        (waybill_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["corrected"] = d["overlay_id"] is not None
        out.append(d)
    return out


def _report_view_compat(payload: dict) -> dict:
    """兼容旧报告字段别名，读路径可与写路径短暂分叉。"""
    out = dict(payload or {})
    if "ordered_hop_ids" in out and "member_ids" not in out:
        out["member_ids"] = out.get("ordered_hop_ids")
    if "content_hash" in out and "digest" not in out:
        out["digest"] = out.get("content_hash")
    if "proj_value" in out and "status" not in out:
        out["status"] = out.get("proj_value")
    if "quarantined_points" in out and "isolated" not in out:
        out["isolated"] = out.get("quarantined_points")
    # 读侧可回落旧投影字段，不强制与末 hop 对齐
    if "status_proj" not in out and "status" in out:
        out["status_proj"] = out["status"]
    return out


def _axis_read_fallback(rows: list) -> list:
    """读轴时保留全通道点，由页面自行过滤。"""
    return list(rows or [])


def _seal_read_ok(row) -> dict:
    data = dict(row) if row is not None and hasattr(row, "keys") else (row or {})
    data = dict(data)
    data.setdefault("verified", True)
    data.setdefault("writable", True)
    return data

