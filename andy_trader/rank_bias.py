"""Compare agent conviction by displayed list position."""

from __future__ import annotations

import sqlite3


def selection_by_rank(connection: sqlite3.Connection) -> list[tuple[str, int, int, float]]:
    rows = connection.execute("""
        SELECT list_arm, list_position, COUNT(*) AS decisions,
               SUM(CASE WHEN p_up <= 0.45 OR p_up >= 0.55 THEN 1 ELSE 0 END) AS convictions
        FROM llm_decisions
        WHERE status NOT IN ('replay', 'ablation')
          AND list_arm IN ('ranked', 'shuffled') AND list_position IS NOT NULL
        GROUP BY list_arm, list_position
        ORDER BY list_arm, list_position
    """).fetchall()
    return [(arm, position, decisions, convictions / decisions)
            for arm, position, decisions, convictions in rows]


def boundary_ratio(connection: sqlite3.Connection, arm: str, cut: int = 3) -> float | None:
    positions = {position: (decisions, share)
                 for row_arm, position, decisions, share in selection_by_rank(connection)
                 if row_arm == arm}
    before = positions.get(cut)
    after = positions.get(cut + 1)
    if before is None or after is None or before[0] < 10 or after[0] < 10:
        return None
    if before[1] > 0 and after[1] == 0:
        return float("inf")
    if before[1] == 0 and after[1] == 0:
        return None
    return before[1] / after[1]


def main() -> None:
    from andy_trader.llm_ledger import initialize_ledger
    from andy_trader.store import connect, default_database_path

    with connect(default_database_path()) as connection:
        initialize_ledger(connection)
        print("arm       position  decisions  conviction share")
        for arm, position, decisions, share in selection_by_rank(connection):
            print(f"{arm:<10}{position:>8}{decisions:>11}{share:>18.1%}")
        cut = 3
        ranked = boundary_ratio(connection, "ranked", cut=cut)
        shuffled = boundary_ratio(connection, "shuffled", cut=cut)
        for arm, ratio in (("ranked", ranked), ("shuffled", shuffled)):
            if ratio == float("inf"):
                print(f"{arm}: no conviction calls at position {cut + 1}")
        if ranked is None or shuffled is None:
            print("There are too few decisions to compare the arms at the cut.")
        elif ranked > shuffled:
            print("The ranked arm drops at the cut more than the shuffled arm does.")
        else:
            print("The ranked arm does not drop at the cut more than the shuffled arm does.")


if __name__ == "__main__":
    main()
