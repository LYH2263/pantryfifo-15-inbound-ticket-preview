"""票面预检 + 确认入库。

票面(ticket) = 规范化行内容(品项/数量/到期日，排序后)的哈希，无状态。
预检只读，不落行；确认整单一个事务：全部写入或整单拒写。
确认失败零写入——不改 lots、不动既有行的 data_quality。
时钟跨过票面到期日不拒写：该批照常落行并即刻进入可下架集合，
与下架名单、顶条、全层同一代(expiry < today 谓词处处一致)。
"""
import hashlib
import json
from datetime import date

TICKET_PREFIX = "pf1"


class InboundError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _canonical_lines(lines) -> list[dict]:
    """规范化：item_id 取整、qty 取浮点、expiry 归一为 ISO 日期，排序使键序/行序无关。"""
    canon = []
    for ln in lines:
        try:
            item_id = int(ln["item_id"])
            qty = float(ln["qty"])
            expiry = date.fromisoformat(str(ln["expiry"])).isoformat()
        except (KeyError, TypeError, ValueError):
            raise InboundError("bad_line")
        canon.append({"item_id": item_id, "qty": qty, "expiry": expiry})
    canon.sort(key=lambda l: (l["item_id"], l["expiry"], l["qty"]))
    return canon


def make_ticket(lines) -> str:
    payload = json.dumps(_canonical_lines(lines), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return TICKET_PREFIX + "_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate(canon: list[dict], item_ids: set):
    if not canon:
        raise InboundError("empty_lines")
    for ln in canon:
        if ln["qty"] <= 0:
            raise InboundError("qty_non_positive")
        if ln["item_id"] not in item_ids:
            raise InboundError("item_not_found")


def _items_by_id(conn) -> dict:
    return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM items")}


def _planned(canon: list[dict], items: dict, today: str) -> list[dict]:
    out = []
    for ln in canon:
        it = items[ln["item_id"]]
        out.append({**ln, "name": it["name"], "layer": it["layer"], "removable": ln["expiry"] < today})
    return out


def precheck(conn, lines, today: str) -> dict:
    """只读预检：返回票面与将写入的行(含将落层、是否即刻可下架)。不落任何行。"""
    canon = _canonical_lines(lines)
    items = _items_by_id(conn)
    _validate(canon, set(items))
    return {"ticket": make_ticket(canon), "lines": _planned(canon, items, today)}


def confirm(conn, ticket: str, lines, today: str) -> list[dict]:
    """整单写入。票面不符或行非法 → InboundError，零写入。

    到期日已过的批照常落 on_shelf 行，即刻进入可下架集合(同一事务、同一代)。
    """
    canon = _canonical_lines(lines)
    if make_ticket(canon) != ticket:
        raise InboundError("ticket_mismatch")
    items = _items_by_id(conn)
    _validate(canon, set(items))
    planned = _planned(canon, items, today)
    try:
        for ln in planned:
            cur = conn.execute(
                "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
                (ln["item_id"], ln["qty"], ln["qty"], ln["expiry"], "on_shelf", "clean"))
            ln["id"] = cur.lastrowid
            ln["status"] = "on_shelf"
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return planned
