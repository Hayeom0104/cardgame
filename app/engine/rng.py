"""§16.4 — the RNG operation journal.

> v6.1 claimed that replaying a transition with the persisted `rng_counter`
> reproduces the original roll. **That is false.** A crash after the counter
> advanced but before the outcome was stored makes a replay read the *next*
> value, not the original one.

The fix is to journal the *result*, keyed by a deterministic `op_key`, in the
same transaction as the state mutation. A replay keyed by `op_key` returns the
original result regardless of where the crash landed.

Gacha does **not** use this journal — it happens outside any run and uses a
server-controlled per-transaction seed (§5.9).
"""

from __future__ import annotations

import hashlib
import json
import random
from typing import Any, Callable, Sequence

from app.db.connection import Database, utcnow


class JournaledRng:
    """Draws for one run, replayed by `op_key`.

    Every draw goes through :meth:`draw`. The caller supplies a pure function of
    a `random.Random`; its return value is what gets journaled, so a replay
    reproduces the *outcome* rather than re-running the derivation.
    """

    def __init__(self, db: Database, run_id: int, seed: int):
        self.db = db
        self.run_id = run_id
        self.seed = seed

    # -- core ----------------------------------------------------------
    def draw(self, op_key: str, fn: Callable[[random.Random], Any]) -> Any:
        """Return the journaled result for `op_key`, drawing it if it is new.

        The result, the counter advance and the caller's state change commit
        together — the caller is expected to already hold a transaction, or to
        accept this method's own.
        """
        row = self.db.one(
            "SELECT result_json FROM rng_operations WHERE run_id = ? AND op_key = ?",
            (self.run_id, op_key),
        )
        if row is not None:
            return json.loads(row["result_json"])  # REPLAY — same value

        with self.db.tx() as conn:
            # Re-check inside the transaction: a concurrent handler may have
            # journaled this key between the read above and the write below.
            row = conn.execute(
                "SELECT result_json FROM rng_operations WHERE run_id = ? AND op_key = ?",
                (self.run_id, op_key),
            ).fetchone()
            if row is not None:
                return json.loads(row["result_json"])

            counter_row = conn.execute(
                "SELECT rng_counter FROM runs WHERE run_id = ?", (self.run_id,)
            ).fetchone()
            counter = counter_row["rng_counter"] if counter_row else 0

            result = fn(self._stream(counter, op_key))
            conn.execute(
                "INSERT INTO rng_operations "
                "(run_id, op_key, rng_counter_before, result_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (self.run_id, op_key, counter, json.dumps(result, ensure_ascii=False), utcnow()),
            )
            conn.execute(
                "UPDATE runs SET rng_counter = rng_counter + 1 WHERE run_id = ?",
                (self.run_id,),
            )
        return result

    def _stream(self, counter: int, op_key: str) -> random.Random:
        """A deterministic substream for (seed, counter, op_key).

        Mixing `op_key` in means two different operations at the same counter
        value cannot alias, which matters because the counter advances per
        journaled draw rather than per consumed random number.
        """
        digest = hashlib.sha256(
            f"{self.seed}:{counter}:{op_key}".encode("utf-8")
        ).digest()
        return random.Random(int.from_bytes(digest, "big"))

    # -- typed helpers -------------------------------------------------
    def randint(self, op_key: str, low: int, high: int) -> int:
        """Inclusive on both ends, matching §2.7.3's `0 .. n`."""
        return int(self.draw(op_key, lambda rng: rng.randint(low, high)))

    def chance(self, op_key: str, probability: float) -> bool:
        return bool(self.draw(op_key, lambda rng: rng.random() < probability))

    def choice(self, op_key: str, options: Sequence[Any]) -> Any:
        if not options:
            raise ValueError("choice() from an empty sequence")
        index = int(self.draw(op_key, lambda rng: rng.randrange(len(options))))
        return options[index]

    def weighted_choice(self, op_key: str, options: Sequence[Any],
                        weights: Sequence[float]) -> Any:
        if len(options) != len(weights):
            raise ValueError("options and weights differ in length")
        total = float(sum(weights))
        if total <= 0:
            raise ValueError("weights must sum to a positive value")

        def _pick(rng: random.Random) -> int:
            roll = rng.random() * total
            upto = 0.0
            for index, weight in enumerate(weights):
                upto += weight
                if roll < upto:
                    return index
            return len(options) - 1

        return options[int(self.draw(op_key, _pick))]

    def sample(self, op_key: str, population: Sequence[Any], count: int) -> list[Any]:
        """`count` distinct items. Journaled as indices so replay is stable."""
        count = max(0, min(count, len(population)))
        indices = self.draw(
            op_key, lambda rng: rng.sample(range(len(population)), count)
        )
        return [population[i] for i in indices]

    def shuffled(self, op_key: str, items: Sequence[Any]) -> list[Any]:
        def _order(rng: random.Random) -> list[int]:
            order = list(range(len(items)))
            rng.shuffle(order)
            return order

        return [items[i] for i in self.draw(op_key, _order)]


# -- §16.4 canonical op_key builders -------------------------------------
# Keys are DERIVED DETERMINISTICALLY from the game event, never from a counter.

def key_map_gen() -> str:
    return "map:gen"


def key_encounter(node_index: int) -> str:
    return f"node:{node_index}:encounter"


def key_plan(battle_id: int, round_no: int, enemy_unit_id: int) -> str:
    return f"battle:{battle_id}:r{round_no}:plan:{enemy_unit_id}"


def key_shuffle(battle_id: int, round_no: int, party_slot: int) -> str:
    return f"battle:{battle_id}:r{round_no}:shuffle:{party_slot}"


def key_draw(battle_id: int, round_no: int, unit_id: int) -> str:
    return f"battle:{battle_id}:r{round_no}:draw:{unit_id}"


def key_curse_insert(party_slot: int, seq) -> str:
    """`seq` must be unique per insertion within the run.

    Callers inside a battle build it from (battle, round, acting unit, effect
    index) rather than the effect index alone — two curses inserted in
    different rounds would otherwise share an op_key and the second would
    replay the first's position.
    """
    return f"deck:{party_slot}:curse_insert:{seq}"


def key_settle_keepset() -> str:
    return "settle:keepset"


def key_counter(battle_id: int, round_no: int, seq: int) -> str:
    """§2.13 reactive-ability condition rolls (e.g. `random_chance`)."""
    return f"battle:{battle_id}:r{round_no}:counter:{seq}"


def key_crit(battle_id: int, round_no: int, seq: int) -> str:
    """§10.4.3 `crit_chance` rolls on `deal_damage` / `deal_flat_damage`."""
    return f"battle:{battle_id}:r{round_no}:crit:{seq}"


def next_journaled_seq(db: Database, prefix: str) -> int:
    """How many journal rows already exist under `prefix`, plus one.

    A lazy way to allocate a fresh, unique numeric suffix for an op_key only
    at the moment a roll is actually needed, without a dedicated counter
    column anywhere. Safe because turn processing within one battle is
    strictly sequential — §16.7's CAS is what guarantees that — so nothing
    else can be allocating under the same prefix concurrently.
    """
    row = db.one("SELECT COUNT(*) AS n FROM rng_operations WHERE op_key LIKE ?",
                (f"{prefix}%",))
    return int(row["n"]) + 1
