"""独立车辆组合目录 (vehicle composition catalog).

目录只负责车辆稳定身份、空车质量区间，以及按严格递增序号发生的组合动作。
它不知道磅单、票、货重或运输。

不变量
------
* 实体身份 ``tractor:<stable_id>`` / ``trailer:<stable_id>`` 稳定且永不复用。
* 动作序号严格递增；已应用的历史动作与历史产品不可变。
* 一个挂车同时至多连接一台牵引车；一台牵引车同时至多连接一个挂车。
* 批动作 (``apply_batch``) 整体成功或整体不存在；失败不消耗序号。
* ``product_at(seq, tractor)`` 回答该序号处该牵引车的公开组合产品：身份、
  两实体质量区间、关系来源 (initial/attach/detach/mass_correction)。换挂后
  旧序号处的产品仍是旧产品，绝不会被后来换挂覆盖。
* 质量更正 (``apply_set_mass``) 会为 *当前* 组合产生 ``live`` 新版本，同时
  为每一段历史挂接产生 ``live=0`` 的更正版 (identity 不变)。历史挂在后来
  更正面前不会被改写，但可以通过 ``corrected_product(original)`` 取到
  "同一身份、更正后质量" 的新产品用于重评；它带 ``predecessor`` 前序身份。

质量区间是整数闭区间 [lo, hi]，0 <= lo <= hi，单位 kg。
容量：100 个实体。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Sequence

TRACTOR = "tractor"
TRAILER = "trailer"

SRC_INITIAL = "initial"
SRC_ATTACH = "attach"
SRC_DETACH = "detach"
SRC_MASS_CORRECTION = "mass_correction"

MODE_INITIAL = "INITIAL"
MODE_EMPTY = "EMPTY"
MODE_RELABEL = "RELABEL"

MAX_ENTITIES = 100

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS entity (
    entity_id TEXT PRIMARY KEY,
    kind      TEXT NOT NULL CHECK (kind IN ('tractor','trailer')),
    stable_id TEXT NOT NULL,
    UNIQUE (kind, stable_id)
);

CREATE TABLE IF NOT EXISTS entity_mass (
    entity_id TEXT PRIMARY KEY REFERENCES entity(entity_id),
    lo INTEGER NOT NULL CHECK (lo >= 0),
    hi INTEGER NOT NULL CHECK (hi >= lo)
);

CREATE TABLE IF NOT EXISTS action (
    seq        INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('attach','detach','set_mass')),
    tractor_id TEXT,
    trailer_id TEXT,
    entity_id  TEXT,
    lo         INTEGER,
    hi         INTEGER,
    batch_id   TEXT
);

CREATE TABLE IF NOT EXISTS binding (
    binding_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tractor_id TEXT NOT NULL REFERENCES entity(entity_id),
    trailer_id TEXT NOT NULL REFERENCES entity(entity_id),
    attach_seq INTEGER NOT NULL UNIQUE,
    detach_seq INTEGER,
    CHECK (detach_seq IS NULL OR detach_seq > attach_seq)
);
-- 同一时刻每个实体至多一条 live 关系。
CREATE UNIQUE INDEX IF NOT EXISTS binding_live_tractor
    ON binding(tractor_id) WHERE detach_seq IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS binding_live_trailer
    ON binding(trailer_id) WHERE detach_seq IS NULL;

CREATE TABLE IF NOT EXISTS product (
    product_id   TEXT PRIMARY KEY,
    action_seq   INTEGER NOT NULL,
    tractor_id   TEXT NOT NULL REFERENCES entity(entity_id),
    trailer_id   TEXT REFERENCES entity(entity_id),
    mode         TEXT NOT NULL,
    relation_src TEXT NOT NULL,
    attach_seq   INTEGER,
    predecessor  TEXT REFERENCES product(product_id),
    live         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS product_tractor ON product(tractor_id, live, action_seq);
CREATE INDEX IF NOT EXISTS product_pred ON product(predecessor);

CREATE TABLE IF NOT EXISTS product_mass (
    product_id TEXT NOT NULL REFERENCES product(product_id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('tractor','trailer')),
    entity_id  TEXT NOT NULL REFERENCES entity(entity_id),
    lo         INTEGER NOT NULL,
    hi         INTEGER NOT NULL,
    PRIMARY KEY (product_id, kind)
);

CREATE TABLE IF NOT EXISTS batch (
    batch_id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS batch_entry (
    batch_id TEXT NOT NULL REFERENCES batch(batch_id),
    ordinal  INTEGER NOT NULL,
    seq      INTEGER NOT NULL,
    PRIMARY KEY (batch_id, ordinal)
);
"""


class CatalogError(ValueError):
    """目录动作或查询违反不变量。"""


class LimitError(CatalogError):
    """超过题面容量上限。"""


def _range(lo: int, hi: int) -> tuple[int, int]:
    if isinstance(lo, bool) or isinstance(hi, bool):
        raise CatalogError("mass bounds must be integers")
    lo, hi = int(lo), int(hi)
    if lo < 0 or hi < lo:
        raise CatalogError(f"invalid mass range [{lo},{hi}] (need 0 <= lo <= hi)")
    return lo, hi


def _eid(kind: str, stable_id: str) -> str:
    if kind not in (TRACTOR, TRAILER):
        raise CatalogError(f"unknown entity kind {kind!r}")
    sid = str(stable_id).strip()
    if not sid:
        raise CatalogError("stable_id must be non-empty")
    return f"{kind}:{sid}"


def _bare(eid: str) -> str:
    return eid.split(":", 1)[1]


@dataclass(frozen=True)
class MassRange:
    lo: int
    hi: int

    def __post_init__(self) -> None:
        lo, hi = _range(self.lo, self.hi)
        object.__setattr__(self, "lo", lo)
        object.__setattr__(self, "hi", hi)

    def width(self) -> int:
        return self.hi - self.lo

    def as_list(self) -> list[int]:
        return [self.lo, self.hi]

    def __iter__(self) -> Iterator[int]:
        return iter((self.lo, self.hi))


@dataclass(frozen=True)
class ProductMass:
    kind: str
    entity_id: str
    lo: int
    hi: int

    @property
    def range(self) -> MassRange:
        return MassRange(self.lo, self.hi)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "entity_id": self.entity_id,
            "lo": self.lo,
            "hi": self.hi,
        }


@dataclass(frozen=True)
class Product:
    """某一动作序号处一台牵引车的组合产品；不可变。"""

    product_id: str
    action_seq: int
    tractor_id: str
    trailer_id: str | None
    mode: str
    relation_src: str
    attach_seq: int | None
    predecessor: str | None
    live: bool
    masses: tuple[ProductMass, ...]

    def mass_of(self, kind: str) -> ProductMass:
        for m in self.masses:
            if m.kind == kind:
                return m
        raise CatalogError(f"product {self.product_id} has no {kind} mass")

    def identity(self) -> tuple[str, str | None]:
        return (self.tractor_id, self.trailer_id)

    def stable_identity(self) -> tuple[str, str | None]:
        return (
            _bare(self.tractor_id),
            None if self.trailer_id is None else _bare(self.trailer_id),
        )

    def empty_interval(self) -> tuple[int, int]:
        return (sum(m.lo for m in self.masses), sum(m.hi for m in self.masses))

    def to_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "action_seq": self.action_seq,
            "tractor_id": self.tractor_id,
            "trailer_id": self.trailer_id,
            "mode": self.mode,
            "relation_src": self.relation_src,
            "attach_seq": self.attach_seq,
            "predecessor": self.predecessor,
            "live": self.live,
            "masses": [m.to_dict() for m in self.masses],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Product":
        return cls(
            product_id=data["product_id"],
            action_seq=int(data["action_seq"]),
            tractor_id=data["tractor_id"],
            trailer_id=data.get("trailer_id"),
            mode=data.get("mode", MODE_EMPTY),
            relation_src=data["relation_src"],
            attach_seq=data.get("attach_seq"),
            predecessor=data.get("predecessor"),
            live=bool(data.get("live", True)),
            masses=tuple(
                ProductMass(
                    kind=m["kind"],
                    entity_id=m["entity_id"],
                    lo=int(m["lo"]),
                    hi=int(m["hi"]),
                )
                for m in data["masses"]
            ),
        )


@dataclass(frozen=True)
class Snapshot:
    """目录在某个查询序号处的公开组合快照，可跨系统交换。"""

    seq: int
    product: Product

    def to_dict(self) -> dict:
        return {
            "Kind": "composition_snapshot",
            "Version": 1,
            "seq": int(self.seq),
            "product": self.product.to_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        if data.get("Kind") != "composition_snapshot":
            raise CatalogError("not a composition_snapshot")
        if int(data.get("Version", 0)) != 1:
            raise CatalogError(f"unsupported snapshot Version {data.get('Version')!r}")
        return cls(seq=int(data["seq"]), product=Product.from_dict(data["product"]))

    @classmethod
    def from_json(cls, text: str) -> "Snapshot":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CatalogError(f"invalid snapshot json: {exc}") from exc
        return cls.from_dict(data)


@dataclass(frozen=True)
class Action:
    seq: int
    kind: str
    tractor_id: str | None
    trailer_id: str | None
    entity_id: str | None
    lo: int | None
    hi: int | None
    batch_id: str | None


class Catalog:
    """SQLite 支撑的车辆组合目录。"""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None  # 显式事务
        self.conn.executescript(SCHEMA)

    # ---------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ---------------------------------------------------------------- entities
    def define_mass(self, kind: str, stable_id: str, lo: int, hi: int) -> str:
        """引导：登记实体及初始质量区间。只能在任何组合动作之前调用。"""
        eid = _eid(kind, stable_id)
        lo, hi = _range(lo, hi)
        with self.transaction() as c:
            if self._action_count(c) > 0:
                raise CatalogError(
                    "define_mass is bootstrap-only; use a set_mass action afterwards"
                )
            if self._entity_count(c) >= MAX_ENTITIES and self._find_entity(c, eid) is None:
                raise LimitError(f"entity limit {MAX_ENTITIES} exceeded")
            c.execute(
                "INSERT OR IGNORE INTO entity(entity_id, kind, stable_id) VALUES (?,?,?)",
                (eid, kind, str(stable_id).strip()),
            )
            c.execute(
                "INSERT INTO entity_mass(entity_id, lo, hi) VALUES (?,?,?) "
                "ON CONFLICT(entity_id) DO UPDATE SET lo=excluded.lo, hi=excluded.hi",
                (eid, lo, hi),
            )
            self._rebuild_initial_products(c)
        return eid

    def _find_entity(self, c: sqlite3.Connection, eid: str) -> sqlite3.Row | None:
        return c.execute("SELECT * FROM entity WHERE entity_id=?", (eid,)).fetchone()

    def entity_id(self, kind: str, stable_id: str) -> str:
        eid = _eid(kind, stable_id)
        if self._find_entity(self.conn, eid) is None:
            raise CatalogError(f"unknown entity {eid}")
        return eid

    def entity_mass(self, kind: str, stable_id: str) -> MassRange:
        eid = _eid(kind, stable_id)
        row = self.conn.execute(
            "SELECT lo, hi FROM entity_mass WHERE entity_id=?", (eid,)
        ).fetchone()
        if row is None:
            raise CatalogError(f"entity {eid} has no mass range")
        return MassRange(row["lo"], row["hi"])

    def entities(self) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT e.entity_id, e.kind, e.stable_id, m.lo, m.hi FROM entity e "
                "LEFT JOIN entity_mass m ON m.entity_id=e.entity_id ORDER BY e.entity_id"
            )
        ]

    # ---------------------------------------------------------------- actions
    def next_seq(self) -> int:
        row = self.conn.execute("SELECT MAX(seq) AS m FROM action").fetchone()
        return int(row["m"] or 0) + 1

    def last_seq(self) -> int:
        row = self.conn.execute("SELECT MAX(seq) AS m FROM action").fetchone()
        return int(row["m"] or 0)

    def apply_attach(self, tractor: str, trailer: str, *, seq: int | None = None) -> int:
        teid, reid = self._known(TRACTOR, tractor), self._known(TRAILER, trailer)
        with self.transaction() as c:
            seq = self._reserve(c, seq, "attach", teid, reid, None, None, None)
            self._attach(c, seq, teid, reid)
        return seq

    def apply_detach(self, tractor: str, trailer: str, *, seq: int | None = None) -> int:
        teid, reid = self._known(TRACTOR, tractor), self._known(TRAILER, trailer)
        with self.transaction() as c:
            seq = self._reserve(c, seq, "detach", teid, reid, None, None, None)
            self._detach(c, seq, teid, reid)
        return seq

    def apply_set_mass(
        self, kind: str, stable_id: str, lo: int, hi: int, *, seq: int | None = None
    ) -> int:
        lo, hi = _range(lo, hi)
        eid = self._known(kind, stable_id)
        with self.transaction() as c:
            seq = self._reserve(c, seq, "set_mass", None, None, eid, lo, hi, None)
            self._set_mass(c, seq, eid, lo, hi)
        return seq

    def apply_action(self, action: dict, *, seq: int | None = None) -> int:
        """通用入口：``{"kind": "attach"|"detach"|"set_mass", ...}``。"""
        kind = action.get("kind")
        if kind == "attach":
            return self.apply_attach(action["tractor"], action["trailer"], seq=seq)
        if kind == "detach":
            return self.apply_detach(action["tractor"], action["trailer"], seq=seq)
        if kind == "set_mass":
            ek, sid = action["entity"]
            return self.apply_set_mass(ek, sid, action["lo"], action["hi"], seq=seq)
        raise CatalogError(f"unknown action kind {kind!r}")

    def apply_batch(
        self, actions: Sequence[dict], *, batch_id: str | None = None
    ) -> list[int]:
        """原子批：全部生效，或目录与序号都完全不变。"""
        if not actions:
            raise CatalogError("empty batch")
        bid = batch_id or f"batch-{self.next_seq()}"
        prior = self.conn.execute(
            "SELECT seq FROM batch_entry WHERE batch_id=? ORDER BY ordinal", (bid,)
        ).fetchall()
        if prior:
            return [int(r["seq"]) for r in prior]
        try:
            with self.transaction() as c:
                c.execute("INSERT INTO batch(batch_id) VALUES (?)", (bid,))
                seqs: list[int] = []
                for ordinal, raw in enumerate(actions):
                    kind = raw.get("kind")
                    if kind == "attach":
                        teid = self._known(TRACTOR, raw["tractor"])
                        reid = self._known(TRAILER, raw["trailer"])
                        seq = self._reserve(
                            c, raw.get("seq"), kind, teid, reid, None, None, None, bid
                        )
                        self._attach(c, seq, teid, reid)
                    elif kind == "detach":
                        teid = self._known(TRACTOR, raw["tractor"])
                        reid = self._known(TRAILER, raw["trailer"])
                        seq = self._reserve(
                            c, raw.get("seq"), kind, teid, reid, None, None, None, bid
                        )
                        self._detach(c, seq, teid, reid)
                    elif kind == "set_mass":
                        ek, sid = raw["entity"]
                        lo, hi = _range(raw["lo"], raw["hi"])
                        eid = self._known(ek, sid)
                        seq = self._reserve(
                            c, raw.get("seq"), kind, None, None, eid, lo, hi, bid
                        )
                        self._set_mass(c, seq, eid, lo, hi)
                    else:
                        raise CatalogError(f"unknown action kind {kind!r}")
                    c.execute(
                        "INSERT INTO batch_entry(batch_id, ordinal, seq) VALUES (?,?,?)",
                        (bid, ordinal, seq),
                    )
                    seqs.append(seq)
            return seqs
        except sqlite3.IntegrityError as exc:
            raise CatalogError(f"batch {bid} rejected: {exc}") from exc

    def _known(self, kind: str, stable_id: str) -> str:
        eid = _eid(kind, stable_id)
        if self._find_entity(self.conn, eid) is None:
            raise CatalogError(f"unknown entity {eid}")
        row = self.conn.execute(
            "SELECT 1 FROM entity_mass WHERE entity_id=?", (eid,)
        ).fetchone()
        if row is None:
            raise CatalogError(f"entity {eid} has no mass range")
        return eid

    def _reserve(
        self,
        c: sqlite3.Connection,
        seq: int | None,
        kind: str,
        tractor_eid: str | None,
        trailer_eid: str | None,
        entity_eid: str | None,
        lo: int | None,
        hi: int | None,
        batch_id: str | None = None,
    ) -> int:
        last = c.execute("SELECT MAX(seq) AS m FROM action").fetchone()["m"] or 0
        if seq is None:
            seq = int(last) + 1
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise CatalogError("seq must be an integer")
        if seq <= int(last):
            raise CatalogError(f"action seq must strictly increase: {seq} <= {last}")
        c.execute(
            "INSERT INTO action(seq, kind, tractor_id, trailer_id, entity_id, lo, hi, batch_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (seq, kind, tractor_eid, trailer_eid, entity_eid, lo, hi, batch_id),
        )
        return seq

    def _attach(
        self, c: sqlite3.Connection, seq: int, tractor_eid: str, trailer_eid: str
    ) -> None:
        if c.execute(
            "SELECT 1 FROM binding WHERE tractor_id=? AND detach_seq IS NULL",
            (tractor_eid,),
        ).fetchone():
            raise CatalogError(f"tractor {_bare(tractor_eid)} already has a live trailer")
        if c.execute(
            "SELECT 1 FROM binding WHERE trailer_id=? AND detach_seq IS NULL",
            (trailer_eid,),
        ).fetchone():
            raise CatalogError(f"trailer {_bare(trailer_eid)} is already attached")
        c.execute(
            "INSERT INTO binding(tractor_id, trailer_id, attach_seq) VALUES (?,?,?)",
            (tractor_eid, trailer_eid, seq),
        )
        self._emit(c, seq, tractor_eid, trailer_eid, MODE_EMPTY, SRC_ATTACH, seq, None, True)

    def _detach(
        self, c: sqlite3.Connection, seq: int, tractor_eid: str, trailer_eid: str
    ) -> None:
        row = c.execute(
            "SELECT binding_id, attach_seq FROM binding "
            "WHERE tractor_id=? AND trailer_id=? AND detach_seq IS NULL",
            (tractor_eid, trailer_eid),
        ).fetchone()
        if row is None:
            raise CatalogError(
                f"trailer {_bare(trailer_eid)} is not attached to tractor "
                f"{_bare(tractor_eid)}"
            )
        c.execute(
            "UPDATE binding SET detach_seq=? WHERE binding_id=?",
            (seq, row["binding_id"]),
        )
        self._emit(
            c, seq, tractor_eid, None, MODE_EMPTY, SRC_DETACH,
            int(row["attach_seq"]), None, True,
        )

    def _set_mass(
        self, c: sqlite3.Connection, seq: int, entity_eid: str, lo: int, hi: int
    ) -> None:
        c.execute(
            "UPDATE entity_mass SET lo=?, hi=? WHERE entity_id=?", (lo, hi, entity_eid)
        )
        emitted_attach: set[int] = set()
        # 1) 每台牵引车的当前 live 产品：若涉及该实体，产生 live 更正版。
        for trow in c.execute(
            "SELECT entity_id FROM entity WHERE kind=? ORDER BY entity_id", (TRACTOR,)
        ).fetchall():
            teid = trow["entity_id"]
            prev = c.execute(
                "SELECT * FROM product WHERE tractor_id=? AND live=1 AND action_seq<? "
                "ORDER BY action_seq DESC, rowid DESC LIMIT 1",
                (teid, seq),
            ).fetchone()
            if prev is None:
                continue
            masses = self._mass_ids(c, prev["product_id"])
            if entity_eid == teid or entity_eid in masses:
                self._emit(
                    c, seq, teid, prev["trailer_id"], MODE_RELABEL,
                    SRC_MASS_CORRECTION, prev["attach_seq"], prev["product_id"], True,
                )
                if prev["attach_seq"] is not None and prev["trailer_id"] is not None:
                    emitted_attach.add(int(prev["attach_seq"]))
        # 2) 所有历史挂接：(identity 不变) 的更正版，live=0，供重评使用。
        for brow in c.execute(
            "SELECT * FROM binding WHERE tractor_id=? OR trailer_id=? ORDER BY attach_seq",
            (entity_eid, entity_eid),
        ).fetchall():
            if int(brow["attach_seq"]) in emitted_attach:
                continue
            prev = c.execute(
                "SELECT * FROM product WHERE tractor_id=? AND attach_seq=? "
                "AND trailer_id=? AND action_seq<? "
                "ORDER BY action_seq DESC, rowid DESC LIMIT 1",
                (brow["tractor_id"], brow["attach_seq"], brow["trailer_id"], seq),
            ).fetchone()
            if prev is None:
                continue
            self._emit(
                c, seq, brow["tractor_id"], brow["trailer_id"], MODE_RELABEL,
                SRC_MASS_CORRECTION, int(brow["attach_seq"]), prev["product_id"], False,
            )

    def _mass_ids(self, c: sqlite3.Connection, product_id: str) -> set[str]:
        return {
            r["entity_id"]
            for r in c.execute(
                "SELECT entity_id FROM product_mass WHERE product_id=?", (product_id,)
            )
        }

    def _emit(
        self,
        c: sqlite3.Connection,
        seq: int,
        tractor_eid: str,
        trailer_eid: str | None,
        mode: str,
        relation_src: str,
        attach_seq: int | None,
        predecessor: str | None,
        live: bool,
    ) -> str:
        base = f"P{seq:06d}-{_bare(tractor_eid)}-A{attach_seq if attach_seq is not None else 0}"
        pid = base
        dup = 1
        while c.execute("SELECT 1 FROM product WHERE product_id=?", (pid,)).fetchone():
            dup += 1
            pid = f"{base}-{dup}"
        t = c.execute(
            "SELECT lo, hi FROM entity_mass WHERE entity_id=?", (tractor_eid,)
        ).fetchone()
        if t is None:
            raise CatalogError(f"tractor {_bare(tractor_eid)} has no mass range")
        c.execute(
            "INSERT INTO product(product_id, action_seq, tractor_id, trailer_id, mode, "
            "relation_src, attach_seq, predecessor, live) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                pid, seq, tractor_eid, trailer_eid, mode, relation_src, attach_seq,
                predecessor, int(live),
            ),
        )
        c.execute(
            "INSERT INTO product_mass(product_id, kind, entity_id, lo, hi) VALUES (?,?,?,?,?)",
            (pid, TRACTOR, tractor_eid, t["lo"], t["hi"]),
        )
        if trailer_eid is not None:
            r = c.execute(
                "SELECT lo, hi FROM entity_mass WHERE entity_id=?", (trailer_eid,)
            ).fetchone()
            if r is None:
                raise CatalogError(f"trailer {_bare(trailer_eid)} has no mass range")
            c.execute(
                "INSERT INTO product_mass(product_id, kind, entity_id, lo, hi) "
                "VALUES (?,?,?,?,?)",
                (pid, TRAILER, trailer_eid, r["lo"], r["hi"]),
            )
        return pid

    def _rebuild_initial_products(self, c: sqlite3.Connection) -> None:
        """引导阶段：为每台已定义质量的牵引车生成 seq 0 的空载产品。"""
        c.execute(
            "DELETE FROM product_mass WHERE product_id IN "
            "(SELECT product_id FROM product WHERE action_seq=0)"
        )
        c.execute("DELETE FROM product WHERE action_seq=0")
        for row in c.execute(
            "SELECT e.entity_id FROM entity e JOIN entity_mass m "
            "ON m.entity_id=e.entity_id WHERE e.kind=? ORDER BY e.entity_id",
            (TRACTOR,),
        ).fetchall():
            self._emit(c, 0, row["entity_id"], None, MODE_INITIAL, SRC_INITIAL, None, None, True)

    # ---------------------------------------------------------------- queries
    def _load_product(self, product_id: str) -> Product:
        row = self.conn.execute(
            "SELECT * FROM product WHERE product_id=?", (product_id,)
        ).fetchone()
        if row is None:
            raise CatalogError(f"unknown product {product_id}")
        masses = self.conn.execute(
            "SELECT kind, entity_id, lo, hi FROM product_mass WHERE product_id=? "
            "ORDER BY CASE kind WHEN 'tractor' THEN 0 ELSE 1 END",
            (product_id,),
        ).fetchall()
        return Product(
            product_id=row["product_id"],
            action_seq=int(row["action_seq"]),
            tractor_id=row["tractor_id"],
            trailer_id=row["trailer_id"],
            mode=row["mode"],
            relation_src=row["relation_src"],
            attach_seq=row["attach_seq"],
            predecessor=row["predecessor"],
            live=bool(row["live"]),
            masses=tuple(
                ProductMass(m["kind"], m["entity_id"], int(m["lo"]), int(m["hi"]))
                for m in masses
            ),
        )

    def product(self, product_id: str) -> Product:
        return self._load_product(product_id)

    def product_at(self, seq: int, tractor: str) -> Product:
        """``seq`` 处该牵引车的公开组合：最新 live 产品 (含更正版)。"""
        if seq < 0:
            raise CatalogError("seq must be >= 0")
        eid = _eid(TRACTOR, tractor)
        row = self.conn.execute(
            "SELECT product_id FROM product WHERE tractor_id=? AND live=1 AND action_seq<=? "
            "ORDER BY action_seq DESC, rowid DESC LIMIT 1",
            (eid, int(seq)),
        ).fetchone()
        if row is None:
            raise CatalogError(f"no live product for tractor {tractor} at seq {seq}")
        return self._load_product(row["product_id"])

    def snapshot_at(self, seq: int, tractor: str) -> Snapshot:
        return Snapshot(seq=int(seq), product=self.product_at(seq, tractor))

    def snapshot_of(self, product: Product, *, seq: int | None = None) -> Snapshot:
        return Snapshot(seq=product.action_seq if seq is None else int(seq), product=product)

    def current_product(self, tractor: str) -> Product:
        return self.product_at(self.last_seq(), tractor)

    def products_at(self, seq: int | None = None) -> list[Product]:
        """所有牵引车在该序号处的 live 产品 (每车一份)。"""
        if seq is None:
            seq = self.last_seq()
        if seq < 0:
            raise CatalogError("seq must be >= 0")
        rows = self.conn.execute(
            "SELECT p.product_id FROM product p JOIN ("
            "  SELECT tractor_id, MAX(action_seq) AS m FROM product "
            "  WHERE live=1 AND action_seq<=? GROUP BY tractor_id"
            ") x ON x.tractor_id=p.tractor_id AND x.m=p.action_seq "
            "WHERE p.live=1 ORDER BY p.tractor_id",
            (int(seq),),
        ).fetchall()
        return [self._load_product(r["product_id"]) for r in rows]

    def corrected_product(
        self, product: Product | str, *, as_of_seq: int | None = None
    ) -> Product:
        """同一挂接 (identity + attach_seq) 的最新更正版；无更正则返回原产品。

        沿 ``predecessor`` 链向后走，故结果一定带可追溯的前序身份，而不是从
        当前挂接猜测历史。``as_of_seq`` 可把更正链截断到某序号。
        """
        orig = self.product(product) if isinstance(product, str) else product
        cur = orig
        seen = {cur.product_id}
        while True:
            sql = "SELECT * FROM product WHERE predecessor=?"
            args: list[object] = [cur.product_id]
            if as_of_seq is not None:
                sql += " AND action_seq<=?"
                args.append(int(as_of_seq))
            sql += " ORDER BY action_seq DESC, rowid DESC LIMIT 1"
            row = self.conn.execute(sql, args).fetchone()
            if row is None or row["product_id"] in seen:
                return cur
            # 只接受同一挂接 (identity 不变) 的更正版；换挂不算更正。
            if (
                row["tractor_id"] != orig.tractor_id
                or row["trailer_id"] != orig.trailer_id
                or row["attach_seq"] != orig.attach_seq
            ):
                return cur
            cur = self._load_product(row["product_id"])
            seen.add(cur.product_id)

    def binding_history(self) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT b.binding_id, b.attach_seq, b.detach_seq, "
                "       t.stable_id AS tractor, r.stable_id AS trailer "
                "FROM binding b JOIN entity t ON t.entity_id=b.tractor_id "
                "JOIN entity r ON r.entity_id=b.trailer_id ORDER BY b.attach_seq"
            )
        ]

    def current_bindings(self) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT b.binding_id, b.attach_seq, "
                "       t.stable_id AS tractor, r.stable_id AS trailer "
                "FROM binding b JOIN entity t ON t.entity_id=b.tractor_id "
                "JOIN entity r ON r.entity_id=b.trailer_id "
                "WHERE b.detach_seq IS NULL ORDER BY b.attach_seq"
            )
        ]

    def actions(self) -> list[Action]:
        return [
            Action(
                seq=int(r["seq"]), kind=r["kind"], tractor_id=r["tractor_id"],
                trailer_id=r["trailer_id"], entity_id=r["entity_id"],
                lo=r["lo"], hi=r["hi"], batch_id=r["batch_id"],
            )
            for r in self.conn.execute("SELECT * FROM action ORDER BY seq")
        ]

    def history(self, kind: str, stable_id: str) -> list[dict]:
        """某实体参与过的全部组合产品及其关系来源 (含更正版)。"""
        eid = _eid(kind, stable_id)
        return [
            self._load_product(r["product_id"]).to_dict()
            for r in self.conn.execute(
                "SELECT product_id FROM product WHERE tractor_id=? OR trailer_id=? "
                "ORDER BY action_seq, product_id",
                (eid, eid),
            )
        ]

    # ---------------------------------------------------------------- integrity
    def _action_count(self, c: sqlite3.Connection) -> int:
        return int(c.execute("SELECT COUNT(*) AS n FROM action").fetchone()["n"])

    def _entity_count(self, c: sqlite3.Connection) -> int:
        return int(c.execute("SELECT COUNT(*) AS n FROM entity").fetchone()["n"])

    def verify(self) -> list[str]:
        errs: list[str] = []
        for col, label in (("tractor_id", "tractor"), ("trailer_id", "trailer")):
            for r in self.conn.execute(
                f"SELECT {col} AS e, COUNT(*) AS n FROM binding "
                f"WHERE detach_seq IS NULL GROUP BY {col} HAVING n>1"
            ):
                errs.append(f"live {label} {r['e']} bound {r['n']} times")
        for r in self.conn.execute(
            "SELECT p.product_id, "
            " SUM(CASE WHEN m.kind='tractor' THEN 1 ELSE 0 END) AS nt, "
            " SUM(CASE WHEN m.kind='trailer' THEN 1 ELSE 0 END) AS nr, "
            " p.trailer_id FROM product p LEFT JOIN product_mass m "
            " ON m.product_id=p.product_id GROUP BY p.product_id"
        ):
            if r["nt"] != 1:
                errs.append(f"product {r['product_id']} tractor mass count {r['nt']}")
            if r["nr"] != (0 if r["trailer_id"] is None else 1):
                errs.append(f"product {r['product_id']} trailer mass count {r['nr']}")
        for r in self.conn.execute(
            "SELECT b.binding_id, b.tractor_id, b.trailer_id FROM binding b "
            "WHERE b.detach_seq IS NOT NULL AND NOT EXISTS ("
            " SELECT 1 FROM product p WHERE p.action_seq=b.detach_seq "
            " AND p.tractor_id=b.tractor_id AND p.trailer_id IS NULL)"
        ):
            errs.append(f"binding {r['binding_id']} detach has no EMPTY product")
        for r in self.conn.execute("SELECT product_id, kind, lo, hi FROM product_mass"):
            if not (0 <= r["lo"] <= r["hi"]):
                errs.append(f"product {r['product_id']} {r['kind']} bad range")
        for r in self.conn.execute(
            "SELECT product_id FROM product WHERE live=1 AND tractor_id IN ("
            " SELECT tractor_id FROM product WHERE live=1 GROUP BY tractor_id, action_seq "
            " HAVING COUNT(*)>1)"
        ):
            errs.append(f"ambiguous live product {r['product_id']}")
        return errs
