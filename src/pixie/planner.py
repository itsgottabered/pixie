"""Replica placement proposal engine.

Given a set of volumes and a node pool, proposes new replica placements
using keep-if-suitable round-robin within statefulset groups.

Supports:
  - Explicit node list (--nodes)
  - Topology-based pool selection (--topology key=value)
  - Ring topology for specific index patterns
"""
from __future__ import annotations
import re
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from typing import Optional

from .models import Node, Volume


@dataclass
class Move:
    volume_id: str
    volume_name: str
    namespace: str
    size_gib: float
    from_node_id: str
    from_node_short: str
    to_node_id: str
    to_node_short: str
    move_num: int = 1   # 1 = first move, 2 = second move (for two-move volumes)


@dataclass
class Proposal:
    volumes: list[Volume]
    nodes: dict[str, Node]
    proposed: dict[str, list[str]]   # volume_id -> [node_id, ...]
    moves: list[Move]

    @property
    def first_moves(self) -> list[Move]:
        return [m for m in self.moves if m.move_num == 1]

    @property
    def second_moves(self) -> list[Move]:
        return [m for m in self.moves if m.move_num == 2]


def select_pool(
    nodes: dict[str, Node],
    node_ids: Optional[list[str]] = None,
    topology: Optional[dict[str, str]] = None,
) -> list[str]:
    """Return node IDs matching the requested pool.

    node_ids: explicit list of node IDs, short hostnames, or label prefixes
    topology: dict of topology key->value matched against
              topology.portworx.io/* labels (e.g. {"zone": "pod04"}).
              Falls back to hostname segment matching if no PX topology
              labels are available on any node.
    """
    if node_ids:
        pool = []
        id_set = set(nodes.keys())
        for n in node_ids:
            n = n.strip()
            if n in id_set:
                pool.append(n)
            else:
                # Match by short hostname, label, or hostname prefix
                for nid, node in nodes.items():
                    if (node.hostname.startswith(n)
                            or node.short.startswith(n)
                            or node.label == n):
                        pool.append(nid)
                        break
        return pool

    if topology:
        # Determine whether any nodes have PX topology labels
        has_px_topo = any(n.topology for n in nodes.values())

        pool = []
        for nid, node in nodes.items():
            if has_px_topo:
                # Match against topology.portworx.io/* labels
                # e.g. --topology zone=pod04  matches node.topology["zone"] == "pod04"
                if all(
                    node.topology.get(k, "").lower() == v.lower()
                    for k, v in topology.items()
                ):
                    pool.append(nid)
            else:
                # Fallback: match topology values against hostname segments
                # e.g. zone=pod04 matches compute14.pod04.prd.aussiebb.io
                hostname = node.hostname.lower()
                if all(
                    f".{v.lower()}." in f".{hostname}."
                    or hostname.startswith(f"{v.lower()}.")
                    for v in topology.values()
                ):
                    pool.append(nid)
        return pool

    # Default: all nodes
    return list(nodes.keys())


def propose(
    volumes: list[Volume],
    nodes: dict[str, Node],
    pool: list[str],
    ring: Optional[dict[int, list[str]]] = None,
    keep_if_suitable: bool = True,
) -> Proposal:
    """Generate a placement proposal.

    Args:
        volumes:           volumes to plan for
        nodes:             all cluster nodes (id -> Node)
        pool:              node IDs eligible for replica placement
        ring:              optional {index: [node_id, node_id]} override for specific indices
        keep_if_suitable:  if True, keep existing replicas on pool nodes when load allows
    """
    if not pool:
        raise ValueError("Node pool is empty — check --nodes or --topology arguments.")

    ha = 2  # always plan for HA=2

    # Group by statefulset prefix, sort numerically within each group
    groups: dict[str, list[Volume]] = defaultdict(list)
    ungrouped: list[Volume] = []
    for vol in volumes:
        if vol.index >= 0:
            groups[vol.group].append(vol)
        else:
            ungrouped.append(vol)

    pool_load = Counter({n: 0 for n in pool})
    all_proposed: dict[str, list[str]] = {}

    def _assign(vol: Volume) -> list[str]:
        vid     = vol.id
        current = set(vol.replica_node_ids)

        if ring and vol.index in ring:
            return ring[vol.index]

        if keep_if_suitable:
            in_pool  = current & set(pool)
            median   = sorted(pool_load.values())[len(pool) // 2]
            keepable = {n for n in in_pool if pool_load[n] <= median + 1}
            kept: set[str] = set()
            for n in sorted(keepable, key=lambda n: pool_load[n]):
                if len(kept) < ha:
                    kept.add(n)
            assigned = set(kept)
        else:
            assigned = set()

        while len(assigned) < min(ha, len(pool)):
            best = min(
                (pool_load[n], n) for n in pool if n not in assigned
            )[1]
            assigned.add(best)

        for n in assigned:
            pool_load[n] += 1

        return sorted(assigned)

    # Process groups
    for gname in sorted(groups.keys()):
        gvols = sorted(groups[gname], key=lambda v: v.index)
        for vol in gvols:
            all_proposed[vol.id] = _assign(vol)

    # Process ungrouped
    for vol in sorted(ungrouped, key=lambda v: v.display_name):
        all_proposed[vol.id] = _assign(vol)

    # Generate moves
    moves: list[Move] = []
    for vol in volumes:
        current  = set(vol.replica_node_ids)
        proposed = set(all_proposed.get(vol.id, vol.replica_node_ids))
        to_remove = sorted(current - proposed)
        to_add    = sorted(proposed - current)

        for i, (rem, add) in enumerate(zip(to_remove, to_add), start=1):
            moves.append(Move(
                volume_id=vol.id,
                volume_name=vol.display_name,
                namespace=vol.namespace,
                size_gib=vol.size_gib,
                from_node_id=rem,
                from_node_short=nodes[rem].short if rem in nodes else rem,
                to_node_id=add,
                to_node_short=nodes[add].short if add in nodes else add,
                move_num=i,
            ))

    return Proposal(
        volumes=volumes,
        nodes=nodes,
        proposed=all_proposed,
        moves=moves,
    )


def proposal_to_plan_rows(proposal: Proposal, nodes: dict[str, Node]) -> list[dict]:
    """Convert a Proposal into flat rows suitable for write_plan_csv."""
    rows = []
    # Group moves by volume
    vol_moves: dict[str, list[Move]] = defaultdict(list)
    for m in proposal.moves:
        vol_moves[m.volume_id].append(m)

    proposed_map = proposal.proposed

    for vol in sorted(proposal.volumes, key=lambda v: (v.namespace, v.display_name)):
        vmoves = vol_moves.get(vol.id, [])
        n_moves = len(vmoves)

        cur_ids    = vol.replica_node_ids
        cur_shorts = [nodes[n].short if n in nodes else n for n in cur_ids]
        prop_ids   = proposed_map.get(vol.id, cur_ids)
        prop_shorts = [nodes[n].short if n in nodes else n for n in prop_ids]

        m1 = vmoves[0] if len(vmoves) > 0 else None
        m2 = vmoves[1] if len(vmoves) > 1 else None

        rows.append({
            "namespace":        vol.namespace,
            "pvc":              vol.display_name,
            "volume_id":        vol.id,
            "size_gib":         f"{vol.size_gib:.2f}",
            "ha":               vol.ha,
            "current_nodes":    "|".join(cur_shorts),
            "current_node_ids": "|".join(cur_ids),
            "proposed_nodes":   "|".join(prop_shorts),
            "proposed_node_ids":"|".join(prop_ids),
            "moves":            n_moves,
            "remove_node":      m1.from_node_short if m1 else "",
            "remove_node_id":   m1.from_node_id    if m1 else "",
            "add_node":         m1.to_node_short    if m1 else "",
            "add_node_id":      m1.to_node_id       if m1 else "",
            "remove_node_2":    m2.from_node_short  if m2 else "",
            "remove_node_id_2": m2.from_node_id     if m2 else "",
            "add_node_2":       m2.to_node_short    if m2 else "",
            "add_node_id_2":    m2.to_node_id       if m2 else "",
        })
    return rows
