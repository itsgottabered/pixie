"""pixie propose — generate a replica placement proposal CSV."""
from __future__ import annotations
import re
import sys

import click

from .pxctl import Pxctl, PxError
from .models import parse_nodes, parse_volume, Node, Volume
from .planner import propose, select_pool, proposal_to_plan_rows
from .csv_io import write_plan_csv


def _parse_topology(topology_args: tuple[str, ...]) -> dict[str, str]:
    """Parse 'zone=pod04' style args into a dict."""
    result = {}
    for t in topology_args:
        if "=" not in t:
            raise click.BadParameter(f"Topology must be key=value, got: {t!r}")
        k, _, v = t.partition("=")
        result[k.strip()] = v.strip()
    return result


def _parse_ring(ring_args: tuple[str, ...], nodes: dict[str, Node]) -> dict[int, list[str]]:
    """Parse '0=node1,node2' style ring overrides.

    Example: --ring 0=bootstrap01,bootstrap02 --ring 4=bootstrap05,bootstrap01
    """
    ring: dict[int, list[str]] = {}
    id_by_label: dict[str, str] = {}
    for nid, node in nodes.items():
        id_by_label[node.label]    = nid
        id_by_label[node.short]    = nid
        id_by_label[node.hostname] = nid
        id_by_label[nid]           = nid

    for r in ring_args:
        if "=" not in r:
            raise click.BadParameter(f"Ring must be index=node1,node2, got: {r!r}")
        idx_str, _, node_str = r.partition("=")
        try:
            idx = int(idx_str.strip())
        except ValueError:
            raise click.BadParameter(f"Ring index must be an integer, got: {idx_str!r}")
        node_labels = [n.strip() for n in node_str.split(",")]
        node_ids = []
        for label in node_labels:
            if label in id_by_label:
                node_ids.append(id_by_label[label])
            else:
                raise click.BadParameter(f"Unknown node: {label!r}")
        ring[idx] = node_ids
    return ring


@click.command("propose")
@click.option("-n", "--namespace",   multiple=True,
              help="Namespace(s) to plan. May be repeated.")
@click.option("-a", "--all",         "all_cluster", is_flag=True,
              help="Plan for all volumes in the cluster.")
@click.option("--nodes",             default="",
              help="Comma-separated node labels/IDs to use as replica pool.")
@click.option("--topology",          multiple=True, metavar="KEY=VALUE",
              help="Filter pool by topology label. E.g. --topology zone=pod04")
@click.option("--ring",              multiple=True, metavar="INDEX=NODE1,NODE2",
              help="Pin specific volume indices to node pairs. "
                   "E.g. --ring 0=bs01,bs02 --ring 1=bs02,bs03")
@click.option("--no-keep",           is_flag=True,
              help="Strict round-robin — ignore existing placement entirely.")
@click.option("-o", "--output",      default="-", show_default=True,
              help="Output plan CSV file (default: stdout).")
@click.option("-v", "--verbose",     is_flag=True)
def cmd(namespace, all_cluster, nodes, topology, ring, no_keep, output, verbose):
    """Generate a replica placement proposal and write it as a CSV plan.

    \b
    Examples:
      # Spread mimir across pod04 nodes
      pixie propose -n mimir --topology zone=pod04 -o mimir_plan.csv

      # Explicit node list
      pixie propose -n kea --nodes bootstrap01,bootstrap02,bootstrap03

      # Ring topology for statefulset
      pixie propose -n infra-bootstrap \\
        --ring 0=bootstrap01,bootstrap02 \\
        --ring 1=bootstrap02,bootstrap03 \\
        --ring 2=bootstrap03,bootstrap04 \\
        --ring 3=bootstrap04,bootstrap05 \\
        --ring 4=bootstrap05,bootstrap01

      # Strict spread ignoring current placement
      pixie propose -n gitea --no-keep
    """
    if not all_cluster and not namespace:
        raise click.UsageError("Specify -a (all) or -n <namespace>.")

    px = Pxctl(verbose=verbose)

    try:
        click.echo("Fetching node list...", err=True)
        raw_nodes   = px.node_list()
        nodes_by_id = parse_nodes(raw_nodes)
        nodes_by_ip = {n.ip: n for n in nodes_by_id.values()}

        click.echo("Fetching volumes...", err=True)
        raw_vols = []
        if all_cluster:
            raw_vols = px.fetch_volumes({})
        else:
            for ns in namespace:
                click.echo(f"  namespace: {ns}", err=True)
                raw_vols.extend(px.fetch_volumes({"namespace": ns}))

        volumes: list[Volume] = []
        for rv in raw_vols:
            v = parse_volume(rv, nodes_by_ip)
            if v and v.replica_node_ids:
                volumes.append(v)

        click.echo(f"Planning for {len(volumes)} volumes...", err=True)

        # Build pool
        node_ids_list = [n.strip() for n in nodes.split(",") if n.strip()] if nodes else None
        topo_dict     = _parse_topology(topology) if topology else None
        pool = select_pool(nodes_by_id, node_ids=node_ids_list, topology=topo_dict)
        if not pool:
            raise click.ClickException("Node pool is empty — check --nodes or --topology.")

        pool_labels = [nodes_by_id[n].short for n in pool if n in nodes_by_id]
        click.echo(f"Pool ({len(pool)} nodes): {', '.join(pool_labels)}", err=True)

        # Ring overrides
        ring_map = _parse_ring(ring, nodes_by_id) if ring else None

        proposal = propose(
            volumes=volumes,
            nodes=nodes_by_id,
            pool=pool,
            ring=ring_map,
            keep_if_suitable=not no_keep,
        )

        n_moves = len(proposal.moves)
        n_first  = len(proposal.first_moves)
        n_second = len(proposal.second_moves)
        click.echo(
            f"Proposal: {n_moves} moves "
            f"({n_first} first-pass, {n_second} second-pass).",
            err=True,
        )

        rows = proposal_to_plan_rows(proposal, nodes_by_id)

        if output == "-":
            write_plan_csv(rows, sys.stdout)
        else:
            with open(output, "w", newline="") as f:
                write_plan_csv(rows, f)
            click.echo(f"Plan written to {output}", err=True)

    except PxError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
