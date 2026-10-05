import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, expire_lots
from app.engines.inbound import InboundError, confirm as confirm_lines, precheck as precheck_lines

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    c = connect()
    q = """SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'"""
    args = []
    if layer:
        q += " AND items.layer=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(q, args)]; c.close(); return rows

@app.get("/api/alerts")
def alerts():
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        """SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
           WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL""")]
    c.close()
    out = []
    for r in rows:
        # 与下架扫/下架名单同一谓词：expiry < today 才算已过期可下架
        if r["expiry"] < today:
            r["level"] = "expired"
            out.append(r)
        else:
            # simple day diff via fromisoformat
            delta = (date.fromisoformat(r["expiry"]) - date.today()).days
            if delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
    c.commit(); lid = cur.lastrowid; c.close(); return {"id": lid}

class InboundLine(BaseModel):
    item_id: int
    qty: float
    expiry: str

class PrecheckIn(BaseModel):
    lines: list[InboundLine]

class ConfirmIn(BaseModel):
    ticket: str
    lines: list[InboundLine]

_INBOUND_STATUS = {"ticket_mismatch": 409}

def _line_dicts(lines):
    return [{"item_id": l.item_id, "qty": l.qty, "expiry": l.expiry} for l in lines]

@app.post("/api/inbound/precheck")
def inbound_precheck(body: PrecheckIn):
    c = connect()
    try:
        return precheck_lines(c, _line_dicts(body.lines), date.today().isoformat())
    except InboundError as e:
        raise HTTPException(_INBOUND_STATUS.get(e.reason, 422), e.reason)
    finally:
        c.close()

@app.post("/api/inbound/confirm")
def inbound_confirm(body: ConfirmIn):
    c = connect()
    try:
        lots = confirm_lines(c, body.ticket, _line_dicts(body.lines), date.today().isoformat())
        return {"lots": lots}
    except InboundError as e:
        raise HTTPException(_INBOUND_STATUS.get(e.reason, 422), e.reason)
    finally:
        c.close()

@app.get("/api/removals")
def removals():
    """下架名单：与下架扫同一谓词(on_shelf、有剩余、expiry < today)。"""
    c = connect()
    rows = [dict(r) for r in c.execute(
        """SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
           WHERE lots.status='on_shelf' AND lots.qty_remain>0
             AND lots.expiry IS NOT NULL AND lots.expiry < ?""",
        (date.today().isoformat(),))]
    c.close(); return rows

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    c = connect()
    lots = [dict(r) for r in c.execute(
        "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0", (body.item_id,))]
    result = consume_fefo(lots, body.qty)
    if not result["ok"] and result["reason"] == "qty_non_positive":
        c.close(); raise HTTPException(400, result["reason"])
    if not result["ok"]:
        c.close(); raise HTTPException(409, result)
    for d in result["deductions"]:
        c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?", (d["take"], d["lot_id"]))
        rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
        if rem <= 0:
            c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
    c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
              (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
    c.commit(); c.close(); return result

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect()
    lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
    ids = expire_lots(lots, date.today().isoformat())
    for i in ids:
        c.execute("UPDATE lots SET status='expired' WHERE id=?", (i,))
    c.commit(); c.close(); return {"expired_ids": ids}

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
