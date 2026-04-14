"""pixie analyse — fetch volume placement and write a CSV report."""
from __future__ import annotations
import sys

import click

from .pxctl import Pxctl, PxError
from .models import parse_nodes, parse_volume, Volume, Node
from .csv_io import write_analyse_csv


@click.command("analyse")
@click.option("-a", "--all",          "all_cluster",    is_flag=True,
              help="All volumes in the cluster (no other filters).")
@click.option("-n", "--namespace",    multiple=True,
              help="Namespace(s) to analyse. May be repeated.")
@click.option("-l", "--label",        default="",
              help="Label filter, e.g. 'app=mimir,env=prod'.")
@click.option("--name",               default="",
              help="Filter by volume name.")
@click.option("--node",               default="",
              help="Show volumes with a replica on this node (ID or hostname).")
@click.option("--pool-uid",           default="",
              help="Show volumes with a replica in this pool UUID.")
@click.option("--group",              default="",
              help="Show volumes in this volume group.")
@click.option("--parent",             default="",
              help="Show snapshots of this parent volume ID.")
@click.option("--snapshots",          "snapshots_only", is_flag=True,
              help="Show only snapshots.")
@click.option("--volumes-only",       "volumes_only",   is_flag=True,
              help="Show only non-snapshot volumes.")
@click.option("--include-snapshots",  "include_snapshots", is_flag=True,
              help="Include snapshots in results (sets pxctl --all).")
@click.option("-o", "--output",  default="-",  show_default=True,
              help="Output CSV file (default: stdout).")
@click.option("-v", "--verbose", is_flag=True,
              help="Show pxctl commands as they run.")
def cmd(all_cluster, namespace, label, name, node, pool_uid, group, parent,
        snapshots_only, volumes_only, include_snapshots, output, verbose):
    """Fetch volume placement information and write a CSV report.

    \b
    Examples:
      pixie analyse -a                              # whole cluster
      pixie analyse -n mimir -o mimir.csv
      pixie analyse -n mimir -n loki -o obs.csv
      pixie analyse --node compute14               # volumes on a specific node
      pixie analyse --pool-uid <uuid>              # volumes in a specific pool
      pixie analyse -n mimir --label repl=3        # extra label filter
      pixie analyse -a --snapshots                 # only snapshots
      pixie analyse -a --include-snapshots         # everything including snapshots
      pixie analyse --name my-vol                  # specific volume by name
      pixie analyse --group my-group               # volume group
      pixie analyse --parent <vol-id>              # snapshots of a volume
    """
    has_filter = any([all_cluster, namespace, label, name, node,
                      pool_uid, group, parent, snapshots_only])
    if not has_filter:
        raise click.UsageError(
            "Specify at least one filter: -a, -n, --node, --label, --name, "
            "--pool-uid, --group, --parent, or --snapshots."
        )

    px = Pxctl(verbose=verbose)

    try:
        click.echo("Fetching node list...", err=True)
        raw_nodes   = px.node_list()
        nodes_by_id = parse_nodes(raw_nodes)
        nodes_by_ip = {n.ip: n for n in nodes_by_id.values()}

        click.echo("Fetching volumes...", err=True)
        raw_vols: list[dict] = []

        if namespace:
            # One fetch per namespace so progress is visible
            for ns in namespace:
                click.echo(f"  namespace: {ns}", err=True)
                raw_vols.extend(px.fetch_volumes({
                    "namespace":      ns,
                    "label":          label or None,
                    "all_vols":       include_snapshots,
                    "snapshots_only": snapshots_only,
                    "volumes_only":   volumes_only,
                }))
        else:
            # Single fetch with all other filters applied
            raw_vols = px.fetch_volumes({
                "label":          label or None,
                "name":           name   or None,
                "node":           node   or None,
                "pool_uid":       pool_uid or None,
                "group":          group  or None,
                "parent":         parent or None,
                "all_vols":       include_snapshots,
                "snapshots_only": snapshots_only,
                "volumes_only":   volumes_only,
            })

        volumes: list[Volume] = []
        for rv in raw_vols:
            v = parse_volume(rv, nodes_by_ip)
            if v:
                volumes.append(v)

        ns_set = set(v.namespace for v in volumes)
        click.echo(
            f"Parsed {len(volumes)} volumes across {len(ns_set)} namespace(s).",
            err=True,
        )

        if output == "-":
            write_analyse_csv(volumes, nodes_by_id, sys.stdout)
        else:
            with open(output, "w", newline="") as f:
                write_analyse_csv(volumes, nodes_by_id, f)
            click.echo(f"Written to {output}", err=True)

    except PxError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
