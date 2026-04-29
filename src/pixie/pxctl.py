"""pxctl execution layer.

Discovers the Portworx pod via the current kube context and runs
pxctl commands inside it, returning parsed JSON where pxctl supports it.

Node topology is sourced from Portworx pool labels:
    topology.portworx.io/datacenter
    topology.portworx.io/region
    topology.portworx.io/zone

These are populated via `pxctl service pool show --json`, with a fallback
to `kubectl get nodes -o json` if the pool command is unavailable.
"""
from __future__ import annotations
import json
import subprocess
import sys
from typing import Any


PXCTL = "/opt/pwx/bin/pxctl"
PX_NAMESPACE = "portworx"
PX_LABEL     = "name=portworx"

# Portworx topology label prefix
TOPO_PREFIX = "topology.portworx.io/"

# Known Portworx CSI/in-tree provisioner names
PX_PROVISIONERS = frozenset({
    "pxd.portworx.com",
    "kubernetes.io/portworx-volume",
})


class PxError(Exception):
    pass


def _px_pod() -> str:
    """Resolve the first running Portworx pod in the current context."""
    try:
        result = subprocess.run(
            [
                "kubectl", "get", "pods",
                "-l", PX_LABEL,
                "-n", PX_NAMESPACE,
                "-o", "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True, text=True, check=True,
        )
        pod = result.stdout.strip()
        if not pod:
            raise PxError(
                f"No pods found with label '{PX_LABEL}' in namespace '{PX_NAMESPACE}'.\n"
                "Check your kube context or set PX_NAMESPACE / PX_LABEL environment variables."
            )
        return pod
    except subprocess.CalledProcessError as e:
        raise PxError(f"kubectl failed: {e.stderr.strip()}") from e
    except FileNotFoundError:
        raise PxError("kubectl not found — is it installed and on PATH?")


def _run(pod: str, args: list[str], json_output: bool = True) -> Any:
    """Run a pxctl command and return parsed JSON (or raw text)."""
    cmd = ["kubectl", "exec", pod, "-n", PX_NAMESPACE, "--", PXCTL]
    if json_output:
        cmd += ["--json"] + args
    else:
        cmd += args

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        raise PxError(f"pxctl error: {e.stderr.strip() or e.stdout.strip()}") from e

    if json_output:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            # Some pxctl commands ignore --json; fall back to raw
            return result.stdout
    return result.stdout




class Pxctl:
    """High-level pxctl interface."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._pod: str | None = None

    @property
    def pod(self) -> str:
        if self._pod is None:
            self._pod = _px_pod()
            if self.verbose:
                print(f"  [pxctl] using pod: {self._pod}", file=sys.stderr)
        return self._pod

    def run(self, *args, json_output: bool = True) -> Any:
        if self.verbose:
            print(f"  [pxctl] {' '.join(args)}", file=sys.stderr)
        return _run(self.pod, list(args), json_output=json_output)

    # ── Cluster / node info ───────────────────────────────────────────────────

    def cluster_list(self) -> dict:
        """pxctl cluster list --json"""
        return self.run("cluster", "list")

    def node_list(self) -> list[dict]:
        """Return list of storage nodes with id, ip, hostname, used, capacity, topology.

        All data comes from pxctl cluster list (status) --json:
          - Capacity/used: summed from Pools[].TotalSize and Pools[].Used (ints)
          - Topology: topology.portworx.io/* labels from Pools[0].labels
          - Status: integer (2 = online)
          - IP: MgmtIp / DataIp
        """
        data = self.cluster_list()
        nodes = []
        for n in data.get("cluster", {}).get("Nodes", []):
            pools = n.get("Pools", [])

            # Capacity: sum across all pools
            cap_bytes  = sum(p.get("TotalSize", 0) or 0 for p in pools)
            used_bytes = sum(p.get("Used",      0) or 0 for p in pools)

            # Topology: from first pool's labels, filter to topology.portworx.io/*
            topology: dict[str, str] = {}
            if pools:
                all_labels = pools[0].get("labels", {}) or {}
                for k, v in all_labels.items():
                    if k.startswith(TOPO_PREFIX):
                        topology[k.replace(TOPO_PREFIX, "")] = str(v)

            # Status: 2 = online
            status_int = n.get("Status", 0)
            status_str = "Online" if status_int == 2 else f"Status({status_int})"

            nodes.append({
                "id":       n.get("Id", ""),
                "ip":       n.get("DataIp", n.get("MgmtIp", "")),
                "hostname": n.get("SchedulerNodeName", ""),
                "used_gib": used_bytes / (1024 ** 3),
                "cap_gib":  cap_bytes  / (1024 ** 3),
                "status":   status_str,
                "topology": topology,
            })
        return nodes

    # ── Volume info ───────────────────────────────────────────────────────────

    def volume_list(
        self,
        namespace: str | None = None,
        label: str | None = None,
        name: str | None = None,
        node: str | None = None,
        pool_uid: str | None = None,
        group: str | None = None,
        parent: str | None = None,
        all_vols: bool = False,
        snapshots_only: bool = False,
        volumes_only: bool = False,
    ) -> list[dict]:
        """pxctl volume list with full flag support.

        Args:
            namespace:      filter by namespace= label (convenience wrapper)
            label:          raw label filter string, e.g. "app=mimir,zone=pod04"
            name:           filter by volume name
            node:           show volumes with a replica on this node (ID or hostname)
            pool_uid:       show volumes with a replica in this pool UUID
            group:          show all volumes in a volume group
            parent:         show snapshots of this parent volume ID
            all_vols:       include snapshots (-a / --all)
            snapshots_only: show only snapshots (-s / --snapshot)
            volumes_only:   show only non-snapshot volumes (-v / --volumes)
        """
        args = ["volume", "list"]

        # Build label filter — namespace is sugar for label=namespace=<ns>
        label_parts = []
        if namespace:
            label_parts.append(f"namespace={namespace}")
        if label:
            label_parts.append(label)
        if label_parts:
            args += ["--label", ",".join(label_parts)]

        if name:
            args += ["--name", name]
        if node:
            args += ["--node", node]
        if pool_uid:
            args += ["--pool-uid", pool_uid]
        if group:
            args += ["--group", group]
        if parent:
            args += ["--parent", parent]
        if all_vols:
            args += ["--all"]
        if snapshots_only:
            args += ["--snapshot"]
        if volumes_only:
            args += ["--volumes"]

        data = self.run(*args)
        return data.get("volumes", data) if isinstance(data, dict) else (data or [])

    def volume_inspect(self, volume_id: str) -> dict:
        """Inspect a single volume by ID."""
        return self.run("volume", "inspect", volume_id)

    def _is_px_pvc(self, item: dict) -> bool:
        """Return True if a PVC item is backed by Portworx.

        Checks (in order):
          1. volume.beta.kubernetes.io/storage-provisioner annotation
          2. storageClassName starting with "px-" (fallback for older clusters)
        """
        annotations = (item.get("metadata") or {}).get("annotations") or {}
        provisioner = annotations.get("volume.beta.kubernetes.io/storage-provisioner", "")
        if provisioner in PX_PROVISIONERS:
            return True
        sc = (item.get("spec") or {}).get("storageClassName", "") or ""
        return sc.startswith("px-")

    def _kubectl_px_pvcs(self, namespace: str) -> list[tuple[str, str]]:
        """Return (pvc_name, pv_name) tuples for Portworx PVCs in *namespace*.

        pv_name (spec.volumeName, e.g. pvc-1c904458-...) is used directly
        with `pxctl volume inspect` to avoid a redundant volume list lookup.
        """
        try:
            result = subprocess.run(
                ["kubectl", "get", "pvc", "-n", namespace, "-o", "json"],
                capture_output=True, text=True, check=True,
            )
            data = json.loads(result.stdout)
        except subprocess.CalledProcessError as e:
            print(f"  [kubectl] warning: get pvc failed: {e.stderr.strip()}", file=sys.stderr)
            return []
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print(f"  [kubectl] warning: {e}", file=sys.stderr)
            return []

        items = data.get("items", [])
        pvcs = [
            (item["metadata"]["name"], (item.get("spec") or {}).get("volumeName", ""))
            for item in items
            if self._is_px_pvc(item)
        ]

        if self.verbose:
            print(f"  [kubectl] {len(items)} PVC(s) in namespace, {len(pvcs)} Portworx", file=sys.stderr)
            non_px = [i["metadata"]["name"] for i in items if not self._is_px_pvc(i)]
            if non_px:
                print(f"  [kubectl] skipped (non-Portworx): {non_px}", file=sys.stderr)

        return pvcs

    def _inspect_volume_list(self, vols: list[dict]) -> list[dict]:
        """Inspect each volume in *vols*, returning fully-detailed dicts."""
        detailed = []
        for v in vols:
            vid = v.get("id") or v.get("Id")
            if not vid:
                continue
            try:
                detail = self.volume_inspect(str(vid))
                if isinstance(detail, list):
                    detail = detail[0]
                detailed.append(detail)
            except PxError as e:
                if self.verbose:
                    print(f"  [pxctl] warning: inspect {vid} failed: {e}", file=sys.stderr)
                detailed.append(v)
        return detailed

    def fetch_volumes(self, list_kwargs: dict) -> list[dict]:
        """List volumes with given filters, then inspect each for full detail.

        When fetching by namespace, also queries kubectl to find any Portworx PVCs
        that pxctl missed due to missing namespace labels, and fetches those by name.

        list_kwargs are passed directly to volume_list().
        Returns fully-inspected volume dicts.
        """
        vols = self.volume_list(**list_kwargs)
        detailed = self._inspect_volume_list(vols)

        namespace = list_kwargs.get("namespace")
        if not namespace:
            return detailed

        # Build the set of PVC names already retrieved
        fetched_pvcs: set[str] = set()
        for d in detailed:
            locator = d.get("locator") or {}
            labels = (locator.get("volume_labels") or
                      (d.get("spec") or {}).get("volume_labels") or
                      d.get("labels") or {})
            pvc_name = labels.get("pvc", "") if isinstance(labels, dict) else ""
            if pvc_name:
                fetched_pvcs.add(pvc_name)

        # Compare against what kubectl sees
        kube_pvcs = self._kubectl_px_pvcs(namespace)
        missing = [(pvc, pv) for pvc, pv in kube_pvcs if pvc not in fetched_pvcs]

        if missing:
            print(
                f"  [{len(missing)} PVC(s) missing from pxctl label query; "
                f"fetching by volume name]",
                file=sys.stderr,
            )
            if self.verbose:
                for pvc_name, pv_name in missing:
                    print(f"    {pvc_name} ({pv_name})", file=sys.stderr)

            for pvc_name, pv_name in missing:
                if not pv_name:
                    if self.verbose:
                        print(f"  [pxctl] warning: no volumeName for PVC {pvc_name!r}, skipping", file=sys.stderr)
                    continue
                try:
                    detail = self.volume_inspect(pv_name)
                    if isinstance(detail, list):
                        detail = detail[0]
                    # Backfill namespace/pvc labels that pxctl omitted
                    locator = detail.setdefault("locator", {})
                    vol_labels = locator.setdefault("volume_labels", {})
                    vol_labels.setdefault("namespace", namespace)
                    vol_labels.setdefault("pvc", pvc_name)
                    detailed.append(detail)
                except PxError as e:
                    if self.verbose:
                        print(f"  [pxctl] warning: inspect {pv_name!r} failed: {e}", file=sys.stderr)

        return detailed

    # ── HA update commands ────────────────────────────────────────────────────

    def ha_update(self, volume_id: str, repl: int, node_id: str) -> str:
        """pxctl volume ha-update --repl N <vol> --node <node_id>"""
        return self.run(
            "volume", "ha-update",
            "--repl", str(repl),
            volume_id,
            "--node", node_id,
            json_output=False,
        )

    def volume_status(self, volume_id: str) -> dict:
        """Get replication status for a volume."""
        detail = self.volume_inspect(volume_id)
        if isinstance(detail, list):
            detail = detail[0]
        return detail



