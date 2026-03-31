"""Graph query helpers for coin relationship traversal."""

from __future__ import annotations

import sqlite3

from cointoss.models.ontology import CoinRelationship, RelationshipType


def get_coin_relationships(conn: sqlite3.Connection, coin_id: str) -> list[dict[str, object]]:
    """Return all relationships for a coin as a list of annotated dicts.

    Each dict has keys: coin, relationship, direction, confidence, source, notes.
    Outgoing edges (coin_from == coin_id) have direction "outgoing".
    Incoming edges (coin_to == coin_id) have direction "incoming", or "symmetric"
    when the relationship type is symmetric.
    """
    rel_types: dict[int, RelationshipType] = {rt.id: rt for rt in RelationshipType.select(conn)}

    results: list[dict[str, object]] = []

    for rel in CoinRelationship.select(conn, coin_from=coin_id):
        rt = rel_types.get(rel.relationship_type)
        results.append(
            {
                "coin": rel.coin_to,
                "relationship": rt.name if rt else str(rel.relationship_type),
                "direction": "outgoing",
                "confidence": rel.confidence,
                "source": rel.source,
                "notes": rel.notes,
            }
        )

    for rel in CoinRelationship.select(conn, coin_to=coin_id):
        rt = rel_types.get(rel.relationship_type)
        direction = "symmetric" if (rt and rt.is_symmetric) else "incoming"
        results.append(
            {
                "coin": rel.coin_from,
                "relationship": rt.name if rt else str(rel.relationship_type),
                "direction": direction,
                "confidence": rel.confidence,
                "source": rel.source,
                "notes": rel.notes,
            }
        )

    return results
