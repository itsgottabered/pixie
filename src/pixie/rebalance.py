"""pixie rebalance — execute a placement plan CSV.

Stage 1: all first-pass moves (drop HA to 1, then raise to 2 on new node).
         Waits for replication sync before proceeding.
Stage 2: all second-pass moves (volumes that needed two moves).
"""
from __future__ import annotations
import sys
import time

import click

from .pxctl import Pxctl, PxError
from .csv_io import read_plan_csv


# RuntimeState values from pxctl: "clean" = healthy, anything else = in progress
SYNC_OK = {"clean", "up", "ok", ""}


def _repl_ok(status: str) -> bool:
    return status.strip().lower() in SYNC_OK


def _wait_for_sync(
    px: Pxctl,
    volume_ids: list[str],
    poll_interval: int,
    timeout: int,
) -> bool:
    """Poll until all volumes report replication Up (or timeout)."""
    deadline = time.time() + timeout
    pending  = set(volume_ids)
    click.echo(
        f"\nWaiting for {len(pending)} volume(s) to sync "
        f"(timeout {timeout}s, polling every {poll_interval}s)...",
        err=True,
    )

    while pending and time.time() < deadline:
        still_pending = set()
        for vid in sorted(pending):
            try:
                detail = px.volume_status(vid)
                status = detail.get("replication_status", "").strip().lower()
                if _repl_ok(status):
                    click.echo(f"  ✓ {vid[:16]}... synced", err=True)
                else:
                    still_pending.add(vid)
                    if status:
                        click.echo(f"  … {vid[:16]}... {status}", err=True)
            except PxError as e:
                click.echo(f"  ? {vid[:16]}... status error: {e}", err=True)
                still_pending.add(vid)
        pending = still_pending
        if pending:
            time.sleep(poll_interval)

    if pending:
        click.echo(
            f"\nTimeout! {len(pending)} volume(s) still not synced:",
            err=True,
        )
        for vid in sorted(pending):
            click.echo(f"  {vid}", err=True)
        return False

    click.echo("All volumes synced ✓", err=True)
    return True


def _execute_move(
    px: Pxctl,
    volume_id: str,
    volume_name: str,
    ha: int,
    remove_node_id: str,
    remove_node: str,
    add_node_id: str,
    add_node: str,
    dry_run: bool,
    sleep_s: int,
) -> bool:
    """Execute a single replica move for a volume of any HA level.

    For HA=2: --repl 1 (remove) then --repl 2 (add)
    For HA=3: --repl 2 (remove) then --repl 3 (add)

    You can only move one replica at a time regardless of HA level.
    Each move within a multi-move volume must fully sync before the next.
    """
    repl_low  = max(ha - 1, 1)
    repl_high = ha

    click.echo(
        f"  {volume_name}  {remove_node} → {add_node}  [HA={ha}]",
        err=True,
    )

    if dry_run:
        click.echo(f"# {volume_name}  ({remove_node} → {add_node})  HA={ha}")
        click.echo(f"pxctl volume ha-update --repl {repl_low} {volume_id} --node {remove_node_id}")
        click.echo(f"sleep {sleep_s}")
        click.echo(f"pxctl volume ha-update --repl {repl_high} {volume_id} --node {add_node_id}")
        click.echo()
        return True

    try:
        click.echo(f"    ↓ setting repl={repl_low} (removing {remove_node})...", err=True)
        px.ha_update(volume_id, repl_low, remove_node_id)
        time.sleep(sleep_s)
        click.echo(f"    ↑ setting repl={repl_high} (adding {add_node})...", err=True)
        px.ha_update(volume_id, repl_high, add_node_id)
        return True
    except PxError as e:
        click.echo(f"    ✗ FAILED: {e}", err=True)
        return False


@click.command("rebalance")
@click.option("--plan",          required=True,       metavar="PLAN.csv",
              help="Plan CSV produced by 'pixie propose'.")
@click.option("--dry-run",       is_flag=True,
              help="Print commands without executing them.")
@click.option("--sleep",         default=6, show_default=True,
              help="Seconds to sleep between ha-update pair.")
@click.option("--poll-interval", default=30, show_default=True,
              help="Seconds between sync-check polls.")
@click.option("--sync-timeout",  default=600, show_default=True,
              help="Seconds to wait for replication sync before stage 2.")
@click.option("--stage",         default=0, type=click.Choice(["0","1","2"]),
              show_default=True,
              help="Run only stage 1, only stage 2, or both (0=both).")
@click.option("--namespace",     "-n", multiple=True,
              help="Limit execution to these namespace(s).")
@click.option("-v", "--verbose", is_flag=True)
def cmd(plan, dry_run, sleep, poll_interval, sync_timeout, stage, namespace, verbose):
    """Execute a rebalancing plan from a CSV file.

    \b
    Execution model (works for any HA level):
      HA=2: --repl 1 (drop) → sync → --repl 2 (add new node)
      HA=3: --repl 2 (drop) → sync → --repl 3 (add new node)

    \b
    Stage ordering:
      Stage 1: one move per volume (all volumes in parallel batches)
      [wait for RuntimeState=clean on all moved volumes]
      Stage 2: second move for volumes needing two replacements
               For HA=3 this is the second 3→2→3 cycle on the same volume.

    \b
    Examples:
      pixie rebalance --plan mimir_plan.csv
      pixie rebalance --plan mimir_plan.csv --dry-run
      pixie rebalance --plan mimir_plan.csv --stage 1   # stage 1 only
      pixie rebalance --plan mimir_plan.csv --stage 2   # stage 2 only (after manual check)
      pixie rebalance --plan mimir_plan.csv -n mimir    # limit to namespace
    """
    rows = read_plan_csv(plan)

    # Filter to namespaces if requested
    ns_filter = set(namespace)
    if ns_filter:
        rows = [r for r in rows if r.get("namespace", "") in ns_filter]
        click.echo(f"Filtered to {len(rows)} rows in namespace(s): {', '.join(ns_filter)}", err=True)

    # Split into stage 1 and stage 2
    stage1_rows = [r for r in rows if r.get("remove_node_id", "").strip()
                   and r.get("add_node_id", "").strip()]
    stage2_rows = [r for r in rows if r.get("remove_node_id_2", "").strip()
                   and r.get("add_node_id_2", "").strip()]

    run_s1 = stage in ("0", "1")
    run_s2 = stage in ("0", "2")

    click.echo(
        f"\nPlan: {len(rows)} volumes — "
        f"{len(stage1_rows)} stage-1 moves, {len(stage2_rows)} stage-2 moves.",
        err=True,
    )
    if dry_run:
        click.echo("#!/bin/bash")
        click.echo(f"# pixie rebalance --dry-run  plan={plan}")
        click.echo(f"# {len(stage1_rows)} stage-1 moves, {len(stage2_rows)} stage-2 moves")
        click.echo()
    click.echo("[DRY RUN — no changes will be made]", err=True) if dry_run else None

    px = Pxctl(verbose=verbose) if not dry_run else None
    if dry_run:
        # Still need px for dry-run echo, just won't exec
        px = Pxctl(verbose=False)

    errors: list[str] = []

    # ── Stage 1 ───────────────────────────────────────────────────────────────
    if run_s1 and stage1_rows:
        click.echo(f"\n{'─'*60}", err=True)
        click.echo(f"Stage 1: {len(stage1_rows)} moves", err=True)
        click.echo(f"{'─'*60}", err=True)
        if dry_run:
            click.echo(f"# ── Stage 1: {len(stage1_rows)} moves ──")
            click.echo()

        for row in stage1_rows:
            ok = _execute_move(
                px=px,
                volume_id=row["volume_id"],
                volume_name=row["pvc"],
                ha=int(row.get("ha") or 2),
                remove_node_id=row["remove_node_id"],
                remove_node=row["remove_node"],
                add_node_id=row["add_node_id"],
                add_node=row["add_node"],
                dry_run=dry_run,
                sleep_s=sleep,
            )
            if not ok:
                errors.append(f"Stage-1 move failed: {row['pvc']}")

    # ── Sync check ────────────────────────────────────────────────────────────
    if run_s1 and run_s2 and stage2_rows and not dry_run:
        # Wait for all stage-1 volumes to sync before stage 2
        s1_vol_ids = list({r["volume_id"] for r in stage1_rows})
        synced = _wait_for_sync(px, s1_vol_ids, poll_interval, sync_timeout)
        if not synced:
            click.echo(
                "\nStage 2 skipped because some volumes did not sync in time.\n"
                "Re-run with --stage 2 once replication is healthy.",
                err=True,
            )
            sys.exit(1)

    elif run_s1 and run_s2 and stage2_rows and dry_run:
        click.echo("\n[dry-run] Would wait for sync here before stage 2.", err=True)

    # ── Stage 2 ───────────────────────────────────────────────────────────────
    if run_s2 and stage2_rows:
        click.echo(f"\n{'─'*60}", err=True)
        click.echo(f"Stage 2: {len(stage2_rows)} moves", err=True)
        click.echo(f"{'─'*60}", err=True)
        if dry_run:
            click.echo(f"# ── Stage 2: {len(stage2_rows)} moves (run after sync) ──")
            click.echo()

        for row in stage2_rows:
            ok = _execute_move(
                px=px,
                volume_id=row["volume_id"],
                volume_name=row["pvc"],
                ha=int(row.get("ha") or 2),
                remove_node_id=row["remove_node_id_2"],
                remove_node=row["remove_node_2"],
                add_node_id=row["add_node_id_2"],
                add_node=row["add_node_2"],
                dry_run=dry_run,
                sleep_s=sleep,
            )
            if not ok:
                errors.append(f"Stage-2 move failed: {row['pvc']}")

    # ── Summary ───────────────────────────────────────────────────────────────
    click.echo(f"\n{'─'*60}", err=True)
    if errors:
        click.echo(f"Completed with {len(errors)} error(s):", err=True)
        for e in errors:
            click.echo(f"  ✗ {e}", err=True)
        sys.exit(1)
    else:
        click.echo("All moves completed successfully ✓", err=True)
