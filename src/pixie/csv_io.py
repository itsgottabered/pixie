"""CSV output for pixie analyse and pixie propose."""
from __future__ import annotations
import csv
import io
import sys
from typing import TextIO

from .models import Node, Volume


# ── Analyse report ────────────────────────────────────────────────────────────

ANALYSE_FIELDS = [
    "namespace",
    "pvc",
    "volume_id",
    "size_gib",
    "ha",
    "state",
    "repl_status",
    "replica_nodes",        # pipe-separated short hostnames
    "replica_node_ids",     # pipe-separated UUIDs
    "replica_topology",     # pipe-separated zone/region per replica node
    "attached_node",
    "attached_node_id",
    "group",
    "index",
]


def write_analyse_csv(
    volumes: list[Volume],
    nodes_by_id: dict[str, Node],
    out: TextIO = sys.stdout,
) -> None:
    writer = csv.DictWriter(out, fieldnames=ANALYSE_FIELDS, lineterminator="\n")
    writer.writeheader()

    for vol in sorted(volumes, key=lambda v: (v.namespace, v.display_name)):
        replica_nodes = [
            nodes_by_id[nid].short if nid in nodes_by_id else nid
            for nid in vol.replica_node_ids
        ]
        replica_topo = [
            ",".join(f"{k}={v}" for k, v in sorted(nodes_by_id[nid].topology.items()))
            if nid in nodes_by_id else ""
            for nid in vol.replica_node_ids
        ]
        att_node = (
            nodes_by_id[vol.attached_node_id].short
            if vol.attached_node_id and vol.attached_node_id in nodes_by_id
            else (vol.attached_node_ip or "")
        )
        writer.writerow({
            "namespace":        vol.namespace,
            "pvc":              vol.display_name,
            "volume_id":        vol.id,
            "size_gib":         f"{vol.size_gib:.2f}",
            "ha":               vol.ha,
            "state":            vol.state,
            "repl_status":      vol.repl_status,
            "replica_nodes":    "|".join(replica_nodes),
            "replica_node_ids": "|".join(vol.replica_node_ids),
            "replica_topology": "|".join(replica_topo),
            "attached_node":    att_node,
            "attached_node_id": vol.attached_node_id or "",
            "group":            vol.group,
            "index":            vol.index,
        })


# ── Proposal / plan CSV ───────────────────────────────────────────────────────

PLAN_FIELDS = [
    "namespace",
    "pvc",
    "volume_id",
    "size_gib",
    "ha",
    # Current state
    "current_nodes",
    "current_node_ids",
    # Proposed state
    "proposed_nodes",
    "proposed_node_ids",
    # Moves needed
    "moves",                # number of moves
    "remove_node",          # node to drop replica from (step 1)
    "remove_node_id",
    "add_node",             # node to add replica on (step 2)
    "add_node_id",
    # For two-move volumes, second move
    "remove_node_2",
    "remove_node_id_2",
    "add_node_2",
    "add_node_id_2",
]


def write_plan_csv(
    plan_rows: list[dict],
    out: TextIO = sys.stdout,
) -> None:
    """Write a rebalance plan to CSV.

    Each row in plan_rows should have keys matching PLAN_FIELDS.
    """
    writer = csv.DictWriter(out, fieldnames=PLAN_FIELDS, lineterminator="\n",
                            extrasaction="ignore")
    writer.writeheader()
    for row in plan_rows:
        writer.writerow(row)


def read_plan_csv(path: str) -> list[dict]:
    """Read a rebalance plan CSV back into a list of dicts."""
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(dict(row))
    return rows
