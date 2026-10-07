"""Append-only, persistent event journal (SQLite, WAL, synchronous=FULL).

The journal is the source of truth for restarts. Order state changes are
written BEFORE the corresponding venue action (write-ahead), so a crash
mid-submission leaves a SUBMITTING record that startup reconciliation turns
into UNKNOWN.
"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Iterator, Optional

from .models import Fill, Purpose, Side
from .state_machine import Order, OrderState


def _enc(o: Any) -> Any:
    if isinstance(o, Decimal):
        return {"__d": str(o)}
    if isinstance(o, (Side, Purpose, OrderState)):
        return o.name
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(type(o))


def _dec(d: dict) -> Any:
    if "__d" in d and len(d) == 1:
        return Decimal(d["__d"])
    return d


def fill_to_dict(f: Fill) -> dict:
    return {k: getattr(f, k) for k in f.__dataclass_fields__}


def fill_from_dict(d: dict) -> Fill:
    d = dict(d)
    d["side"] = Side[d["side"]]
    return Fill(**d)


def order_to_dict(o: Order) -> dict:
    d = {k: getattr(o, k) for k in o.__dataclass_fields__ if k != "fills"}
    d["fill_ids"] = sorted(o.fills)
    return d


def order_from_dict(d: dict, fills: dict[str, Fill]) -> Order:
    d = dict(d)
    ids = d.pop("fill_ids", [])
    d["side"] = Side[d["side"]]
    d["purpose"] = Purpose[d["purpose"]]
    d["state"] = OrderState[d["state"]]
    o = Order(**d)
    for i in ids:
        if i in fills:
            o.fills[i] = fills[i]
    return o


class Journal:
    def __init__(self, path: str, config_hash: str):
        self.path = path
        self.config_hash = config_hash
        self.db = sqlite3.connect(path, isolation_level=None)
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            " seq INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL,"
            " type TEXT NOT NULL, key TEXT, config_hash TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_type ON events(type)")
        # Retrospective analytics live in a SEPARATE table that trading code never reads.
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS markouts ("
            " fill_id TEXT, horizon_s INTEGER, payload TEXT, PRIMARY KEY(fill_id, horizon_s))"
        )

    # -- writes ----------------------------------------------------------
    def append(self, ts_ms: int, type_: str, payload: dict, key: Optional[str] = None) -> int:
        cur = self.db.execute(
            "INSERT INTO events(ts_ms,type,key,config_hash,payload) VALUES(?,?,?,?,?)",
            (ts_ms, type_, key, self.config_hash, json.dumps(payload, default=_enc, sort_keys=True)),
        )
        return int(cur.lastrowid)

    def record_order(self, ts_ms: int, order: Order) -> None:
        self.append(ts_ms, "ORDER", order_to_dict(order), key=order.client_order_id)

    def record_fill(self, ts_ms: int, fill: Fill) -> None:
        self.append(ts_ms, "FILL", fill_to_dict(fill), key=fill.fill_id)

    def record_markout(self, fill_id: str, horizon_s: int, payload: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO markouts VALUES(?,?,?)",
            (fill_id, horizon_s, json.dumps(payload, default=_enc, sort_keys=True)),
        )

    # -- reads -----------------------------------------------------------
    def events(self, type_: Optional[str] = None) -> Iterator[tuple[int, int, str, Optional[str], dict]]:
        q = "SELECT seq, ts_ms, type, key, payload FROM events"
        args: tuple = ()
        if type_:
            q += " WHERE type=?"
            args = (type_,)
        for seq, ts, t, k, p in self.db.execute(q + " ORDER BY seq", args):
            yield seq, ts, t, k, json.loads(p, object_hook=_dec)

    def load_fills(self) -> dict[str, Fill]:
        return {k: fill_from_dict(p) for _, _, _, k, p in self.events("FILL")}

    def load_orders(self) -> dict[str, Order]:
        fills = self.load_fills()
        latest: dict[str, dict] = {}
        for _, _, _, k, p in self.events("ORDER"):
            latest[k] = p
        orders = {k: order_from_dict(p, fills) for k, p in latest.items()}
        # Attach fills that arrived after the last order snapshot.
        for f in fills.values():
            o = orders.get(f.client_order_id)
            if o is not None:
                o.fills.setdefault(f.fill_id, f)
        return orders

    def close(self) -> None:
        self.db.close()
