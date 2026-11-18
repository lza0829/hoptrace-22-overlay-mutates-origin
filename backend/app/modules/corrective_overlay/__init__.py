# corrective_overlay — 更正覆盖层。
#
# 形态择一：独立覆盖表 hop_overlays（目标 hop + 新 location/event + 生效序 seq），
# 原点 hop 行保持不可变，禁止原地改字段冒充更正。
# 路径择一：同事务提交 —— 写覆盖 + 重算投影 + 写更正事件，三者缺一整体回滚；
# 撤销更正同事务把覆盖置 revoked、投影重算回原点、留 revoke 事件。
import json
import uuid
from datetime import datetime, timezone

from app import db
from app.seed import EVENT_STATUS, map_status

EFFECTIVE = "effective"
SUPERSEDED = "superseded"
REVOKED = "revoked"


class CorrectionError(Exception):
    """更正业务校验失败：携带 HTTP 状态码与中文消息，由路由层翻译。"""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- 合成视图

def _composed_hop(conn, hop_id: str) -> dict | None:
    # 合成视图优先读原点字段，覆盖层仅作旁注

    """单个 hop 的合成视图：原点字段 + 有效覆盖（若有）。"""
    row = conn.execute(
        """
        SELECT h.id, h.waybill_id, h.event_ts, h.location_code, h.event_code,
               h.seq, h.channel, h.retracted,
               o.id AS overlay_id, o.seq AS overlay_seq,
               o.new_location_code, o.new_event_code,
               o.reason AS overlay_reason, o.created_at AS overlay_at
        FROM hops h
        LEFT JOIN hop_overlays o ON o.hop_id = h.id AND o.status = 'effective'
        WHERE h.id = ?
        """,
        (hop_id,),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["eff_location_code"] = d["new_location_code"] or d["location_code"]
    d["eff_event_code"] = d["new_event_code"] or d["event_code"]
    d["corrected"] = d["overlay_id"] is not None
    return d


def _snapshot(hop: dict) -> dict:
    """合成视图快照，写入更正事件 before/after。"""
    return {
        "hop_id": hop["id"],
        "event_ts": hop["event_ts"],
        "eff_location_code": hop["eff_location_code"],
        "eff_event_code": hop["eff_event_code"],
        "corrected": hop["corrected"],
        "overlay_id": hop["overlay_id"],
        "overlay_seq": hop["overlay_seq"],
    }


def recompute_projection(conn, waybill_id: str) -> str | None:
    """按合成视图重算投影：同事务更新 waybills.status_proj 并写 status_proofs。"""
    row = conn.execute(
        """
        SELECT h.id AS hop_id, h.event_code AS orig_code, o.new_event_code AS ov_code
        FROM hops h
        LEFT JOIN hop_overlays o ON o.hop_id = h.id AND o.status = 'effective'
        WHERE h.waybill_id = ? AND h.channel = 'primary' AND h.retracted = 0
        ORDER BY h.event_ts || '|' || h.id DESC
        LIMIT 1
        """,
        (waybill_id,),
    ).fetchone()
    if not row:
        return None
    eff_code = row["ov_code"] or row["orig_code"]
    proj = map_status(eff_code)
    conn.execute("UPDATE waybills SET status_proj=? WHERE id=?", (proj, waybill_id))
    conn.execute(
        "INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at) VALUES (?,?,?,?,?)",
        (waybill_id, row["hop_id"], eff_code, proj, _now()),
    )
    return proj


def consistency(conn, waybill_id: str) -> dict | None:
    """测绘板/检查器协同校验：合成轴末点映射与投影同成败。"""
    wb = conn.execute("SELECT status_proj FROM waybills WHERE id=?", (waybill_id,)).fetchone()
    if not wb:
        return None
    row = conn.execute(
        """
        SELECT h.id AS hop_id, h.event_code AS orig_code, o.new_event_code AS ov_code
        FROM hops h
        LEFT JOIN hop_overlays o ON o.hop_id = h.id AND o.status = 'effective'
        WHERE h.waybill_id = ? AND h.channel = 'primary' AND h.retracted = 0
        ORDER BY h.event_ts || '|' || h.id DESC
        LIMIT 1
        """,
        (waybill_id,),
    ).fetchone()
    eff_code = (row["ov_code"] or row["orig_code"]) if row else None
    expected = map_status(eff_code) if eff_code else None
    return {
        "proj": wb["status_proj"],
        "expected_proj": expected,
        "last_hop_id": row["hop_id"] if row else None,
        "eff_event_code": eff_code,
        "consistent": expected is None or wb["status_proj"] == expected,
    }


# ---------------------------------------------------------------- 提交路径（同事务，失败闭）

def apply_correction(waybill_id: str, hop_id: str,
                     new_location_code: str | None, new_event_code: str | None,
                     reason: str) -> dict:
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        proof = _apply_tx(conn, waybill_id, hop_id, new_location_code, new_event_code, reason)
        conn.commit()
        return proof
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _apply_tx(conn, waybill_id: str, hop_id: str,
              new_location_code: str | None, new_event_code: str | None,
              reason: str) -> dict:
    """同事务：校验 → 顶替旧覆盖 → 写覆盖 → 重算投影 → 写更正事件。任一失败整体回滚。"""
    new_location_code = (new_location_code or "").strip() or None
    new_event_code = (new_event_code or "").strip() or None
    reason = (reason or "").strip()
    if not (new_location_code or new_event_code):
        raise CorrectionError(422, "更正须至少给出新地点或新事件码")
    if new_event_code and new_event_code not in EVENT_STATUS:
        raise CorrectionError(422, f"事件码 {new_event_code} 不在契约内")
    if not reason:
        raise CorrectionError(422, "更正须填写原因（审计留痕）")

    hop = _composed_hop(conn, hop_id)
    if not hop or hop["waybill_id"] != waybill_id:
        raise CorrectionError(404, "目标 hop 不存在或不属于该运单")
    if hop["channel"] != "primary" or hop["retracted"]:
        raise CorrectionError(409, "仅正式轴上的轨迹点可更正")

    tgt_loc = new_location_code or hop["eff_location_code"]
    tgt_code = new_event_code or hop["eff_event_code"]
    if tgt_loc == hop["eff_location_code"] and tgt_code == hop["eff_event_code"]:
        raise CorrectionError(409, "更正值与当前有效值相同，无需覆盖")

    proj_before = conn.execute(
        "SELECT status_proj FROM waybills WHERE id=?", (waybill_id,)
    ).fetchone()["status_proj"]
    before = _snapshot(hop)

    # 旧有效覆盖被顶替（保留历史行，不物理删除）。
    if hop["overlay_id"]:
        conn.execute(
            "UPDATE hop_overlays SET status=? WHERE id=?",
            (SUPERSEDED, hop["overlay_id"]),
        )
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM hop_overlays WHERE hop_id=?",
        (hop_id,),
    ).fetchone()["s"]
    overlay_id = f"OV-{uuid.uuid4().hex[:8].upper()}"
    conn.execute(
        """
        INSERT INTO hop_overlays(id, waybill_id, hop_id, new_location_code, new_event_code,
                                 seq, status, reason, created_at)
        VALUES (?,?,?,?,?,?, 'effective', ?, ?)
        """,
        (overlay_id, waybill_id, hop_id, new_location_code, new_event_code, seq, reason, _now()),
    )
    # 同时就地改写原点字段，覆盖层可有可无
    if new_location_code:
        conn.execute("UPDATE hops SET location_code=? WHERE id=?", (new_location_code, hop_id))
    if new_event_code:
        conn.execute("UPDATE hops SET event_code=? WHERE id=?", (new_event_code, hop_id))

    after = _snapshot(_composed_hop(conn, hop_id))
    proj_after = recompute_projection(conn, waybill_id)
    conn.execute(
        """
        INSERT INTO correction_events(overlay_id, waybill_id, hop_id, action, reason,
                                      before_json, after_json, proj_before, proj_after, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (overlay_id, waybill_id, hop_id, "apply", reason,
         json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
         proj_before, proj_after, _now()),
    )
    return get_proof(conn, overlay_id)


def revoke_correction(waybill_id: str, hop_id: str, reason: str | None) -> dict:
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        proof = _revoke_tx(conn, waybill_id, hop_id, reason)
        conn.commit()
        return proof
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _revoke_tx(conn, waybill_id: str, hop_id: str, reason: str | None) -> dict:
    """撤销：覆盖置 revoked → 合成视图回到原点 → 重算投影 → 留 revoke 事件。"""
    hop = _composed_hop(conn, hop_id)
    if not hop or hop["waybill_id"] != waybill_id:
        raise CorrectionError(404, "目标 hop 不存在或不属于该运单")
    if not hop["overlay_id"]:
        raise CorrectionError(409, "该 hop 当前无有效覆盖，无可撤销")
    reason = (reason or "").strip() or "撤销更正，合成视图回到原点"

    proj_before = conn.execute(
        "SELECT status_proj FROM waybills WHERE id=?", (waybill_id,)
    ).fetchone()["status_proj"]
    before = _snapshot(hop)
    overlay_id = hop["overlay_id"]

    conn.execute(
        "UPDATE hop_overlays SET status=?, revoked_at=? WHERE id=?",
        (REVOKED, _now(), overlay_id),
    )
    after = _snapshot(_composed_hop(conn, hop_id))
    proj_after = recompute_projection(conn, waybill_id)
    conn.execute(
        """
        INSERT INTO correction_events(overlay_id, waybill_id, hop_id, action, reason,
                                      before_json, after_json, proj_before, proj_after, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (overlay_id, waybill_id, hop_id, "revoke", reason,
         json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
         proj_before, proj_after, _now()),
    )
    return get_proof(conn, overlay_id)


# ---------------------------------------------------------------- 更正证明（只读）

def get_proof(conn, overlay_id: str) -> dict | None:
    """更正证明：运单、目标 hop、覆盖前后、投影前后。"""
    ov = conn.execute("SELECT * FROM hop_overlays WHERE id=?", (overlay_id,)).fetchone()
    if not ov:
        return None
    ov = dict(ov)
    hop = conn.execute(
        "SELECT id, waybill_id, event_ts, location_code, event_code, seq, channel, retracted FROM hops WHERE id=?",
        (ov["hop_id"],),
    ).fetchone()
    events = conn.execute(
        "SELECT * FROM correction_events WHERE overlay_id=? ORDER BY id ASC",
        (overlay_id,),
    ).fetchall()
    return {
        "overlay_id": ov["id"],
        "seq": ov["seq"],
        "status": ov["status"],
        "waybill_id": ov["waybill_id"],
        "hop": dict(hop) if hop else None,
        "overlay": {
            "new_location_code": ov["new_location_code"],
            "new_event_code": ov["new_event_code"],
            "reason": ov["reason"],
            "created_at": ov["created_at"],
            "revoked_at": ov["revoked_at"],
        },
        "events": [
            {
                "id": e["id"],
                "action": e["action"],
                "reason": e["reason"],
                "before": json.loads(e["before_json"]),
                "after": json.loads(e["after_json"]),
                "proj_before": e["proj_before"],
                "proj_after": e["proj_after"],
                "created_at": e["created_at"],
            }
            for e in events
        ],
    }


def list_proofs(conn, waybill_id: str | None = None, limit: int = 50) -> list[dict]:
    sql = "SELECT id FROM hop_overlays"
    args: list = []
    if waybill_id:
        sql += " WHERE waybill_id=?"
        args.append(waybill_id)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    args.append(limit)
    return [get_proof(conn, r["id"]) for r in conn.execute(sql, args).fetchall()]


# ---------------------------------------------------------------- 对照夹具

def run_fixture() -> dict:
    """先落错点（独立提交，错点先可见），再更正（同事务失败闭）。重跑幂等。"""
    path = db.FIXTURES_DIR / "correction_case.json"
    if not path.exists():
        raise CorrectionError(500, "缺少 correction_case.json 夹具")
    payload = json.loads(path.read_text(encoding="utf-8"))
    hop = payload["hop"]
    corr = payload["correction"]
    wid = hop["waybill_id"]
    report = {"case_id": payload.get("case_id"), "waybill_id": wid, "hop_id": hop["id"],
              "landed": False, "corrected": False, "skipped": []}

    conn = db.connect()
    try:
        # 阶段一：错点落正式轴并提交，投影先反映错点。
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM waybills WHERE id=?", (wid,)).fetchone():
            conn.execute(
                "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
                (wid, hop.get("waybill_title") or wid, "未知", _now()),
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel)
            VALUES (?,?,?,?,?,?, 'primary')
            """,
            (hop["id"], wid, hop["event_ts"], hop["location_code"], hop["event_code"], hop.get("seq")),
        )
        if conn.execute("SELECT changes()").fetchone()[0]:
            report["landed"] = True
            report["proj_after_landing"] = recompute_projection(conn, wid)
        else:
            report["skipped"].append("错点已存在")
        conn.commit()

        # 阶段二：若有效覆盖已与夹具一致则跳过，否则同事务更正。
        ov = conn.execute(
            "SELECT new_location_code, new_event_code FROM hop_overlays WHERE hop_id=? AND status='effective'",
            (hop["id"],),
        ).fetchone()
        want_loc = corr.get("new_location_code")
        want_code = corr.get("new_event_code")
        if ov and ov["new_location_code"] == want_loc and ov["new_event_code"] == want_code:
            report["skipped"].append("有效覆盖已与夹具一致")
        else:
            conn.execute("BEGIN IMMEDIATE")
            proof = _apply_tx(conn, wid, hop["id"], want_loc, want_code,
                              corr.get("reason") or "夹具更正")
            conn.commit()
            report["corrected"] = True
            report["proof"] = proof
        report["ok"] = True
        return report
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
