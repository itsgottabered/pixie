"""pixie - Portworx replica placement analyser and rebalancer."""
from __future__ import annotations
import os
import sys
import click
from . import analyse, propose, rebalance, status
from . import pxctl as pxctl_module


@click.group()
@click.version_option("0.1.0", prog_name="pixie")
@click.option("--px-namespace", envvar="PX_NAMESPACE", default="portworx",
              show_default=True, help="Kubernetes namespace where PX pods run.")
@click.option("--px-label",     envvar="PX_LABEL",     default="name=portworx",
              show_default=True, help="Label selector for the PX pod.")
@click.pass_context
def main(ctx, px_namespace, px_label):
    """pixie — Portworx replica placement tool.

    \b
    Workflows:
      pixie status                        # cluster health overview
      pixie analyse -a                    # whole-cluster placement report
      pixie analyse -n mimir              # single namespace -> stdout (CSV)
      pixie propose -n mimir --topology zone=pod04 -o plan.csv
      pixie propose -n mimir --nodes compute13,compute14,...
      pixie rebalance --plan plan.csv --dry-run
      pixie rebalance --plan plan.csv

    \b
    Environment variables:
      PX_NAMESPACE   Kubernetes namespace for PX pods  [default: portworx]
      PX_LABEL       Pod label selector                [default: name=portworx]
    """
    # Push namespace/label into the pxctl module so all subcommands pick them up
    pxctl_module.PX_NAMESPACE = px_namespace
    pxctl_module.PX_LABEL     = px_label


main.add_command(status.cmd,    name="status")
main.add_command(analyse.cmd,   name="analyse")
main.add_command(propose.cmd,   name="propose")
main.add_command(rebalance.cmd, name="rebalance")
