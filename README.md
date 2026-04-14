# pixie

Portworx replica placement analyser and rebalancer.

## Installation

```bash
pip install -e .
```

Requires: Python ≥ 3.10, `kubectl` on PATH, an active kube context pointing at
a cluster with Portworx running in the `portworx` namespace (pod label `name=portworx`).

---

## Commands

### pixie status

Show cluster node health, capacity, and replica load at a glance.

```bash
pixie status
```

Displays a table of all storage nodes with used/capacity, free %, replica count,
attached volume count, and topology labels (`zone`, `region`, `datacenter`).

---

### pixie analyse

Fetch current volume replica placement and write a CSV report.

```bash
# Whole cluster
pixie analyse -a -o placement.csv

# One or more namespaces
pixie analyse -n mimir -n loki -o observability.csv

# Volumes with a replica on a specific node
pixie analyse --node compute14 -o compute14.csv

# Volumes in a specific pool
pixie analyse --pool-uid <uuid> -o pool.csv

# Filter by volume name or volume group
pixie analyse --name my-volume
pixie analyse --group my-group

# Additional label filter (combined with -n)
pixie analyse -n mimir --label repl=3 -o mimir-ha3.csv

# Snapshots only
pixie analyse -a --snapshots -o snapshots.csv

# Non-snapshot volumes only (default behaviour, explicit)
pixie analyse -a --volumes-only

# Include snapshots alongside regular volumes
pixie analyse -a --include-snapshots -o everything.csv

# Snapshots of a specific parent volume
pixie analyse --parent <volume-id> -o children.csv

# To stdout (pipe into other tools)
pixie analyse -n mimir | column -t -s,
```

**CSV columns:** `namespace, pvc, volume_id, size_gib, ha, state, repl_status,
replica_nodes, replica_node_ids, replica_topology, attached_node, attached_node_id,
group, index`

`replica_topology` contains pipe-separated topology strings per replica node,
e.g. `datacenter=IA,region=core,zone=pod04|datacenter=IA,region=core,zone=pod04`.

---

### pixie propose

Generate a placement proposal and write it as a plan CSV.

```bash
# Spread mimir across pod04 nodes using PX topology labels
pixie propose -n mimir --topology zone=pod04 -o mimir_plan.csv

# Multiple topology constraints (ANDed)
pixie propose -n mimir --topology zone=pod04 --topology datacenter=IA

# Explicit node list (label, short hostname, or node UUID)
pixie propose -n kea --nodes bootstrap01,bootstrap02,bootstrap03,bootstrap04,bootstrap05

# Ring topology for statefulsets
pixie propose -n infra-bootstrap \
  --ring 0=bootstrap01,bootstrap02 \
  --ring 1=bootstrap02,bootstrap03 \
  --ring 2=bootstrap03,bootstrap04 \
  --ring 3=bootstrap04,bootstrap05 \
  --ring 4=bootstrap05,bootstrap01 \
  -o ring_plan.csv

# Strict spread — ignore current placement entirely
pixie propose -n gitea --no-keep -o gitea_plan.csv

# Multiple namespaces in one plan
pixie propose -n kea -n monitoring --no-keep -o spread_plan.csv
```

**Plan CSV columns:** `namespace, pvc, volume_id, size_gib, ha,
current_nodes, current_node_ids, proposed_nodes, proposed_node_ids, moves,
remove_node, remove_node_id, add_node, add_node_id,
remove_node_2, remove_node_id_2, add_node_2, add_node_id_2`

Rows with `moves=0` are already correctly placed and will be skipped by `rebalance`.
The plan CSV is human-readable — open it in Excel to review before executing.

---

### pixie rebalance

Execute a plan CSV produced by `propose`.

```bash
# Dry run first — prints executable pxctl commands to stdout
pixie rebalance --plan mimir_plan.csv --dry-run
pixie rebalance --plan mimir_plan.csv --dry-run > script.sh

# Execute (both stages, with sync wait between them)
pixie rebalance --plan mimir_plan.csv

# Tune timing
pixie rebalance --plan mimir_plan.csv --sleep 10 --poll-interval 60 --sync-timeout 900

# Run only stage 1 now, stage 2 later
pixie rebalance --plan mimir_plan.csv --stage 1
# ... check health manually ...
pixie rebalance --plan mimir_plan.csv --stage 2

# Limit to a specific namespace within a combined plan
pixie rebalance --plan combined_plan.csv -n mimir
```

**Execution model (HA-aware):**

pixie handles any HA level correctly — it never drops a volume below `ha - 1` replicas:

| HA level | Drop step | Raise step |
|---|---|---|
| HA=2 | `--repl 1` (remove old node) | `--repl 2` (add new node) |
| HA=3 | `--repl 2` (remove old node) | `--repl 3` (add new node) |

Stage 1 runs one move per volume. pixie then polls `RuntimeState` on every moved
volume until all report `clean`. Stage 2 runs the second move for volumes that
needed two replica replacements — for HA=3 this is a second `3→2→3` cycle on the
same volume, which is safe because stage 1 has fully synced first.

> **Note:** If a volume needs more than two replicas moved (e.g. all three replicas
> of an HA=3 volume are on wrong nodes), run `propose` and `rebalance` a second time
> after the first plan completes — Portworx only allows one replica transition in
> flight per volume at a time.

---

## Environment / configuration

| Variable | Default | Purpose |
|---|---|---|
| `PX_NAMESPACE` | `portworx` | Kubernetes namespace where PX pods run |
| `PX_LABEL` | `name=portworx` | Pod label selector for the PX pod |

These can also be passed as global flags:

```bash
pixie --px-namespace kube-system --px-label app=portworx status
```

---

## Topology matching

`--topology key=value` matches against the `topology.portworx.io/*` labels that
Portworx reads from pool labels in `pxctl cluster list`. These are the same labels
Portworx itself uses for placement decisions.

```bash
--topology zone=pod04        # topology.portworx.io/zone=pod04
--topology region=core       # topology.portworx.io/region=core
--topology datacenter=IA     # topology.portworx.io/datacenter=IA
```

Multiple `--topology` flags are ANDed. If no `topology.portworx.io/*` labels exist
on any node (e.g. a cluster without topology configured), pixie falls back to
matching the value against hostname segments.

---

## How the planner works

1. Volumes are grouped by statefulset prefix (trailing `-N` stripped).
2. Within each group, volumes are sorted by numeric index.
3. For each volume, existing replicas already on eligible pool nodes that aren't
   overloaded are kept in place (keep-if-suitable). Remaining slots are filled
   from the coldest pool node by replica count.
4. `--no-keep` disables step 3, forcing strict round-robin from scratch.
5. `--ring` pins specific indices to exact node pairs, bypassing the planner entirely.

The planner tracks a running load counter per node across all statefulset groups
in the same run, so groups don't all cluster onto the same starting nodes.
