"""pixie status — show cluster health and replica load per node."""
from __future__ import annotations
import sys

import click

from .pxctl import Pxctl, PxError
from .models import parse_nodes, parse_volume, Node, Volume


@click.command("status")
@click.option("-v", "--verbose", is_flag=True)
def cmd(verbose):
    """Show cluster node health and replica load summary.

    \b
    Example:
      pixie status
    """
    px = Pxctl(verbose=verbose)

    try:
        click.echo("Fetching cluster status...", err=True)
        raw_nodes = px.node_list()
        nodes = parse_nodes(raw_nodes)

        if not nodes:
            click.echo("No storage nodes found.", err=True)
            sys.exit(1)

        click.echo("Fetching volume list...", err=True)
        raw_vols = px.fetch_volumes({"volumes_only": True})
        volumes: list[Volume] = []
        nodes_by_ip = {n.ip: n for n in nodes.values()}
        for rv in raw_vols:
            v = parse_volume(rv, nodes_by_ip)
            if v and v.replica_node_ids:
                volumes.append(v)

        # Count replicas per node
        from collections import Counter
        replica_count: Counter = Counter()
        for vol in volumes:
            for nid in vol.replica_node_ids:
                replica_count[nid] += 1

        attached_count: Counter = Counter()
        for vol in volumes:
            if vol.attached_node_id:
                attached_count[vol.attached_node_id] += 1

        # Print table
        click.echo()
        header = f"{'Node':<40} {'Status':<10} {'Used GiB':>9} {'Cap GiB':>8} {'Free%':>6}  {'Replicas':>9}  {'Attached':>9}  Topology"
        click.echo(header)
        click.echo("─" * len(header))

        for nid, node in sorted(nodes.items(), key=lambda x: x[1].hostname):
            free_pct = node.free_pct
            reps     = replica_count.get(nid, 0)
            att      = attached_count.get(nid, 0)
            status   = node.status
            topo_str = "  ".join(f"{k}={v}" for k, v in sorted(node.topology.items())) if node.topology else ""

            warn = ""
            if free_pct < 15:
                warn = " ◀ LOW SPACE"
            elif reps == 0:
                warn = " ◀ no replicas"

            click.echo(
                f"{node.hostname:<40} {status:<10} {node.used_gib:>9.0f} "
                f"{node.cap_gib:>8.0f} {free_pct:>5.1f}%  {reps:>9}  {att:>9}  {topo_str}{warn}"
            )

        click.echo()
        click.echo(f"Totals: {len(nodes)} nodes, {len(volumes)} replicated volumes, "
                   f"{sum(replica_count.values())} replica slots")

    except PxError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
