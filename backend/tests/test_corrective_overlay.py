import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, seed
from app.main import app
from app.modules import corrective_overlay as co

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hoptrace.db")
    monkeypatch.setattr(db, "FIXTURES_DIR", FIXTURES)
    monkeypatch.setattr(seed, "FIXTURES_DIR", FIXTURES)
    seed.init_db()
    with TestClient(app) as c:
        yield c


def raw_hop(hop_id):
    conn = db.connect()
    try:
        return dict(conn.execute("SELECT * FROM hops WHERE id=?", (hop_id,)).fetchone())
    finally:
        conn.close()


def counts():
    conn = db.connect()
    try:
        ov = conn.execute("SELECT COUNT(*) c FROM hop_overlays").fetchone()["c"]
        ev = conn.execute("SELECT COUNT(*) c FROM correction_events").fetchone()["c"]
        return ov, ev
    finally:
        conn.close()


def test_composed_view_defaults_to_origin(client):
    hops = client.get("/api/waybills/WB-1001/timeline").json()["hops"]
    assert len(hops) == 3
    for h in hops:
        assert h["corrected"] is False
        assert h["eff_location_code"] == h["location_code"]
        assert h["eff_event_code"] == h["event_code"]


def test_apply_correction_keeps_origin_immutable(client):
    before = raw_hop("H-2")
    r = client.post(
        "/api/waybills/WB-1001/hops/H-2/corrections",
        json={"new_location_code": "NJ-HUB-2", "reason": "站点录错"},
    )
    assert r.status_code == 200, r.text
    proof = r.json()
    assert proof["hop"]["location_code"] == "NJ-HUB"
    assert proof["overlay"]["new_location_code"] == "NJ-HUB-2"
    assert proof["events"][0]["action"] == "apply"
    assert proof["events"][0]["before"]["corrected"] is False
    assert proof["events"][0]["after"]["eff_location_code"] == "NJ-HUB-2"

    # 原点行未被 UPDATE/DELETE
    after = raw_hop("H-2")
    assert after == before

    # 合成视图：eff 变、原点透出
    hops = client.get("/api/waybills/WB-1001/timeline").json()["hops"]
    h2 = next(h for h in hops if h["id"] == "H-2")
    assert h2["corrected"] is True
    assert h2["location_code"] == "NJ-HUB"
    assert h2["eff_location_code"] == "NJ-HUB-2"


def test_projection_follows_overlay_and_consistency(client):
    r = client.post(
        "/api/waybills/WB-1001/hops/H-3/corrections",
        json={"new_event_code": "DELIVERED", "reason": "事件码录错"},
    )
    assert r.status_code == 200, r.text
    ev = r.json()["events"][0]
    assert (ev["proj_before"], ev["proj_after"]) == ("到站", "已签收")

    detail = client.get("/api/waybills/WB-1001/timeline").json()
    assert detail["waybill"]["status_proj"] == "已签收"
    assert detail["consistency"]["consistent"] is True
    assert detail["consistency"]["expected_proj"] == "已签收"
    assert detail["proof"]["mapped_code"] == "DELIVERED"


def test_revoke_returns_composed_view_to_origin(client):
    client.post(
        "/api/waybills/WB-1001/hops/H-3/corrections",
        json={"new_event_code": "DELIVERED", "reason": "事件码录错"},
    )
    r = client.post(
        "/api/waybills/WB-1001/hops/H-3/corrections/revoke",
        json={"reason": "误操作撤销"},
    )
    assert r.status_code == 200, r.text
    proof = r.json()
    assert proof["status"] == "revoked"
    assert [e["action"] for e in proof["events"]] == ["apply", "revoke"]
    assert proof["events"][1]["after"]["corrected"] is False
    assert (proof["events"][1]["proj_before"], proof["events"][1]["proj_after"]) == ("已签收", "到站")

    detail = client.get("/api/waybills/WB-1001/timeline").json()
    h3 = next(h for h in detail["hops"] if h["id"] == "H-3")
    assert h3["corrected"] is False
    assert h3["eff_event_code"] == "ARRIVED_HUB"
    assert detail["waybill"]["status_proj"] == "到站"
    assert detail["consistency"]["consistent"] is True


def test_recorrection_supersedes_and_bumps_seq(client):
    client.post(
        "/api/waybills/WB-1001/hops/H-2/corrections",
        json={"new_location_code": "NJ-HUB-2", "reason": "第一次"},
    )
    r = client.post(
        "/api/waybills/WB-1001/hops/H-2/corrections",
        json={"new_location_code": "NJ-HUB-9", "reason": "第二次"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["seq"] == 2

    conn = db.connect()
    try:
        eff = conn.execute(
            "SELECT COUNT(*) c FROM hop_overlays WHERE hop_id='H-2' AND status='effective'"
        ).fetchone()["c"]
        sup = conn.execute(
            "SELECT COUNT(*) c FROM hop_overlays WHERE hop_id='H-2' AND status='superseded'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert (eff, sup) == (1, 1)

    hops = client.get("/api/waybills/WB-1001/timeline").json()["hops"]
    h2 = next(h for h in hops if h["id"] == "H-2")
    assert h2["eff_location_code"] == "NJ-HUB-9"


def test_fail_closed_validation_writes_nothing(client):
    ov0, ev0 = counts()
    proj0 = client.get("/api/waybills/WB-1001/timeline").json()["waybill"]["status_proj"]

    bad = [
        ({"new_event_code": "NOPE", "reason": "x"}, 422),          # 事件码不在契约
        ({"reason": "x"}, 422),                                    # 无更正值
        ({"new_location_code": "  ", "reason": "x"}, 422),         # 空白更正值
        ({"new_location_code": "NJ-HUB", "reason": "x"}, 409),     # 与有效值相同
        ({"new_location_code": "NJ-HUB-2"}, 422),                  # 缺原因
    ]
    for body, code in bad:
        r = client.post("/api/waybills/WB-1001/hops/H-2/corrections", json=body)
        assert r.status_code == code, (body, r.status_code, r.text)

    # 不存在的 hop / 运单
    assert client.post(
        "/api/waybills/WB-1001/hops/H-404/corrections",
        json={"new_location_code": "X", "reason": "x"},
    ).status_code == 404
    # 无覆盖可撤销
    assert client.post(
        "/api/waybills/WB-1001/hops/H-2/corrections/revoke", json={}
    ).status_code == 409

    assert counts() == (ov0, ev0)
    proj1 = client.get("/api/waybills/WB-1001/timeline").json()["waybill"]["status_proj"]
    assert proj1 == proj0


def test_proof_endpoint_readonly(client):
    r = client.post(
        "/api/waybills/WB-1001/hops/H-3/corrections",
        json={"new_event_code": "DELIVERED", "reason": "事件码录错"},
    )
    overlay_id = r.json()["overlay_id"]

    proof = client.get(f"/api/corrections/{overlay_id}").json()
    assert proof["waybill_id"] == "WB-1001"
    assert proof["hop"]["id"] == "H-3"
    assert proof["hop"]["event_code"] == "ARRIVED_HUB"  # 原点
    assert proof["overlay"]["new_event_code"] == "DELIVERED"
    ev = proof["events"][0]
    assert ev["before"]["eff_event_code"] == "ARRIVED_HUB"
    assert ev["after"]["eff_event_code"] == "DELIVERED"
    assert (ev["proj_before"], ev["proj_after"]) == ("到站", "已签收")

    lst = client.get("/api/waybills/WB-1001/corrections").json()
    assert [p["overlay_id"] for p in lst] == [overlay_id]
    assert client.get("/api/corrections/OV-NONE").status_code == 404


def test_fixture_lands_wrong_point_then_corrects(client):
    r = client.post("/api/corrections/run-fixture")
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["ok"] is True
    assert report["landed"] is True
    assert report["corrected"] is True
    assert report["proj_after_landing"] == "异常"  # 错点先反映到投影

    detail = client.get("/api/waybills/WB-1001/timeline").json()
    h7 = next(h for h in detail["hops"] if h["id"] == "H-7")
    assert h7["corrected"] is True
    assert (h7["event_code"], h7["location_code"]) == ("EXCEPTION", "HZ-STATION")  # 原点保留
    assert (h7["eff_event_code"], h7["eff_location_code"]) == ("DELIVERED", "HZ-STATION-3")
    assert detail["waybill"]["status_proj"] == "已签收"
    assert detail["consistency"]["consistent"] is True

    # 重跑幂等：不再落点、不再覆盖
    r2 = client.post("/api/corrections/run-fixture").json()
    assert r2["landed"] is False
    assert r2["corrected"] is False
    assert r2["skipped"]


def test_ingest_after_correction_stays_consistent(client):
    # 先更正（夹具落 H-7 错点并覆盖为 DELIVERED），再跑基座装载：
    # 装载的投影重写必须走合成视图，不得把投影踩回原点语义。
    client.post("/api/corrections/run-fixture")
    r = client.post("/api/ingest/run")
    assert r.json()["ok"] is True

    detail = client.get("/api/waybills/WB-1001/timeline").json()
    assert detail["waybill"]["status_proj"] == "已签收"
    assert detail["consistency"]["consistent"] is True
    h7 = next(h for h in detail["hops"] if h["id"] == "H-7")
    assert h7["corrected"] is True
    assert h7["eff_event_code"] == "DELIVERED"


# ------------------------------------------------------------ 撕裂核证（基线所无）

def test_attestation_bundle_links_origin_overlay_projection_and_revoke(client):
    r = client.post(
        "/api/waybills/WB-1001/hops/H-3/corrections",
        json={"new_event_code": "DELIVERED", "reason": "事件码录错"},
    )
    overlay_id = r.json()["overlay_id"]

    bundle = client.get(f"/api/corrections/{overlay_id}/attestation").json()
    # 四要素齐：事实点 id、覆盖 id、合成投影、撤销事件（此时为空）
    assert bundle["hop_id"] == "H-3"
    assert bundle["overlay_id"] == overlay_id
    assert bundle["overlay_status"] == "effective"
    assert bundle["overlay_queryable"] is True
    assert bundle["torn"] is False
    assert bundle["tear_reasons"] == []
    assert bundle["revoke_events"] == []
    assert len(bundle["attestations"]) == 1
    att = bundle["attestations"][0]
    assert att["action"] == "apply"
    assert att["hop_id"] == "H-3"
    assert att["overlay_id"] == overlay_id
    assert att["composed"]["eff_event_code"] == "DELIVERED"
    assert att["composed"]["origin_event_code"] == "ARRIVED_HUB"
    assert att["status_proj"] == "已签收"
    assert att["origin_fingerprint"] == bundle["origin_fingerprint"]
    # 投影与合成轴一致
    assert bundle["consistency"]["consistent"] is True

    # 撤销后核证包多一条 revoke 核证与撤销事件，合成投影回到原点语义
    client.post(
        "/api/waybills/WB-1001/hops/H-3/corrections/revoke",
        json={"reason": "误操作撤销"},
    )
    bundle2 = client.get(f"/api/corrections/{overlay_id}/attestation").json()
    assert bundle2["torn"] is False
    assert bundle2["overlay_status"] == "revoked"
    assert [a["action"] for a in bundle2["attestations"]] == ["apply", "revoke"]
    assert len(bundle2["revoke_events"]) == 1
    assert bundle2["attestations"][-1]["composed"]["eff_event_code"] == "ARRIVED_HUB"
    assert bundle2["attestations"][-1]["status_proj"] == "到站"


def test_attestation_list_endpoint_and_404(client):
    assert client.get("/api/corrections/attestations").json() == []
    r = client.post(
        "/api/waybills/WB-1001/hops/H-2/corrections",
        json={"new_location_code": "NJ-HUB-2", "reason": "站点录错"},
    )
    overlay_id = r.json()["overlay_id"]

    rows = client.get("/api/corrections/attestations").json()
    assert len(rows) == 1
    assert rows[0]["overlay_id"] == overlay_id
    assert rows[0]["hop_id"] == "H-2"

    rows = client.get("/api/corrections/attestations?waybill_id=WB-1001").json()
    assert len(rows) == 1
    assert client.get("/api/corrections/attestations?waybill_id=WB-404").json() == []
    assert client.get("/api/corrections/OV-NONE/attestation").status_code == 404


# ------------------------------------------------------------ 事实点不可变（两路径）

def test_origin_immutable_through_apply_and_revoke(client):
    before = raw_hop("H-3")
    client.post(
        "/api/waybills/WB-1001/hops/H-3/corrections",
        json={"new_event_code": "DELIVERED", "reason": "事件码录错"},
    )
    applied = raw_hop("H-3")
    assert applied == before  # 提交更正后事实点字段不变

    client.post(
        "/api/waybills/WB-1001/hops/H-3/corrections/revoke",
        json={"reason": "误操作撤销"},
    )
    revoked = raw_hop("H-3")
    assert revoked == before  # 撤销后事实点字段仍不变，且事实点行从未被删除
    assert revoked["event_code"] == "ARRIVED_HUB"

    conn = db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM hops WHERE id='H-3'").fetchone()["c"]
        assert n == 1
    finally:
        conn.close()


def test_db_triggers_block_in_place_mutation_and_delete(client):
    # DB 层兜底：任何绕过覆盖层的事实点就地改/删都被触发器 ABORT
    conn = db.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="不可就地修改"):
            conn.execute("UPDATE hops SET event_code='DELIVERED' WHERE id='H-3'")
        with pytest.raises(sqlite3.IntegrityError, match="不可删除"):
            conn.execute("DELETE FROM hops WHERE id='H-3'")
        conn.rollback()
    finally:
        conn.close()

    # 触发器拒绝后事实点原样可读
    assert raw_hop("H-3")["event_code"] == "ARRIVED_HUB"


def test_attestation_write_failure_rolls_back_whole_correction(client):
    # 失败闭：核证写入失败时事务必须整体回滚，不留无核证的覆盖。
    ov0, ev0 = counts()
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(co.CorrectionError):
            co._write_attestation(
                conn, overlay_id="OV-GHOST", waybill_id="WB-1001", hop_id="H-404",
                action="apply", event_id=None,
                composed_hop={"id": "H-404"}, proj_value=None,
            )
        conn.rollback()
    finally:
        conn.close()
    assert counts() == (ov0, ev0)

    # 端到端：正常更正路径四件齐写（覆盖 + 投影证明 + 更正事件 + 撕裂核证），同成败。
    r = client.post(
        "/api/waybills/WB-1001/hops/H-2/corrections",
        json={"new_location_code": "NJ-HUB-2", "reason": "站点录错"},
    )
    assert r.status_code == 200
    overlay_id = r.json()["overlay_id"]
    conn = db.connect()
    try:
        n_ov = conn.execute(
            "SELECT COUNT(*) c FROM hop_overlays WHERE id=? AND status='effective'",
            (overlay_id,),
        ).fetchone()["c"]
        n_ev = conn.execute(
            "SELECT COUNT(*) c FROM correction_events WHERE overlay_id=?", (overlay_id,)
        ).fetchone()["c"]
        n_at = conn.execute(
            "SELECT COUNT(*) c FROM correction_attestations WHERE overlay_id=?", (overlay_id,)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert (n_ov, n_ev, n_at) == (1, 1, 1)


# ------------------------------------------------------------ 撕裂检测（只读核证）

def test_attestation_flags_torn_when_origin_silently_mutated(client):
    client.post(
        "/api/waybills/WB-1001/hops/H-3/corrections",
        json={"new_event_code": "DELIVERED", "reason": "事件码录错"},
    )
    overlay_id = client.get("/api/corrections/attestations").json()[0]["overlay_id"]

    # 模拟绕过应用的库内篡改：临时摘掉触发器、改事实点、恢复触发器。
    conn = db.connect()
    try:
        conn.execute("DROP TRIGGER trg_hops_no_business_update")
        conn.execute("UPDATE hops SET event_code='PICKED' WHERE id='H-3'")
        conn.execute(
            """
            CREATE TRIGGER trg_hops_no_business_update
            BEFORE UPDATE OF id, waybill_id, event_ts, location_code, event_code, seq, channel ON hops
            BEGIN
              SELECT RAISE(ABORT, '事实点业务字段不可就地修改：更正只能经覆盖层');
            END
            """
        )
        conn.commit()
    finally:
        conn.close()

    bundle = client.get(f"/api/corrections/{overlay_id}/attestation").json()
    assert bundle["torn"] is True
    assert any("指纹变化" in reason for reason in bundle["tear_reasons"])


def test_attestation_flags_torn_when_origin_deleted(client):
    client.post(
        "/api/waybills/WB-1001/hops/H-2/corrections",
        json={"new_location_code": "NJ-HUB-2", "reason": "站点录错"},
    )
    overlay_id = client.get("/api/corrections/attestations").json()[0]["overlay_id"]

    conn = db.connect()
    try:
        # 模拟库级篡改：独立连接关闭外键与删除触发器后直接删事实点。
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TRIGGER trg_hops_no_delete")
        conn.execute("DELETE FROM hops WHERE id='H-2'")
        conn.commit()
    finally:
        conn.close()

    bundle = client.get(f"/api/corrections/{overlay_id}/attestation").json()
    assert bundle["torn"] is True
    assert any("事实点已缺失" in reason for reason in bundle["tear_reasons"])


# ------------------------------------------------------------ 本地夹具：两路径 + SQLite

def test_fixture_covers_apply_and_revoke_with_origin_unchanged(client):
    r = client.post("/api/corrections/run-fixture", json={"revoke": True})
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["ok"] is True
    assert report["landed"] is True
    assert report["corrected"] is True
    assert report["revoked"] is True
    assert report["origin_unchanged"] is True
    assert report["origin_fingerprint"] == report["origin_fingerprint_after"]

    # 撤销后合成视图回到事实点，投影回到错点语义（异常），原点字段始终是错点原值
    detail = client.get("/api/waybills/WB-1001/timeline").json()
    h7 = next(h for h in detail["hops"] if h["id"] == "H-7")
    assert h7["corrected"] is False
    assert (h7["event_code"], h7["location_code"]) == ("EXCEPTION", "HZ-STATION")
    assert (h7["eff_event_code"], h7["eff_location_code"]) == ("EXCEPTION", "HZ-STATION")
    assert detail["waybill"]["status_proj"] == "异常"

    overlay_id = report["proof"]["overlay_id"]
    bundle = client.get(f"/api/corrections/{overlay_id}/attestation").json()
    assert [a["action"] for a in bundle["attestations"]] == ["apply", "revoke"]
    assert len(bundle["revoke_events"]) == 1
    assert bundle["torn"] is False

    # 重跑撤销夹具幂等：不再落点、不再覆盖，撤销阶段跳过
    again = client.post("/api/corrections/run-fixture", json={"revoke": True}).json()
    assert again["landed"] is False
    assert again["corrected"] is False
    assert again["revoked"] is False
    assert again["origin_unchanged"] is True
    # 核证不重复追加
    bundle2 = client.get(f"/api/corrections/{overlay_id}/attestation").json()
    assert [a["action"] for a in bundle2["attestations"]] == ["apply", "revoke"]


def test_overview_exposes_attestation_count(client):
    ov = client.get("/api/overview").json()
    assert ov["correction_attestations"] == 0
    client.post(
        "/api/waybills/WB-1001/hops/H-2/corrections",
        json={"new_location_code": "NJ-HUB-2", "reason": "站点录错"},
    )
    assert client.get("/api/overview").json()["correction_attestations"] == 1
