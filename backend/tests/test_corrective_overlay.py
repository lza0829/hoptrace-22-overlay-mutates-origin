import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, seed
from app.main import app

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
