import hashlib
import hmac
import json
import re
import secrets
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect, write_tx
from app.engines.fefo import consume_fefo, expire_lots

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
        if r["expiry"] <= today:
            r["level"] = "expired"
            out.append(r)
        else:
            # simple day diff via fromisoformat
            delta = (date.fromisoformat(r["expiry"]) - date.today()).days
            if delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

# --- inbound tickets -------------------------------------------------------
# A ticket is an HMAC over (item_id, qty, expiry) signed with a per-process
# secret. It proves the payload passed precheck unchanged; a restart simply
# asks clients to precheck again.
_TICKET_SECRET = secrets.token_hex(32)
_EXPIRY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def _ticket(item_id: int, qty: float, expiry: str) -> str:
    msg = f"{item_id}|{float(qty)!r}|{expiry}".encode()
    return hmac.new(_TICKET_SECRET.encode(), msg, hashlib.sha256).hexdigest()

def _check_expiry(expiry: str) -> None:
    # strict YYYY-MM-DD so lexicographic compare == chronological compare
    if not _EXPIRY_RE.match(expiry or ""):
        raise HTTPException(400, "bad_expiry")
    try:
        date.fromisoformat(expiry)
    except ValueError:
        raise HTTPException(400, "bad_expiry")

def _check_inbound(item, qty: float, expiry: str) -> None:
    if not item: raise HTTPException(404, "item")
    if qty <= 0: raise HTTPException(400, "qty_non_positive")
    _check_expiry(expiry)

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

class PrecheckIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

class ConfirmIn(BaseModel):
    item_id: int
    qty: float
    expiry: str
    ticket: str

@app.post("/api/lots/precheck")
def precheck(body: PrecheckIn):
    """Read-only: what would be written and where it lands. Never adds a row,
    so the layer's lot count is unchanged by precheck."""
    c = connect()
    try:
        item = c.execute("SELECT * FROM items WHERE id=?", (body.item_id,)).fetchone()
        _check_inbound(item, body.qty, body.expiry)
        if body.expiry < date.today().isoformat():
            raise HTTPException(409, "expiry_passed")
        layer_n = c.execute(
            """SELECT COUNT(*) n FROM lots JOIN items ON items.id=lots.item_id
               WHERE lots.status='on_shelf' AND items.layer=?""", (item["layer"],)).fetchone()["n"]
        total_n = c.execute("SELECT COUNT(*) n FROM lots WHERE status='on_shelf'").fetchone()["n"]
        return {
            "item_id": item["id"], "name": item["name"], "unit": item["unit"],
            "qty": body.qty, "expiry": body.expiry, "layer": item["layer"],
            "layer_lot_count": layer_n, "total_lot_count": total_n,
            "ticket": _ticket(body.item_id, body.qty, body.expiry),
        }
    finally:
        c.close()

@app.post("/api/lots/confirm")
def confirm(body: ConfirmIn):
    """Write exactly what the ticket says, in one generation with sweep/consume.

    If the clock has crossed the ticket's expiry by confirm time, the lot
    would instantly join the sweepable set; the whole order is rejected
    instead (expiry_passed), so layer view, alert bar and sweep list never
    disagree about a just-written lot.
    """
    c = connect()
    try:
        with write_tx(c):
            item = c.execute("SELECT * FROM items WHERE id=?", (body.item_id,)).fetchone()
            _check_inbound(item, body.qty, body.expiry)
            if not hmac.compare_digest(body.ticket or "", _ticket(body.item_id, body.qty, body.expiry)):
                raise HTTPException(409, "ticket_mismatch")
            if body.expiry < date.today().isoformat():
                raise HTTPException(409, "expiry_passed")
            cur = c.execute(
                "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
                (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
            lid = cur.lastrowid
        return {"id": lid, "item_id": item["id"], "name": item["name"], "layer": item["layer"],
                "qty": body.qty, "expiry": body.expiry, "status": "on_shelf"}
    finally:
        c.close()

@app.post("/api/lots")
def inbound(body: LotIn):
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
    c.commit(); lid = cur.lastrowid; c.close(); return {"id": lid}

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    c = connect()
    try:
        with write_tx(c):
            lots = [dict(r) for r in c.execute(
                "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0", (body.item_id,))]
            result = consume_fefo(lots, body.qty)
            if not result["ok"] and result["reason"] == "qty_non_positive":
                raise HTTPException(400, result["reason"])
            if not result["ok"]:
                raise HTTPException(409, result)
            for d in result["deductions"]:
                c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?", (d["take"], d["lot_id"]))
                rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
                if rem <= 0:
                    c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
            c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                      (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
        return result
    finally:
        c.close()

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect()
    try:
        with write_tx(c):
            lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
            ids = expire_lots(lots, date.today().isoformat())
            for i in ids:
                c.execute("UPDATE lots SET status='expired' WHERE id=?", (i,))
        return {"expired_ids": ids}
    finally:
        c.close()

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
