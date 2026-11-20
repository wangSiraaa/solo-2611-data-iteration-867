"""过磅事实仓 (weighbridge fact store).

仓库消费目录在票面动作序号处的 *公开组合产品*，把票面毛重换算成货重区间，
并把两次过磅组织成一次可确认的运输。它与目录解耦：

* 目录回答：某序号处是什么组合、两实体空车质量区间、关系来源；
* 仓库回答：这张票为什么得到这个货重、哪些实体贡献不确定性、这一对票能否
  成立、净装卸量的精确最小/最大是多少。

语义
----
* 票：稳定 ``ticket_id``、牵引车稳定 ID、目录动作序号 ``catalog_seq``、整数
  时间 ``ts``、方向 IN/OUT、非负整数毛重 ``gross``。同一车同一秒允许多张票。
* 单票货重：``tare = Σ 空车质量``，``goods = gross - tare``，
  可行区间 ``[max(0, gross - tare_hi), gross - tare_lo]``；若 ``gross < tare_lo``
  （货重上界为负）则票面不合物理约束。
* 运输：显式选定一张 IN 票和一张 OUT 票，绝不按最近时间猜配；同一张票只能
  进入一次 *已确认* 运输。IN 时间不得晚于 OUT。
* LOAD 要求离厂货重 >= 入厂货重；UNLOAD 相反。
* 组合完全相同：空车质量是同一未知量，在毛重差中精确抵消，净装卸量
  == 毛重差，与区间宽度无关。
* 组合不同 (换挂)：共享实体 (如牵引车) 的系数为 0，精确抵消；不同实体
  各自以整数空重区间参与。在 "两张票货重非负 + 运输方向" 的联合可行域上，
  用精确整数分支定界求净装卸量的最小/最大；可行域为空则拒绝。绝不把区间
  中点或实数顶点当作结算真值。
* 重评：只新增带前序身份的结果；旧评估与旧运输保留。已被确认运输占用的票
  不会被重新记成另一运输。
* 外部组合快照：与目录相同的 JSON 格式 (``Snapshot``)，仓库可直接导入；
  历史产品只从票面序号 + 快照取得，绝不从 "当前挂接" 回填。

容量：目录 100 实体；仓库 1000 张票、200 次运输。
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable, Iterator, Sequence

from .catalog import Catalog, CatalogError, Product, Snapshot

MAX_TICKETS = 1000
MAX_TRIPS = 200

IN = "IN"
OUT = "OUT"
LOAD = "LOAD"
UNLOAD = "UNLOAD"

STATUS_DRAFT = "DRAFT"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_REVIEW = "REVIEW"
STATUS_SUPERSEDED = "SUPERSEDED"

REASON_OK = "OK"
REASON_TICKET_INFEASIBLE = "TICKET_INFEASIBLE"
REASON_DIRECTION_IMPOSSIBLE = "DIRECTION_IMPOSSIBLE"
REASON_MASS_INTERVAL_CONFLICT = "MASS_INTERVAL_CONFLICT"
REASON_TIME_ORDER = "IN_AFTER_OUT"
REASON_ROLE_MISMATCH = "ROLE_MISMATCH"
REASON_TRACTOR_MISMATCH = "TRACTOR_MISMATCH"
REASON_NO_PRODUCT = "NO_COMPOSITION_PRODUCT"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ticket (
    ticket_id   TEXT PRIMARY KEY,
    tractor     TEXT NOT NULL,
    catalog_seq INTEGER NOT NULL CHECK (catalog_seq >= 0),
    ts          INTEGER NOT NULL,
    direction   TEXT NOT NULL CHECK (direction IN ('IN','OUT')),
    gross       INTEGER NOT NULL CHECK (gross >= 0),
    payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ticket_tractor ON ticket(tractor, ts);

CREATE TABLE IF NOT EXISTS trip (
    trip_id      TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('LOAD','UNLOAD')),
    status       TEXT NOT NULL CHECK (status IN ('DRAFT','CONFIRMED','REVIEW','SUPERSEDED')),
    predecessor_trip TEXT REFERENCES trip(trip_id),
    created_order INTEGER NOT NULL UNIQUE,
    notes        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS trip_ticket (
    trip_id     TEXT NOT NULL REFERENCES trip(trip_id),
    role        TEXT NOT NULL CHECK (role IN ('IN','OUT')),
    ticket_id   TEXT NOT NULL REFERENCES ticket(ticket_id),
    product_seq INTEGER NOT NULL,
    PRIMARY KEY (trip_id, role),
    UNIQUE (trip_id, ticket_id)
);

CREATE TABLE IF NOT EXISTS confirmed_ticket (
    ticket_id TEXT PRIMARY KEY REFERENCES ticket(ticket_id),
    trip_id   TEXT NOT NULL REFERENCES trip(trip_id)
);

CREATE TABLE IF NOT EXISTS evaluation (
    eval_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id          TEXT NOT NULL REFERENCES trip(trip_id),
    predecessor_eval INTEGER REFERENCES evaluation(eval_id),
    predecessor_trip TEXT,
    kind             TEXT NOT NULL,
    feasible         INTEGER NOT NULL,
    direction_ok     INTEGER NOT NULL,
    net_lo           INTEGER,
    net_hi           INTEGER,
    flow_lo          INTEGER,
    flow_hi          INTEGER,
    reason           TEXT NOT NULL,
    product_in       TEXT NOT NULL,
    product_out      TEXT NOT NULL,
    seq_in           INTEGER NOT NULL,
    seq_out          INTEGER NOT NULL,
    snapshot_in      TEXT NOT NULL,
    snapshot_out     TEXT NOT NULL,
    detail           TEXT NOT NULL,
    superseded_by    INTEGER REFERENCES evaluation(eval_id)
);
CREATE INDEX IF NOT EXISTS evaluation_trip ON evaluation(trip_id);

CREATE TABLE IF NOT EXISTS external_product (
    product_id   TEXT PRIMARY KEY,
    snapshot_seq INTEGER NOT NULL,
    tractor      TEXT NOT NULL,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS external_lookup ON external_product(tractor, snapshot_seq);
"""


class WarehouseError(ValueError):
    """仓库输入或状态错误。"""


class TicketRejected(WarehouseError):
    """票面违反物理或容量约束。"""


class TicketBusy(WarehouseError):
    """票已被另一次已确认运输占用。"""


def _as_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise WarehouseError(f"{name} must be an integer")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise WarehouseError(f"{name} must be an integer") from exc


def _bare(eid: str) -> str:
    return eid.split(":", 1)[1]


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    tractor: str
    catalog_seq: int
    ts: int
    direction: str
    gross: int
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "tractor": self.tractor,
            "catalog_seq": self.catalog_seq,
            "ts": self.ts,
            "direction": self.direction,
            "gross": self.gross,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class EntityInterval:
    entity_id: str
    label: str
    lo: int
    hi: int

    @property
    def width(self) -> int:
        return self.hi - self.lo

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "label": self.label,
            "lo": self.lo,
            "hi": self.hi,
            "width": self.width,
        }


@dataclass(frozen=True)
class TicketDerivation:
    """单票货重推导：为什么是这个区间、哪些实体贡献了多少不确定性。"""

    ticket_id: str
    direction: str
    gross: int
    product_seq: int
    product: Product
    entities: tuple[EntityInterval, ...]
    tare_lo: int
    tare_hi: int
    goods_lo: int
    goods_hi: int
    feasible: bool
    reason: str
    rationale: str

    def to_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "direction": self.direction,
            "gross": self.gross,
            "product_seq": self.product_seq,
            "product": self.product.to_dict(),
            "entities": [e.to_dict() for e in self.entities],
            "tare": [self.tare_lo, self.tare_hi],
            "goods": [self.goods_lo, self.goods_hi],
            "feasible": self.feasible,
            "reason": self.reason,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class EntityContribution:
    """净装卸量中某台实体的作用。"""

    entity_id: str
    label: str
    role: str  # cancelled | fixed | uncertain
    coefficient: int
    lo: int
    hi: int
    width: int
    note: str

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "label": self.label,
            "role": self.role,
            "coefficient": self.coefficient,
            "lo": self.lo,
            "hi": self.hi,
            "width": self.width,
            "note": self.note,
        }


@dataclass
class PairEvaluation:
    trip_id: str
    kind: str
    status: str
    feasible: bool
    direction_ok: bool
    reason: str
    net_lo: int | None
    net_hi: int | None
    flow_lo: int | None
    flow_hi: int | None
    derived_in: TicketDerivation
    derived_out: TicketDerivation
    cancellation: str
    contributions: list[EntityContribution] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    predecessor_trip: str | None = None

    def to_dict(self) -> dict:
        return {
            "trip_id": self.trip_id,
            "kind": self.kind,
            "status": self.status,
            "feasible": self.feasible,
            "direction_ok": self.direction_ok,
            "reason": self.reason,
            "net": None if self.net_lo is None else [self.net_lo, self.net_hi],
            "flow": None if self.flow_lo is None else [self.flow_lo, self.flow_hi],
            "ticket_in": self.derived_in.to_dict(),
            "ticket_out": self.derived_out.to_dict(),
            "cancellation": self.cancellation,
            "contributions": [c.to_dict() for c in self.contributions],
            "notes": list(self.notes),
            "predecessor_trip": self.predecessor_trip,
        }


# ----------------------------------------------------------------- 精确求解
def _solve_real(
    bounds: Sequence[tuple[int, int]],
    rows: Sequence[tuple[Sequence[Fraction], Fraction]],
    objective: Sequence[Fraction],
) -> tuple[bool, Fraction | None, Fraction | None, list[Fraction] | None]:
    """实数线性规划（精确有理数），供整数分支定界做松弛界。

    盒约束 + ``a·x <= b`` 的有界多面体，枚举 n 个紧约束子集求全部顶点。
    返回 ``(feasible, obj_min, obj_max, argmin_point)``。
    """
    n = len(bounds)
    constraints: list[tuple[list[Fraction], Fraction]] = []
    for i, (lo, hi) in enumerate(bounds):
        row = [Fraction(0)] * n
        row[i] = Fraction(-1)
        constraints.append((row, Fraction(-lo)))
        row = [Fraction(0)] * n
        row[i] = Fraction(1)
        constraints.append((row, Fraction(hi)))
    for coefs, rhs in rows:
        if len(coefs) != n:
            raise WarehouseError("constraint width mismatch")
        constraints.append(([Fraction(c) for c in coefs], Fraction(rhs)))

    def solve_subset(indices: Sequence[int]) -> list[Fraction] | None:
        mat = [
            [constraints[idx][0][j] for j in range(n)] + [constraints[idx][1]]
            for idx in indices
        ]
        for col in range(n):
            pivot = next((r for r in range(col, n) if mat[r][col] != 0), None)
            if pivot is None:
                return None
            mat[col], mat[pivot] = mat[pivot], mat[col]
            pv = mat[col][col]
            mat[col] = [v / pv for v in mat[col]]
            for r in range(n):
                if r != col and mat[r][col] != 0:
                    factor = mat[r][col]
                    mat[r] = [a - factor * b for a, b in zip(mat[r], mat[col])]
        return [mat[i][n] for i in range(n)]

    def satisfies(x: Sequence[Fraction]) -> bool:
        return all(
            sum((a * b for a, b in zip(coefs, x)), Fraction(0)) <= rhs
            for coefs, rhs in constraints
        )

    best_min: tuple[Fraction, list[Fraction]] | None = None
    best_max: Fraction | None = None
    for indices in itertools.combinations(range(len(constraints)), n):
        x = solve_subset(indices)
        if x is None or not satisfies(x):
            continue
        val = sum((c * v for c, v in zip(objective, x)), Fraction(0))
        if best_min is None or val < best_min[0]:
            best_min = (val, x)
        if best_max is None or val > best_max:
            best_max = val
    if best_min is None:
        return False, None, None, None
    return True, best_min[0], best_max, best_min[1]


def _solve_int_opt(
    bounds: list[tuple[int, int]],
    rows: Sequence[tuple[Sequence[Fraction], Fraction]],
    objective: Sequence[Fraction],
    maximize: bool,
) -> tuple[bool, int | None]:
    """整数分支定界；返回 ``(feasible, 原方向极值)``。"""
    sign = Fraction(-1) if maximize else Fraction(1)
    obj = [sign * c for c in objective]
    best_val: Fraction | None = None

    def dfs(lo_b: tuple[int, ...], hi_b: tuple[int, ...]) -> None:
        nonlocal best_val
        b = list(zip(lo_b, hi_b))
        feasible, lp_min, _, x_min = _solve_real(b, rows, obj)
        if not feasible or x_min is None:
            return
        if best_val is not None and lp_min > best_val:
            return
        frac_idx = next((i for i, v in enumerate(x_min) if v.denominator != 1), None)
        if frac_idx is None:
            val = sum((c * v for c, v in zip(obj, x_min)), Fraction(0))
            if best_val is None or val < best_val:
                best_val = val
            return
        v = x_min[frac_idx]
        floor_v = v.numerator // v.denominator
        ceil_v = -((-v.numerator) // v.denominator)
        lo, hi = b[frac_idx]
        if lo <= min(hi, floor_v):
            left_hi = list(hi_b)
            left_hi[frac_idx] = min(hi, floor_v)
            dfs(lo_b, tuple(left_hi))
        if max(lo, ceil_v) <= hi:
            right_lo = list(lo_b)
            right_lo[frac_idx] = max(lo, ceil_v)
            dfs(tuple(right_lo), hi_b)

    dfs(tuple(lo for lo, _ in bounds), tuple(hi for _, hi in bounds))
    if best_val is None:
        return False, None
    # best_val 是 min(sign·objective·x)；原方向极值 = sign⁻¹ · best_val = sign·best_val。
    return True, int(sign * best_val)


def _solve_int(
    bounds: Sequence[tuple[int, int]],
    rows: Sequence[tuple[Sequence[Fraction], Fraction]],
    objective: Sequence[Fraction],
) -> tuple[bool, int | None, int | None]:
    """整数可行域上的最小/最大。"""
    ok_lo, lo = _solve_int_opt(list(bounds), rows, objective, maximize=False)
    if not ok_lo:
        return False, None, None
    ok_hi, hi = _solve_int_opt(list(bounds), rows, objective, maximize=True)
    if not ok_hi:
        return False, None, None
    return True, lo, hi


# --------------------------------------------------------------------- 仓库
class Warehouse:
    """过磅事实仓。``catalog`` 可为 ``None``（只用外部快照）。"""

    def __init__(self, catalog: Catalog | None = None, path: str = ":memory:") -> None:
        self.catalog = catalog
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None
        self.conn.executescript(SCHEMA)

    # ---------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Warehouse":
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

    # ---------------------------------------------------------------- tickets
    def add_ticket(self, ticket: dict | Ticket) -> Ticket:
        if isinstance(ticket, Ticket):
            t = ticket
        else:
            t = Ticket(
                ticket_id=str(ticket["ticket_id"]),
                tractor=str(ticket["tractor"]),
                catalog_seq=_as_int(ticket["catalog_seq"], "catalog_seq"),
                ts=_as_int(ticket["ts"], "ts"),
                direction=str(ticket["direction"]).upper(),
                gross=_as_int(ticket["gross"], "gross"),
                payload=dict(ticket.get("payload", {})),
            )
        if not t.ticket_id.strip():
            raise WarehouseError("ticket_id must be non-empty")
        if t.direction not in (IN, OUT):
            raise WarehouseError(f"direction must be IN/OUT, got {t.direction!r}")
        if t.catalog_seq < 0:
            raise WarehouseError("catalog_seq must be >= 0")
        if t.gross < 0:
            raise WarehouseError("gross must be >= 0")
        payload = json.dumps(t.to_dict(), sort_keys=True, separators=(",", ":"))
        with self.transaction() as c:
            row = c.execute(
                "SELECT payload FROM ticket WHERE ticket_id=?", (t.ticket_id,)
            ).fetchone()
            if row is not None:
                if row["payload"] == payload:
                    return t
                raise WarehouseError(f"ticket {t.ticket_id} exists with different data")
            n = c.execute("SELECT COUNT(*) AS n FROM ticket").fetchone()["n"]
            if n >= MAX_TICKETS:
                raise WarehouseError(f"ticket limit {MAX_TICKETS} exceeded")
            c.execute(
                "INSERT INTO ticket(ticket_id, tractor, catalog_seq, ts, direction, gross, payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (t.ticket_id, t.tractor, t.catalog_seq, t.ts, t.direction, t.gross, payload),
            )
        return t

    def add_tickets(self, tickets: Iterable[dict | Ticket]) -> list[Ticket]:
        return [self.add_ticket(t) for t in tickets]

    def ticket(self, ticket_id: str) -> Ticket:
        row = self.conn.execute(
            "SELECT * FROM ticket WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise WarehouseError(f"unknown ticket {ticket_id}")
        return Ticket(
            ticket_id=row["ticket_id"],
            tractor=row["tractor"],
            catalog_seq=int(row["catalog_seq"]),
            ts=int(row["ts"]),
            direction=row["direction"],
            gross=int(row["gross"]),
            payload=json.loads(row["payload"]),
        )

    def tickets(self) -> list[Ticket]:
        return [
            self.ticket(r["ticket_id"])
            for r in self.conn.execute("SELECT ticket_id FROM ticket ORDER BY ticket_id")
        ]

    # ------------------------------------------------------- external snapshots
    def register_snapshot(self, snapshot: Snapshot | dict | str) -> Product:
        """导入外部同格式组合快照（历史产品，不得由当前挂接推导）。"""
        if isinstance(snapshot, str):
            snap = Snapshot.from_json(snapshot)
        elif isinstance(snapshot, dict):
            snap = Snapshot.from_dict(snapshot)
        else:
            snap = snapshot
        if not isinstance(snap, Snapshot):
            raise WarehouseError("not a composition snapshot")
        payload = snap.to_json()
        tractor = _bare(snap.product.tractor_id)
        with self.transaction() as c:
            row = c.execute(
                "SELECT payload FROM external_product WHERE product_id=?",
                (snap.product.product_id,),
            ).fetchone()
            if row is not None:
                if row["payload"] != payload:
                    raise WarehouseError(
                        f"external product {snap.product.product_id} conflicts with stored snapshot"
                    )
                return snap.product
            c.execute(
                "INSERT INTO external_product(product_id, snapshot_seq, tractor, payload) "
                "VALUES (?,?,?,?)",
                (snap.product.product_id, snap.seq, tractor, payload),
            )
        return snap.product

    def _external_product(self, seq: int, tractor: str) -> Product | None:
        row = self.conn.execute(
            "SELECT payload FROM external_product WHERE tractor=? AND snapshot_seq<=? "
            "ORDER BY snapshot_seq DESC, rowid DESC LIMIT 1",
            (tractor, seq),
        ).fetchone()
        if row is None:
            return None
        return Snapshot.from_json(row["payload"]).product

    def _product_for(self, seq: int, tractor: str) -> Product:
        """只按票面序号取公开组合产品；绝不用当前挂接回填历史。"""
        if self.catalog is not None:
            try:
                return self.catalog.product_at(seq, tractor)
            except CatalogError:
                pass
        product = self._external_product(seq, tractor)
        if product is None:
            raise WarehouseError(
                f"no composition product for tractor {tractor} at catalog seq {seq} "
                f"({REASON_NO_PRODUCT})"
            )
        return product

    # ---------------------------------------------------------------- derivation
    @staticmethod
    def _derive_with_product(t: Ticket, product: Product, seq: int) -> TicketDerivation:
        entities = tuple(
            EntityInterval(
                entity_id=m.entity_id, label=_bare(m.entity_id), lo=m.lo, hi=m.hi
            )
            for m in product.masses
        )
        tare_lo = sum(e.lo for e in entities)
        tare_hi = sum(e.hi for e in entities)
        goods_lo = max(0, t.gross - tare_hi)
        goods_hi = t.gross - tare_lo
        feasible = goods_hi >= 0
        if feasible:
            rationale = (
                f"gross {t.gross} kg - tare [{tare_lo},{tare_hi}] kg "
                f"= goods [{goods_lo},{goods_hi}] kg"
            )
            reason = REASON_OK
        else:
            rationale = (
                f"gross {t.gross} kg < minimum tare {tare_lo} kg; "
                f"goods upper bound {goods_hi} kg is negative"
            )
            reason = REASON_TICKET_INFEASIBLE
        return TicketDerivation(
            ticket_id=t.ticket_id, direction=t.direction, gross=t.gross,
            product_seq=seq, product=product, entities=entities,
            tare_lo=tare_lo, tare_hi=tare_hi, goods_lo=goods_lo, goods_hi=goods_hi,
            feasible=feasible, reason=reason, rationale=rationale,
        )

    def derive_ticket(
        self, ticket: Ticket | str, *, product_seq: int | None = None
    ) -> TicketDerivation:
        t = self.ticket(ticket) if isinstance(ticket, str) else ticket
        seq = t.catalog_seq if product_seq is None else int(product_seq)
        product = self._product_for(seq, t.tractor)
        return self._derive_with_product(t, product, seq)

    def _resolve_products(
        self,
        product_seqs: dict[str, int] | None,
        tin: Ticket,
        tout: Ticket,
    ) -> tuple[Product, Product]:
        """决定两张票引用哪份公开产品。

        * 未给 ``product_seqs``：按票面 ``catalog_seq`` 取当时的 live 产品。
        * 给了 ``product_seqs``：按给定序号取当前 live 产品 (用于按最新
          目录状态重评)。
        """
        if not product_seqs:
            return (
                self._product_for(tin.catalog_seq, tin.tractor),
                self._product_for(tout.catalog_seq, tout.tractor),
            )
        seq_in = int(product_seqs.get(IN, tin.catalog_seq))
        seq_out = int(product_seqs.get(OUT, tout.catalog_seq))
        return (
            self._product_for(seq_in, tin.tractor),
            self._product_for(seq_out, tout.tractor),
        )

    # ---------------------------------------------------------------- preview
    def preview(
        self,
        kind: str,
        ticket_in: str,
        ticket_out: str,
        *,
        product_seqs: dict[str, int] | None = None,
        notes: str = "",
    ) -> PairEvaluation:
        """建立一张 DRAFT 运输（可随后 ``confirm``）。"""
        return self._create_trip(
            kind, ticket_in, ticket_out,
            product_seqs=product_seqs, notes=notes,
            predecessor=None, status=STATUS_DRAFT,
        )

    def _create_trip(
        self,
        kind: str,
        ticket_in: str,
        ticket_out: str,
        *,
        product_seqs: dict[str, int] | None,
        notes: str,
        predecessor: str | None,
        status: str | None,
        resolved: dict[str, tuple[int, Product]] | None = None,
    ) -> PairEvaluation:
        kind = kind.upper()
        if kind not in (LOAD, UNLOAD):
            raise WarehouseError(f"kind must be LOAD/UNLOAD, got {kind!r}")
        if ticket_in == ticket_out:
            raise WarehouseError("a ticket cannot fill both IN and OUT roles")
        tin = self.ticket(ticket_in)
        tout = self.ticket(ticket_out)
        if tin.direction != IN or tout.direction != OUT:
            raise WarehouseError(
                f"trip roles need an IN and an OUT ticket "
                f"(got {tin.ticket_id}:{tin.direction}, {tout.ticket_id}:{tout.direction})"
            )
        if tin.tractor != tout.tractor:
            raise WarehouseError(
                f"tickets belong to different tractors "
                f"({tin.tractor}, {tout.tractor}); pass matching tickets")
        if resolved is not None:
            seq_in, pin = resolved[IN]
            seq_out, pout = resolved[OUT]
        else:
            seqs = dict(product_seqs or {})
            seq_in = int(seqs.get(IN, tin.catalog_seq))
            seq_out = int(seqs.get(OUT, tout.catalog_seq))
            pin, pout = self._resolve_products(seqs, tin, tout)
        din = self._derive_with_product(tin, pin, int(seq_in))
        dout = self._derive_with_product(tout, pout, int(seq_out))

        with self.transaction() as c:
            order = int(
                c.execute(
                    "SELECT COALESCE(MAX(created_order),0)+1 AS n FROM trip"
                ).fetchone()["n"]
            )
            if order > MAX_TRIPS:
                raise WarehouseError(f"trip limit {MAX_TRIPS} exceeded")
            trip_id = f"TRIP{order:04d}"
            pred_status: str | None = None
            if predecessor is not None:
                prow = c.execute(
                    "SELECT status FROM trip WHERE trip_id=?", (predecessor,)
                ).fetchone()
                if prow is None:
                    raise WarehouseError(f"unknown predecessor trip {predecessor}")
                pred_status = prow["status"]
                if pred_status in (STATUS_DRAFT, STATUS_REVIEW):
                    c.execute(
                        "UPDATE trip SET status=? WHERE trip_id=?",
                        (STATUS_SUPERSEDED, predecessor),
                    )
                    status = status or STATUS_DRAFT
                else:
                    status = status or STATUS_REVIEW
            if status is None:
                status = STATUS_DRAFT
            c.execute(
                "INSERT INTO trip(trip_id, kind, status, predecessor_trip, created_order, notes) "
                "VALUES (?,?,?,?,?,?)",
                (trip_id, kind, status, predecessor, order, notes),
            )
            c.execute(
                "INSERT INTO trip_ticket(trip_id, role, ticket_id, product_seq) VALUES (?,?,?,?)",
                (trip_id, IN, tin.ticket_id, seq_in),
            )
            c.execute(
                "INSERT INTO trip_ticket(trip_id, role, ticket_id, product_seq) VALUES (?,?,?,?)",
                (trip_id, OUT, tout.ticket_id, seq_out),
            )
            self._store_evaluation(c, trip_id, kind, din, dout, predecessor, pred_status)
        return self._load_evaluation(trip_id)

    # ---------------------------------------------------------------- evaluation
    def _evaluate_pair(
        self, kind: str, din: TicketDerivation, dout: TicketDerivation
    ) -> tuple:
        notes: list[str] = []
        if din.direction != IN or dout.direction != OUT:
            return (
                False, False, REASON_ROLE_MISMATCH, None, None, None, None,
                "role mismatch", [], notes,
            )
        if din.product.tractor_id != dout.product.tractor_id:
            return (
                False, False, REASON_TRACTOR_MISMATCH, None, None, None, None,
                "different tractors", [], notes,
            )
        tin = self.ticket(din.ticket_id)
        tout = self.ticket(dout.ticket_id)
        if tin.ts > tout.ts:
            notes.append(
                f"IN time {tin.ts} is later than OUT time {tout.ts}; trip rejected"
            )
            return (
                False, False, REASON_TIME_ORDER, None, None, None, None,
                "time order", [], notes,
            )
        if not din.feasible or not dout.feasible:
            notes.append("at least one ticket violates goods >= 0")
            return (
                False, False, REASON_TICKET_INFEASIBLE, None, None, None, None,
                "ticket infeasible", [], notes,
            )
        if din.product.identity() == dout.product.identity():
            return self._evaluate_cancellation(kind, din, dout, notes)
        return self._evaluate_change(kind, din, dout, notes)

    def _evaluate_cancellation(
        self, kind: str, din: TicketDerivation, dout: TicketDerivation, notes: list[str]
    ) -> tuple:
        # 同一组合 = 同一组实体；空车质量是同一未知量，必须存在共同赋值。
        in_masses = {m.entity_id: m for m in din.product.masses}
        out_masses = {m.entity_id: m for m in dout.product.masses}
        if set(in_masses) != set(out_masses):
            return (
                False, False, REASON_MASS_INTERVAL_CONFLICT, None, None, None, None,
                "same identity but different entity sets", [], notes,
            )
        overlap: dict[str, tuple[int, int]] = {}
        for eid, a in in_masses.items():
            b = out_masses[eid]
            lo, hi = max(a.lo, b.lo), min(a.hi, b.hi)
            if hi < lo:
                return (
                    False, False, REASON_MASS_INTERVAL_CONFLICT, None, None, None, None,
                    "same identity but disjoint mass intervals",
                    [
                        EntityContribution(
                            entity_id=e.entity_id, label=e.label, role="fixed",
                            coefficient=0, lo=e.lo, hi=e.hi, width=e.width,
                            note="mass intervals are disjoint between the two tickets",
                        )
                        for e in din.entities
                    ],
                    notes,
                )
            overlap[eid] = (lo, hi)
        combined_lo = sum(lo for lo, _ in overlap.values())
        if combined_lo > min(din.gross, dout.gross):
            notes.append(
                f"shared minimum tare {combined_lo} kg exceeds "
                f"min(gross_in,gross_out)={min(din.gross, dout.gross)} kg"
            )
            return (
                False, False, REASON_TICKET_INFEASIBLE, None, None, None, None,
                "no shared tare satisfies both tickets", [], notes,
            )
        net = dout.gross - din.gross
        direction_ok = (net >= 0) if kind == LOAD else (net <= 0)
        if not direction_ok:
            notes.append(
                f"{kind} requires {'out >= in' if kind == LOAD else 'out <= in'} gross; "
                f"got in={din.gross}, out={dout.gross}"
            )
            return (
                False, False, REASON_DIRECTION_IMPOSSIBLE, None, None, None, None,
                "direction impossible", [], notes,
            )
        magnitude = abs(net)
        contributions = [
            EntityContribution(
                entity_id=e.entity_id, label=e.label, role="cancelled",
                coefficient=0, lo=overlap[e.entity_id][0], hi=overlap[e.entity_id][1],
                width=overlap[e.entity_id][1] - overlap[e.entity_id][0],
                note="same entity on both tickets: cancels exactly in the weight difference",
            )
            for e in din.entities
        ]
        notes.append(
            f"same composition {din.product.stable_identity()}: tare is one unknown and "
            f"cancels; net = |gross_out - gross_in| = {magnitude} kg exactly"
        )
        return (
            True, True, REASON_OK, magnitude, magnitude, net, net,
            "exact cancellation", contributions, notes,
        )

    def _evaluate_change(
        self, kind: str, din: TicketDerivation, dout: TicketDerivation, notes: list[str]
    ) -> tuple:
        # 变量：每个出现过的实体一份整数空车质量，取值 = 两票区间之交。
        entity_vars: dict[str, int] = {}
        bounds: list[tuple[int, int]] = []
        for p in (din.product, dout.product):
            for m in p.masses:
                idx = entity_vars.get(m.entity_id)
                if idx is None:
                    entity_vars[m.entity_id] = len(bounds)
                    bounds.append((m.lo, m.hi))
                else:
                    lo = max(bounds[idx][0], m.lo)
                    hi = min(bounds[idx][1], m.hi)
                    if hi < lo:
                        return (
                            False, False, REASON_MASS_INTERVAL_CONFLICT,
                            None, None, None, None,
                            "entity mass intervals conflict between tickets", [], notes,
                        )
                    bounds[idx] = (lo, hi)
        n_vars = len(bounds)

        def entity_row(product: Product) -> list[Fraction]:
            row = [Fraction(0)] * n_vars
            for m in product.masses:
                row[entity_vars[m.entity_id]] += 1
            return row

        row_in = entity_row(din.product)
        row_out = entity_row(dout.product)
        base_rows: list[tuple[list[Fraction], Fraction]] = [
            (row_in, Fraction(din.gross)),   # goods_in >= 0
            (row_out, Fraction(dout.gross)), # goods_out >= 0
        ]
        diff_e = [row_out[i] - row_in[i] for i in range(n_vars)]
        delta = dout.gross - din.gross
        # flow = goods_out - goods_in = delta - diff_e·e
        if kind == LOAD:
            dir_row = (list(diff_e), Fraction(delta))       # flow >= 0
        else:
            dir_row = ([-v for v in diff_e], Fraction(-delta))  # flow <= 0

        obj_diff = [Fraction(diff_e[i]) for i in range(n_vars)]
        base_feasible, _, _, _ = _solve_real(bounds, base_rows, obj_diff)
        if not base_feasible:
            return (
                False, False, REASON_TICKET_INFEASIBLE, None, None, None, None,
                "no common mass assignment satisfies both tickets' goods",
                [], notes,
            )
        feasible, obj_min, obj_max = _solve_int(
            bounds, base_rows + [dir_row], obj_diff
        )
        if not feasible:
            # 去掉方向约束后整数域是否仍可行？若是，说明是方向不可能；
            # 否则两张票的货重非负在本行程内无共同赋值。
            undirected, _, _ = _solve_int(bounds, base_rows, obj_diff)
            if undirected:
                notes.append("the direction constraint empties the feasible region")
                return (
                    False, False, REASON_DIRECTION_IMPOSSIBLE, None, None, None, None,
                    "direction impossible", [], notes,
                )
            notes.append(
                "no mass assignment satisfies both tickets' goods non-negativity"
            )
            return (
                False, False, REASON_TICKET_INFEASIBLE, None, None, None, None,
                "no common assignment", [], notes,
            )
        assert obj_min is not None and obj_max is not None
        flow_lo = delta - obj_max
        flow_hi = delta - obj_min
        if kind == LOAD:
            net_lo, net_hi = flow_lo, flow_hi
            # net = |flow| = flow = delta - diff_e·e  =>  实体系数 = -diff_e
            sign = -1
        else:
            net_lo, net_hi = -flow_hi, -flow_lo
            # net = |flow| = -flow = diff_e·e - delta  =>  实体系数 = +diff_e
            sign = 1

        # net = |flow| 的线性系数：LOAD -> -diff_e, UNLOAD -> +diff_e。
        coef_map: dict[str, int] = {}
        for m in din.product.masses:
            coef_map[m.entity_id] = coef_map.get(m.entity_id, 0) - 1
        for m in dout.product.masses:
            coef_map[m.entity_id] = coef_map.get(m.entity_id, 0) + 1
        contributions = []
        for eid, idx in entity_vars.items():
            lo, hi = bounds[idx]
            coef = sign * coef_map.get(eid, 0)
            if coef == 0:
                role = "cancelled"
                note = "shared entity: equal coefficients on both tickets cancel"
            elif hi == lo:
                role = "fixed"
                note = "single-valued; no uncertainty contribution"
            else:
                role = "uncertain"
                note = f"coefficient {coef}; interval width {hi - lo} kg"
            contributions.append(
                EntityContribution(
                    entity_id=eid, label=_bare(eid), role=role, coefficient=coef,
                    lo=lo, hi=hi, width=hi - lo, note=note,
                )
            )
        notes.append(
            "changed composition: shared entities cancel, distinct entities are "
            "jointly optimised over their integer mass intervals"
        )
        cancellation = (
            "partial cancellation"
            if any(c.role == "cancelled" for c in contributions)
            else "no cancellation"
        )
        return (
            True, True, REASON_OK, net_lo, net_hi, flow_lo, flow_hi,
            cancellation, contributions, notes,
        )

    def _store_evaluation(
        self,
        c: sqlite3.Connection,
        trip_id: str,
        kind: str,
        din: TicketDerivation,
        dout: TicketDerivation,
        predecessor: str | None,
        pred_status: str | None,
    ) -> int:
        (
            feasible, direction_ok, reason, net_lo, net_hi, flow_lo, flow_hi,
            cancellation, contributions, notes,
        ) = self._evaluate_pair(kind, din, dout)
        predecessor_eval = None
        if predecessor is not None:
            prow = c.execute(
                "SELECT eval_id FROM evaluation WHERE trip_id=? "
                "ORDER BY eval_id DESC LIMIT 1",
                (predecessor,),
            ).fetchone()
            if prow is not None:
                predecessor_eval = int(prow["eval_id"])
        detail = {
            "cancellation": cancellation,
            "contributions": [x.to_dict() for x in contributions],
            "notes": notes,
            "mass_check": {
                "in": [din.goods_lo, din.goods_hi],
                "out": [dout.goods_lo, dout.goods_hi],
            },
        }
        cur = c.execute(
            "INSERT INTO evaluation(trip_id, predecessor_eval, predecessor_trip, kind, "
            "feasible, direction_ok, net_lo, net_hi, flow_lo, flow_hi, reason, "
            "product_in, product_out, seq_in, seq_out, snapshot_in, snapshot_out, detail) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trip_id, predecessor_eval, predecessor, kind, int(feasible),
                int(direction_ok), net_lo, net_hi, flow_lo, flow_hi, reason,
                din.product.product_id, dout.product.product_id,
                din.product_seq, dout.product_seq,
                json.dumps(Snapshot(din.product_seq, din.product).to_dict(), sort_keys=True),
                json.dumps(Snapshot(dout.product_seq, dout.product).to_dict(), sort_keys=True),
                json.dumps(detail, sort_keys=True),
            ),
        )
        eval_id = int(cur.lastrowid)
        # 只有当旧运输本身已被取代 (草稿/复核) 时才标记旧评估；已确认运输
        # 的评估是历史事实，永不覆盖。
        if predecessor_eval is not None and pred_status in (STATUS_DRAFT, STATUS_REVIEW):
            c.execute(
                "UPDATE evaluation SET superseded_by=? WHERE eval_id=? AND superseded_by IS NULL",
                (eval_id, predecessor_eval),
            )
        return eval_id

    def _load_evaluation(self, trip_id: str) -> PairEvaluation:
        row = self.conn.execute(
            "SELECT * FROM evaluation WHERE trip_id=? ORDER BY eval_id DESC LIMIT 1",
            (trip_id,),
        ).fetchone()
        if row is None:
            raise WarehouseError(f"trip {trip_id} has no evaluation")
        trip = self.conn.execute("SELECT * FROM trip WHERE trip_id=?", (trip_id,)).fetchone()
        roles = {
            r["role"]: r
            for r in self.conn.execute(
                "SELECT role, ticket_id, product_seq FROM trip_ticket WHERE trip_id=?",
                (trip_id,),
            )
        }
        # 从存下的快照重建推导：评估不随后来目录状态变化。
        pin = Snapshot.from_json(row["snapshot_in"])
        pout = Snapshot.from_json(row["snapshot_out"])
        din = self._derive_with_product(
            self.ticket(roles[IN]["ticket_id"]), pin.product, int(row["seq_in"])
        )
        dout = self._derive_with_product(
            self.ticket(roles[OUT]["ticket_id"]), pout.product, int(row["seq_out"])
        )
        detail = json.loads(row["detail"])
        contributions = [EntityContribution(**x) for x in detail.get("contributions", [])]
        return PairEvaluation(
            trip_id=trip_id, kind=row["kind"], status=trip["status"],
            feasible=bool(row["feasible"]), direction_ok=bool(row["direction_ok"]),
            reason=row["reason"], net_lo=row["net_lo"], net_hi=row["net_hi"],
            flow_lo=row["flow_lo"], flow_hi=row["flow_hi"],
            derived_in=din, derived_out=dout,
            cancellation=detail.get("cancellation", ""),
            contributions=contributions, notes=detail.get("notes", []),
            predecessor_trip=trip["predecessor_trip"],
        )

    # ---------------------------------------------------------------- confirm
    def confirm(self, trip_id: str) -> PairEvaluation:
        with self.transaction() as c:
            trip = c.execute("SELECT * FROM trip WHERE trip_id=?", (trip_id,)).fetchone()
            if trip is None:
                raise WarehouseError(f"unknown trip {trip_id}")
            if trip["status"] == STATUS_CONFIRMED:
                return self._load_evaluation(trip_id)
            if trip["status"] != STATUS_DRAFT:
                raise WarehouseError(
                    f"trip {trip_id} status {trip['status']} cannot be confirmed"
                )
            ev = c.execute(
                "SELECT * FROM evaluation WHERE trip_id=? ORDER BY eval_id DESC LIMIT 1",
                (trip_id,),
            ).fetchone()
            if ev is None or not ev["feasible"]:
                reason = ev["reason"] if ev is not None else "no evaluation"
                raise TicketRejected(
                    f"trip {trip_id} is not physically feasible ({reason})"
                )
            roles = c.execute(
                "SELECT role, ticket_id FROM trip_ticket WHERE trip_id=?", (trip_id,)
            ).fetchall()
            if {r["role"] for r in roles} != {IN, OUT}:
                raise WarehouseError(f"trip {trip_id} needs exactly one IN and one OUT")
            for r in roles:
                busy = c.execute(
                    "SELECT trip_id FROM confirmed_ticket WHERE ticket_id=?",
                    (r["ticket_id"],),
                ).fetchone()
                if busy is not None:
                    raise TicketBusy(
                        f"ticket {r['ticket_id']} already used by confirmed trip "
                        f"{busy['trip_id']}"
                    )
            for r in roles:
                c.execute(
                    "INSERT INTO confirmed_ticket(ticket_id, trip_id) VALUES (?,?)",
                    (r["ticket_id"], trip_id),
                )
            c.execute("UPDATE trip SET status=? WHERE trip_id=?", (STATUS_CONFIRMED, trip_id))
        return self._load_evaluation(trip_id)

    # ---------------------------------------------------------------- re-evaluate
    def reevaluate(
        self,
        trip_id: str,
        *,
        catalog_seq: int | None = None,
        product_seqs: dict[str, int] | None = None,
        corrected_history: bool = False,
        notes: str = "",
    ) -> PairEvaluation:
        """目录更正后重评：新增带前序身份的运输/评估，绝不覆盖旧记录。

        三种模式（优先级从高到低）：

        * ``product_seqs``：显式指定每张票引用的目录序号，取该序号处的 live
          公开产品；
        * ``catalog_seq``：两票都改用该序号处的 live 公开产品；
        * ``corrected_history=True``：对原评估记录里的 *历史产品* 各取
          ``corrected_product``（同一 identity + attach_seq 的更正版），这是
          "目录迟到更正" 的正确语义；
        * 都不给：沿用原产品，只复制一份评估记录。
        """
        src = self.conn.execute("SELECT * FROM trip WHERE trip_id=?", (trip_id,)).fetchone()
        if src is None:
            raise WarehouseError(f"unknown trip {trip_id}")
        rows = {
            r["role"]: r
            for r in self.conn.execute(
                "SELECT role, ticket_id, product_seq FROM trip_ticket WHERE trip_id=?",
                (trip_id,),
            )
        }
        tin = self.ticket(rows[IN]["ticket_id"])
        tout = self.ticket(rows[OUT]["ticket_id"])
        prev_eval = self.conn.execute(
            "SELECT snapshot_in, snapshot_out, seq_in, seq_out FROM evaluation "
            "WHERE trip_id=? ORDER BY eval_id DESC LIMIT 1",
            (trip_id,),
        ).fetchone()
        if prev_eval is None:
            raise WarehouseError(f"trip {trip_id} has no evaluation to correct")
        pin_old = Snapshot.from_json(prev_eval["snapshot_in"]).product
        pout_old = Snapshot.from_json(prev_eval["snapshot_out"]).product

        if product_seqs:
            seqs = {
                IN: int(product_seqs.get(IN, tin.catalog_seq)),
                OUT: int(product_seqs.get(OUT, tout.catalog_seq)),
            }
            pin = self._product_for(seqs[IN], tin.tractor)
            pout = self._product_for(seqs[OUT], tout.tractor)
        elif catalog_seq is not None:
            seqs = {IN: int(catalog_seq), OUT: int(catalog_seq)}
            pin = self._product_for(seqs[IN], tin.tractor)
            pout = self._product_for(seqs[OUT], tout.tractor)
        elif corrected_history and self.catalog is not None:
            pin = self.catalog.corrected_product(pin_old)
            pout = self.catalog.corrected_product(pout_old)
            seqs = {IN: pin.action_seq, OUT: pout.action_seq}
        else:
            pin, pout = pin_old, pout_old
            seqs = {
                IN: int(prev_eval["seq_in"]),
                OUT: int(prev_eval["seq_out"]),
            }
        default_note = f"re-evaluated from {trip_id}"
        if corrected_history or catalog_seq is not None or product_seqs:
            default_note += " after catalogue/history correction"
        return self._create_trip(
            src["kind"], rows[IN]["ticket_id"], rows[OUT]["ticket_id"],
            product_seqs=None, notes=notes or default_note,
            predecessor=trip_id,
            status=STATUS_DRAFT if src["status"] == STATUS_DRAFT else None,
            resolved={IN: (seqs[IN], pin), OUT: (seqs[OUT], pout)},
        )

    def reopen(
        self,
        trip_id: str,
        *,
        catalog_seq: int | None = None,
        corrected_history: bool = True,
    ) -> PairEvaluation:
        """重开一条 (通常已确认) 运输：只产生带前序身份的复核结果。

        默认按目录里同一挂接的更正版产品重评；已确认运输与其票绑定不变。
        """
        return self.reevaluate(
            trip_id, catalog_seq=catalog_seq,
            corrected_history=corrected_history,
            notes=f"reopened {trip_id} after catalogue change",
        )

    # ---------------------------------------------------------------- queries
    def trip(self, trip_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM trip WHERE trip_id=?", (trip_id,)).fetchone()
        if row is None:
            raise WarehouseError(f"unknown trip {trip_id}")
        tickets = {
            r["role"]: {"ticket_id": r["ticket_id"], "product_seq": int(r["product_seq"])}
            for r in self.conn.execute(
                "SELECT role, ticket_id, product_seq FROM trip_ticket WHERE trip_id=?",
                (trip_id,),
            )
        }
        ev = self.conn.execute(
            "SELECT eval_id, feasible, direction_ok, reason, net_lo, net_hi, "
            "flow_lo, flow_hi, superseded_by FROM evaluation "
            "WHERE trip_id=? ORDER BY eval_id DESC LIMIT 1",
            (trip_id,),
        ).fetchone()
        return {
            "trip_id": row["trip_id"], "kind": row["kind"], "status": row["status"],
            "predecessor_trip": row["predecessor_trip"], "notes": row["notes"],
            "tickets": tickets, "evaluation": dict(ev) if ev else None,
        }

    def trips(self) -> list[dict]:
        return [
            self.trip(r["trip_id"])
            for r in self.conn.execute("SELECT trip_id FROM trip ORDER BY created_order")
        ]

    def evaluation(self, trip_id: str) -> PairEvaluation:
        return self._load_evaluation(trip_id)

    def _trip_family(self, trip_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT trip_id, predecessor_trip FROM trip"
        ).fetchall()
        pred = {r["trip_id"]: r["predecessor_trip"] for r in rows}
        if trip_id not in pred:
            raise WarehouseError(f"unknown trip {trip_id}")
        root = trip_id
        seen = {root}
        while pred[root] is not None:
            root = pred[root]
            if root in seen:
                raise WarehouseError("cycle in trip predecessors")
            seen.add(root)
        children: dict[str, list[str]] = {}
        for tid, p in pred.items():
            children.setdefault(p, []).append(tid)
        order: list[str] = []
        stack = [root]
        while stack:
            node = stack.pop()
            order.append(node)
            stack.extend(reversed(children.get(node, [])))
        return order

    def evaluation_history(self, trip_id: str) -> list[dict]:
        """该运输族 (含前序与后继重评) 的全部评估记录。"""
        family = self._trip_family(trip_id)
        placeholders = ",".join("?" for _ in family)
        rows = self.conn.execute(
            f"SELECT e.*, t.status AS trip_status, t.created_order AS trip_order "
            f"FROM evaluation e JOIN trip t ON t.trip_id=e.trip_id "
            f"WHERE e.trip_id IN ({placeholders}) ORDER BY e.eval_id",
            family,
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"])
            out.append(d)
        return out

    def ticket_usage(self, ticket_id: str) -> dict:
        rows = self.conn.execute(
            "SELECT t.trip_id, t.status, tt.role FROM trip_ticket tt "
            "JOIN trip t ON t.trip_id=tt.trip_id WHERE tt.ticket_id=? "
            "ORDER BY t.created_order",
            (ticket_id,),
        ).fetchall()
        confirmed = self.conn.execute(
            "SELECT trip_id FROM confirmed_ticket WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        return {
            "ticket_id": ticket_id,
            "confirmed_trip": confirmed["trip_id"] if confirmed else None,
            "trips": [dict(r) for r in rows],
        }

    def trips_for_ticket(self, ticket_id: str) -> list[str]:
        return [
            r["trip_id"]
            for r in self.conn.execute(
                "SELECT tt.trip_id FROM trip_ticket tt JOIN trip t ON t.trip_id=tt.trip_id "
                "WHERE tt.ticket_id=? ORDER BY t.created_order",
                (ticket_id,),
            )
        ]

    def explain_ticket(self, ticket_id: str, *, product_seq: int | None = None) -> dict:
        return self.derive_ticket(ticket_id, product_seq=product_seq).to_dict()

    def explain_trip(self, trip_id: str) -> dict:
        ev = self._load_evaluation(trip_id)
        out = ev.to_dict()
        out["ticket_usage"] = {
            role: self.ticket_usage(
                self.conn.execute(
                    "SELECT ticket_id FROM trip_ticket WHERE trip_id=? AND role=?",
                    (trip_id, role),
                ).fetchone()["ticket_id"]
            )
            for role in (IN, OUT)
        }
        return out

    # ---------------------------------------------------------------- reconciliation
    def reconcile(self) -> list[str]:
        """核对入口：返回不一致列表（空 = 通过）。"""
        errs: list[str] = []

        # 1. 确认运输：角色 = 方向，两票同车、时间有序，且评估可行。
        for tr in self.conn.execute("SELECT trip_id FROM trip WHERE status='CONFIRMED'"):
            tid = tr["trip_id"]
            rows = self.conn.execute(
                "SELECT tt.role, t.ticket_id, t.ts, t.tractor, t.direction "
                "FROM trip_ticket tt JOIN ticket t ON t.ticket_id=tt.ticket_id "
                "WHERE tt.trip_id=?",
                (tid,),
            ).fetchall()
            roles = {r["role"]: r for r in rows}
            if set(roles) != {IN, OUT}:
                errs.append(f"confirmed trip {tid} lacks both tickets")
                continue
            if roles[IN]["direction"] != IN or roles[OUT]["direction"] != OUT:
                errs.append(f"confirmed trip {tid} has a ticket with the wrong direction")
            if roles[IN]["tractor"] != roles[OUT]["tractor"]:
                errs.append(f"confirmed trip {tid} mixes tractors")
            if roles[IN]["ts"] > roles[OUT]["ts"]:
                errs.append(f"confirmed trip {tid} has IN after OUT")
            ev = self.conn.execute(
                "SELECT feasible FROM evaluation WHERE trip_id=? "
                "ORDER BY eval_id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            if ev is None or not ev["feasible"]:
                errs.append(f"confirmed trip {tid} has no feasible evaluation")

        # 2. 确认占票唯一；每张确认票恰好绑定一个确认运输。
        for r in self.conn.execute(
            "SELECT ct.ticket_id, ct.trip_id, t.status AS trip_status "
            "FROM confirmed_ticket ct JOIN trip t ON t.trip_id=ct.trip_id"
        ):
            if r["trip_status"] != STATUS_CONFIRMED:
                errs.append(
                    f"confirmed_ticket {r['ticket_id']} points to non-confirmed trip"
                )
        for r in self.conn.execute(
            "SELECT tt.ticket_id, tt.trip_id FROM trip_ticket tt "
            "JOIN trip t ON t.trip_id=tt.trip_id WHERE t.status='CONFIRMED' "
            "AND NOT EXISTS (SELECT 1 FROM confirmed_ticket ct "
            "WHERE ct.ticket_id=tt.ticket_id AND ct.trip_id=tt.trip_id)"
        ):
            errs.append(f"ticket {r['ticket_id']} of confirmed trip {r['trip_id']} is unclaimed")

        # 3. 每条评估可重算：区间极值、精确抵消、方向。
        for r in self.conn.execute("SELECT trip_id FROM evaluation"):
            try:
                ev = self._load_evaluation(r["trip_id"])
            except WarehouseError as exc:
                errs.append(f"evaluation of {r['trip_id']} unreadable: {exc}")
                continue
            recomputed = self._evaluate_pair(ev.kind, ev.derived_in, ev.derived_out)
            stored = (
                ev.feasible, ev.direction_ok, ev.reason, ev.net_lo, ev.net_hi,
                ev.flow_lo, ev.flow_hi,
            )
            fresh = (
                recomputed[0], recomputed[1], recomputed[2], recomputed[3],
                recomputed[4], recomputed[5], recomputed[6],
            )
            if stored != fresh:
                errs.append(
                    f"trip {ev.trip_id} evaluation not reproducible: {stored} vs {fresh}"
                )

        # 4. 前序身份可追溯。
        for r in self.conn.execute(
            "SELECT eval_id, predecessor_trip FROM evaluation "
            "WHERE predecessor_trip IS NOT NULL"
        ):
            if self.conn.execute(
                "SELECT 1 FROM trip WHERE trip_id=?", (r["predecessor_trip"],)
            ).fetchone() is None:
                errs.append(
                    f"evaluation {r['eval_id']} references missing predecessor trip"
                )

        if self.catalog is not None:
            errs.extend(self.catalog.verify())
        return errs

    def verify(self) -> list[str]:
        return self.reconcile()
