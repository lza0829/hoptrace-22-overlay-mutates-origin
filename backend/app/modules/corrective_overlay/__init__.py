# corrective_overlay — 更正覆盖层。
#
# 形态择一：独立覆盖表 hop_overlays（目标 hop + 新 location/event + 生效序 seq），
# 原点 hop 行保持不可变，禁止原地改字段冒充更正；seed.py 触发器在 DB 层兜底。
# 路径择一：同事务提交 —— 写覆盖 + 重算合成投影 + 写更正事件 + 写撕裂核证，
# 缺一整体回滚；撤销更正同事务把覆盖置 revoked、投影重算回原点、留 revoke 事件与核证。
#
# 撕裂核证 correction_attestations（基线所无）：事实点 id × 覆盖 id × 合成投影 ×
# 撤销事件，配事实点指纹，只读接口即时比对，就地篡改/删除/覆盖不可查均判撕裂。
import hashlib
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
    """单个 hop 的合成视图：原点字段原样保留，有效值读「原点+有效覆盖」合成。"""
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


# ---------------------------------------------------------------- 更正撕裂核证

# 受保护的事实点业务字段（与 seed.py 触发器保护列保持一致）。
_ORIGIN_FIELDS = ("id", "waybill_id", "event_ts", "location_code", "event_code", "seq", "channel")


def _origin_fingerprint(conn, hop_id: str) -> str | None:
    """事实点指纹：只对不可变业务字段取哈希，任何就地篡改都会改变指纹。"""
    row = conn.execute(
        "SELECT id, waybill_id, event_ts, location_code, event_code, seq, channel FROM hops WHERE id=?",
        (hop_id,),
    ).fetchone()
    if not row:
        return None
    payload = json.dumps([row[k] for k in _ORIGIN_FIELDS], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _composed_projection(composed_hop: dict, proj_value: str | None) -> dict:
    """合成投影核证：事实点 id + 有效合成值 + 映射投影。"""
    return {
        "hop_id": composed_hop["id"],
        "waybill_id": composed_hop["waybill_id"],
        "overlay_id": composed_hop.get("overlay_id"),
        "origin_event_code": composed_hop["event_code"],
        "origin_location_code": composed_hop["location_code"],
        "eff_event_code": composed_hop["eff_event_code"],
        "eff_location_code": composed_hop["eff_location_code"],
        "corrected": composed_hop["corrected"],
        "status_proj": proj_value,
    }


def _write_attestation(conn, *, overlay_id: str, waybill_id: str, hop_id: str,
                       action: str, event_id: int, composed_hop: dict,
                       proj_value: str | None) -> dict:
    """同事务写一条撕裂核证；任一步失败由外层事务整体回滚（失败闭）。"""
    fingerprint = _origin_fingerprint(conn, hop_id)
    if fingerprint is None:
        raise CorrectionError(404, "核证失败：事实点不存在")
    composed = _composed_projection(composed_hop, proj_value)
    row = conn.execute(
        """
        INSERT INTO correction_attestations(
          attestation_id, overlay_id, waybill_id, hop_id, action, event_id,
          status_proj, composed_json, origin_fingerprint, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"AT-{uuid.uuid4().hex[:10].upper()}", overlay_id, waybill_id, hop_id, action,
            event_id, proj_value, json.dumps(composed, ensure_ascii=False), fingerprint, _now(),
        ),
    )
    return {"attestation_id": row.lastrowid}


def get_attestation_bundle(conn, overlay_id: str) -> dict | None:
    """只读核证包：事实点 id、覆盖 id、合成投影、撤销事件，并即时重算撕裂判定。"""
    ov = conn.execute("SELECT * FROM hop_overlays WHERE id=?", (overlay_id,)).fetchone()
    if not ov:
        return None
    ov = dict(ov)
    hop = conn.execute(
        "SELECT id, waybill_id, event_ts, location_code, event_code, seq, channel, retracted FROM hops WHERE id=?",
        (ov["hop_id"],),
    ).fetchone()
    rows = conn.execute(
        "SELECT * FROM correction_attestations WHERE overlay_id=? ORDER BY id ASC",
        (overlay_id,),
    ).fetchall()
    events = conn.execute(
        "SELECT id, action, created_at FROM correction_events WHERE overlay_id=? ORDER BY id ASC",
        (overlay_id,),
    ).fetchall()

    current_fingerprint = _origin_fingerprint(conn, ov["hop_id"])
    current = _composed_hop(conn, ov["hop_id"])
    consistency_view = consistency(conn, ov["waybill_id"])

    attestations = []
    tear_reasons: list[str] = []
    for r in rows:
        d = dict(r)
        d["composed"] = json.loads(d.pop("composed_json"))
        attestations.append(d)
        if current_fingerprint is None:
            tear_reasons.append(f"核证 {d['attestation_id']}：事实点已缺失（疑似删除）")
        elif d["origin_fingerprint"] != current_fingerprint:
            tear_reasons.append(f"核证 {d['attestation_id']}：事实点指纹变化（疑似就地篡改）")

    # 撕裂判定：覆盖与投影必须同查、同源、同步。
    overlay_queryable = True
    if ov["status"] == EFFECTIVE:
        if current is None or not current.get("overlay_id"):
            overlay_queryable = False
            tear_reasons.append("覆盖标记 effective 但合成视图查不到（覆盖与投影不同步）")
        if consistency_view and not consistency_view["consistent"]:
            tear_reasons.append("投影与合成轴末点不一致（投影已漂移）")
    else:
        if current is not None and current.get("overlay_id") == overlay_id:
            overlay_queryable = False
            tear_reasons.append("覆盖已失效但合成视图仍引用该覆盖")

    revoke_events = [dict(e) for e in events if e["action"] == "revoke"]
    has_revoke_attestation = any(a["action"] == "revoke" for a in attestations)
    if revoke_events and not has_revoke_attestation:
        tear_reasons.append("存在撤销事件但缺少撤销核证（事件与核证不同步）")

    return {
        "overlay_id": overlay_id,
        "overlay_status": ov["status"],
        "waybill_id": ov["waybill_id"],
        "hop_id": ov["hop_id"],
        "hop": dict(hop) if hop else None,
        "overlay_queryable": overlay_queryable,
        "origin_fingerprint": current_fingerprint,
        "consistency": consistency_view,
        "attestations": attestations,
        "revoke_events": revoke_events,
        "torn": bool(tear_reasons),
        "tear_reasons": tear_reasons,
    }


def list_attestations(conn, waybill_id: str | None = None, limit: int = 100) -> list[dict]:
    """只读核证明细列表，可按运单过滤。"""
    sql = "SELECT * FROM correction_attestations"
    args: list = []
    if waybill_id:
        sql += " WHERE waybill_id=?"
        args.append(waybill_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in conn.execute(sql, args).fetchall():
        d = dict(r)
        d["composed"] = json.loads(d.pop("composed_json"))
        out.append(d)
    return out


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
    # 事实点保持不可变：禁止任何 hops UPDATE/DELETE，更正只经 hop_overlays。
    after_hop = _composed_hop(conn, hop_id)
    after = _snapshot(after_hop)
    proj_after = recompute_projection(conn, waybill_id)
    cur = conn.execute(
        """
        INSERT INTO correction_events(overlay_id, waybill_id, hop_id, action, reason,
                                      before_json, after_json, proj_before, proj_after, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (overlay_id, waybill_id, hop_id, "apply", reason,
         json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
         proj_before, proj_after, _now()),
    )
    # 同事务写撕裂核证；失败则整体回滚，不留无核证的覆盖。
    _write_attestation(
        conn, overlay_id=overlay_id, waybill_id=waybill_id, hop_id=hop_id,
        action="apply", event_id=cur.lastrowid, composed_hop=after_hop, proj_value=proj_after,
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
    after_hop = _composed_hop(conn, hop_id)
    after = _snapshot(after_hop)
    proj_after = recompute_projection(conn, waybill_id)
    cur = conn.execute(
        """
        INSERT INTO correction_events(overlay_id, waybill_id, hop_id, action, reason,
                                      before_json, after_json, proj_before, proj_after, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (overlay_id, waybill_id, hop_id, "revoke", reason,
         json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
         proj_before, proj_after, _now()),
    )
    # 撤销同事务留核证：合成视图已回原点，指纹必须与事实点当前值一致。
    _write_attestation(
        conn, overlay_id=overlay_id, waybill_id=waybill_id, hop_id=hop_id,
        action="revoke", event_id=cur.lastrowid, composed_hop=after_hop, proj_value=proj_after,
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

def run_fixture(revoke: bool = False) -> dict:
    """先落错点（独立提交，错点先可见），再更正（同事务失败闭）；可选再撤销。

    夹具同时覆盖提交更正与撤销两路径，并在报告里回传事实点指纹前后值，
    证明两路径下事实点字段均未变。重跑幂等。
    """
    path = db.FIXTURES_DIR / "correction_case.json"
    if not path.exists():
        raise CorrectionError(500, "缺少 correction_case.json 夹具")
    payload = json.loads(path.read_text(encoding="utf-8"))
    hop = payload["hop"]
    corr = payload["correction"]
    wid = hop["waybill_id"]
    report = {"case_id": payload.get("case_id"), "waybill_id": wid, "hop_id": hop["id"],
              "landed": False, "corrected": False, "revoked": False, "skipped": []}

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
        report["origin_fingerprint"] = _origin_fingerprint(conn, hop["id"])
        conn.commit()

        # 阶段二：最近一条覆盖已与夹具目标值一致则跳过（含已撤销，保证撤销夹具可幂等重跑），
        # 否则同事务更正。
        ov = conn.execute(
            """
            SELECT id, status, new_location_code, new_event_code
            FROM hop_overlays WHERE hop_id=?
            ORDER BY seq DESC, id DESC LIMIT 1
            """,
            (hop["id"],),
        ).fetchone()
        want_loc = corr.get("new_location_code")
        want_code = corr.get("new_event_code")
        effective_overlay_id: str | None = None
        if ov and ov["new_location_code"] == want_loc and ov["new_event_code"] == want_code:
            report["skipped"].append(
                "有效覆盖已与夹具一致" if ov["status"] == EFFECTIVE else "最近覆盖已与夹具一致（曾撤销）"
            )
            effective_row = conn.execute(
                "SELECT id FROM hop_overlays WHERE hop_id=? AND status='effective'",
                (hop["id"],),
            ).fetchone()
            if effective_row:
                effective_overlay_id = effective_row["id"]
        else:
            conn.execute("BEGIN IMMEDIATE")
            proof = _apply_tx(conn, wid, hop["id"], want_loc, want_code,
                              corr.get("reason") or "夹具更正")
            conn.commit()
            report["corrected"] = True
            report["proof"] = proof
            effective_overlay_id = proof["overlay_id"]

        # 阶段三（可选）：同事务撤销，合成视图回到事实点并留撤销事件与核证。
        if revoke:
            still_effective = conn.execute(
                "SELECT id FROM hop_overlays WHERE id=? AND status='effective'",
                (effective_overlay_id,),
            ).fetchone()
            if still_effective:
                conn.execute("BEGIN IMMEDIATE")
                revoke_proof = _revoke_tx(conn, wid, hop["id"], "夹具撤销：回到事实点")
                conn.commit()
                report["revoked"] = True
                report["revoke_proof"] = revoke_proof
            else:
                report["skipped"].append("覆盖已撤销，跳过撤销阶段")

        # 两路径走完，事实点指纹必须与落点时一致。
        report["origin_fingerprint_after"] = _origin_fingerprint(conn, hop["id"])
        report["origin_unchanged"] = (
            report["origin_fingerprint"] == report["origin_fingerprint_after"]
        )
        report["ok"] = True
        return report
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
