"""Volume and node data models, parsed from pxctl JSON output."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Node:
    id: str
    ip: str
    hostname: str          # e.g. compute14.pod04.prd.aussiebb.io
    used_gib: float
    cap_gib: float
    status: str
    topology: dict[str, str] = field(default_factory=dict)
    # e.g. {"zone": "pod04", "region": "core", "datacenter": "IA"}

    @property
    def short(self) -> str:
        """compute14.pod04.prd.aussiebb.io -> compute14.pod04"""
        parts = self.hostname.split(".")
        return ".".join(parts[:2]) if len(parts) >= 2 else self.hostname

    @property
    def label(self) -> str:
        """Shortest useful label: compute14"""
        return self.hostname.split(".")[0]

    @property
    def free_gib(self) -> float:
        return self.cap_gib - self.used_gib

    @property
    def free_pct(self) -> float:
        return self.free_gib / self.cap_gib * 100 if self.cap_gib else 0.0


@dataclass
class ReplicaSet:
    nodes: list[str]   # node IDs
    pool_uuids: list[str] = field(default_factory=list)


@dataclass
class Volume:
    id: str
    name: str
    pvc: str
    namespace: str
    size_gib: float
    ha: int
    state: str           # "attached" | "detached"
    attached_node_id: Optional[str]
    attached_node_ip: Optional[str]
    replica_node_ids: list[str]   # node IDs from replica sets
    repl_status: str
    labels: dict[str, str]
    parent: str = ""     # non-empty = snapshot/clone

    @property
    def display_name(self) -> str:
        return self.pvc or self.name

    @property
    def group(self) -> str:
        """Strip trailing -N to get statefulset group name."""
        return re.sub(r"-\d+$", "", self.display_name)

    @property
    def index(self) -> int:
        m = re.search(r"-(\d+)$", self.display_name)
        return int(m.group(1)) if m else -1

    @property
    def is_attached(self) -> bool:
        return self.state.lower() == "attached"

    @property
    def is_detached(self) -> bool:
        return self.state.lower() == "detached"


def parse_nodes(raw_nodes: list[dict]) -> dict[str, Node]:
    """Parse cluster node list into id->Node map."""
    nodes = {}
    for n in raw_nodes:
        node = Node(
            id=n.get("id", ""),
            ip=n.get("ip", ""),
            hostname=n.get("hostname", ""),
            used_gib=n.get("used_gib", 0.0),
            cap_gib=n.get("cap_gib", 0.0),
            status=n.get("status", ""),
            topology=n.get("topology", {}),
        )
        if node.id:
            nodes[node.id] = node
    return nodes


def _int(v) -> int:
    """Coerce string/int/None to int safely."""
    try:
        return int(v) if v is not None else 0
    except (ValueError, TypeError):
        return 0


def parse_volume(raw: dict, nodes_by_ip: dict[str, Node]) -> Optional[Volume]:
    """Parse a pxctl volume inspect JSON object into a Volume.

    Matches actual pxctl 3.x --json output:
      raw["locator"]["name"]                  — volume/PVC name
      raw["locator"]["volume_labels"]         — labels (namespace, pvc, etc.)
      raw["spec"]["size"]                     — string bytes
      raw["spec"]["ha_level"]                 — string int
      raw["state"]                            — "attached" | "detached"
      raw["attached_on"]                      — IP address of attached node
      raw["replica_sets"][0]["nodes"]         — list of node UUIDs
      raw["runtime_state"][0]["runtime_state"]["RuntimeState"] — "clean"|"resync"
      raw["usage"]                            — string bytes used
      raw["source"]["parent"]                 — non-empty = snapshot/clone
    """
    if not raw:
        return None

    vol_id   = str(raw.get("id", "") or "")
    locator  = raw.get("locator") or {}
    vol_name = str(locator.get("name", "") or "")

    # ── Labels ────────────────────────────────────────────────────────────────
    # Authoritative source is locator.volume_labels
    raw_labels = locator.get("volume_labels") or {}
    if not raw_labels:
        # Fallback for older versions
        raw_labels = (raw.get("spec") or {}).get("volume_labels") or raw.get("labels") or {}
    labels: dict[str, str] = (
        {str(k): str(v) for k, v in raw_labels.items()}
        if isinstance(raw_labels, dict) else {}
    )
    namespace = labels.get("namespace", "")
    pvc       = labels.get("pvc", "")

    # ── Size ──────────────────────────────────────────────────────────────────
    spec       = raw.get("spec") or {}
    size_bytes = _int(spec.get("size", 0))
    size_gib   = size_bytes / (1024 ** 3) if size_bytes else 0.0

    # ── HA level ──────────────────────────────────────────────────────────────
    ha = _int(spec.get("ha_level", 0))

    # ── State / attachment ────────────────────────────────────────────────────
    state_str        = str(raw.get("state", "") or "")
    attached_node_id = None
    attached_node_ip = None

    # attached_on is an IP address in v3
    attached_on_ip = str(raw.get("attached_on", "") or "").strip()
    if attached_on_ip:
        attached_node_ip = attached_on_ip
        if attached_on_ip in nodes_by_ip:
            attached_node_id = nodes_by_ip[attached_on_ip].id

    # ── Replica sets ──────────────────────────────────────────────────────────
    replica_node_ids: list[str] = []
    for rs in raw.get("replica_sets") or []:
        for node_id in rs.get("nodes") or []:
            if node_id and node_id not in replica_node_ids:
                replica_node_ids.append(str(node_id))

    # ── Replication status ────────────────────────────────────────────────────
    # runtime_state is a list; RuntimeState: "clean", "resync", etc.
    repl_status = ""
    runtime_list = raw.get("runtime_state") or []
    if isinstance(runtime_list, list) and runtime_list:
        rs_inner = (runtime_list[0] or {}).get("runtime_state") or {}
        repl_status = str(rs_inner.get("RuntimeState", "") or "")
    elif isinstance(runtime_list, dict):
        # Older versions returned a dict
        repl_status = str(runtime_list.get("RuntimeState", "") or "")

    # ── Parent (snapshot/clone) ───────────────────────────────────────────────
    parent = str((raw.get("source") or {}).get("parent", "") or "")

    return Volume(
        id=vol_id,
        name=vol_name,
        pvc=pvc,
        namespace=namespace,
        size_gib=size_gib,
        ha=ha,
        state=state_str,
        attached_node_id=attached_node_id or None,
        attached_node_ip=attached_node_ip or None,
        replica_node_ids=replica_node_ids,
        repl_status=repl_status,
        labels=labels,
        parent=parent,
    )


def volumes_by_namespace(volumes: list[Volume]) -> dict[str, list[Volume]]:
    result: dict[str, list[Volume]] = {}
    for v in volumes:
        ns = v.namespace or "(none)"
        result.setdefault(ns, []).append(v)
    return result
