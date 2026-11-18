from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import connect
from app.modules import corrective_overlay as co

router = APIRouter(prefix="/api")


class CorrectionIn(BaseModel):
    new_location_code: str | None = None
    new_event_code: str | None = None
    reason: str = Field(min_length=1)


class RevokeIn(BaseModel):
    reason: str | None = None


def _guard(fn, *args):
    try:
        return fn(*args)
    except co.CorrectionError as e:
        raise HTTPException(e.status_code, e.message)


@router.post("/waybills/{waybill_id}/hops/{hop_id}/corrections")
def apply_correction(waybill_id: str, hop_id: str, body: CorrectionIn):
    """更正入口：同事务写覆盖+重算投影+写更正事件，失败整体回滚。"""
    return _guard(co.apply_correction, waybill_id, hop_id,
                  body.new_location_code, body.new_event_code, body.reason)


@router.post("/waybills/{waybill_id}/hops/{hop_id}/corrections/revoke")
def revoke_correction(waybill_id: str, hop_id: str, body: RevokeIn):
    """撤销更正：合成视图回到原点并留事件。"""
    return _guard(co.revoke_correction, waybill_id, hop_id, body.reason)


@router.get("/waybills/{waybill_id}/corrections")
def waybill_corrections(waybill_id: str):
    conn = connect()
    try:
        return co.list_proofs(conn, waybill_id)
    finally:
        conn.close()


@router.get("/corrections")
def all_corrections():
    conn = connect()
    try:
        return co.list_proofs(conn)
    finally:
        conn.close()


@router.get("/corrections/{overlay_id}")
def correction_proof(overlay_id: str):
    """更正证明只读接口：运单、目标 hop、覆盖前后、投影前后。"""
    conn = connect()
    try:
        proof = co.get_proof(conn, overlay_id)
        if not proof:
            raise HTTPException(404, "更正证明不存在")
        return proof
    finally:
        conn.close()


@router.post("/corrections/run-fixture")
def run_correction_fixture():
    """对照夹具：先落错点再更正。"""
    return _guard(co.run_fixture)
