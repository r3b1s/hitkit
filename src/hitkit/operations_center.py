"""Minimal Operations Center REST client: only the calls hitkit makes (docs/plans/incus-cluster.md).

Operations Center speaks the Incus response envelope with the same mutual TLS, so this is
IncusClient with its provisioning calls added. Its address and certificate are the
incus-cluster kit's to pin.
"""

from __future__ import annotations

import datetime as dt

from hitkit import incus, secrets, trace

CLUSTER_TIMEOUT = 600  # creating a cluster runs OpenTofu inside the VM; it took 13 s in the spike


class OperationsCenterClient(incus.IncusClient):
    def _or_none(self, path: str):
        try:
            return self.get(path)
        except incus.IncusError as e:
            if e.status == 404:
                return None
            raise

    def authenticated(self) -> bool:
        return (self.get("/1.0") or {}).get("auth") == "tls"  # Incus's own word is "trusted"

    def has_update(self) -> bool:
        """It has cached an update, which it fetches by itself once it is up."""
        updates = self.get("/1.0/provisioning/updates?recursion=1") or []
        return any(u.get("update_status") == "ready" for u in updates)

    def set_certificate(self, certificate: str, key: str) -> None:
        self.post("/1.0/system/certificate", {"certificate": certificate, "key": key})

    def registration_token(self, uses: int) -> str:
        """A short-lived token for ``uses`` servers to register with. The API hands its id back
        in a header, so the unique description finds it in the list."""
        description = f"hitkit-{secrets.hex_secret(4)()}"
        expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        self.post("/1.0/provisioning/tokens", {"uses_remaining": uses, "description": description,
                                               "expire_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")})
        tokens = self.get("/1.0/provisioning/tokens?recursion=1") or []
        token = next(t["uuid"] for t in tokens if t["description"] == description)
        trace.secret(token)
        return token

    def server(self, name: str) -> dict | None:
        return self._or_none(f"/1.0/provisioning/servers/{name}")

    def cluster(self, name: str) -> dict | None:
        return self._or_none(f"/1.0/provisioning/clusters/{name}")

    def provisioned(self, name: str) -> bool:
        """Its OpenTofu run finished. A cluster whose run failed is listed as ready all the same,
        and cannot be created again; only a finished run stores this artifact."""
        artifacts = self._or_none(f"/1.0/provisioning/clusters/{name}/artifacts") or []
        return any(a.endswith("/terraform-configuration") for a in artifacts)

    def create_cluster(self, name: str, url: str, servers: list[str], application_config: dict) -> None:
        self.request("POST", "/1.0/provisioning/clusters",
                     {"name": name, "connection_url": url, "server_names": servers, "server_type": "incus",
                      "services_config": {}, "application_seed_config": application_config},
                     timeout=CLUSTER_TIMEOUT)

    def add_servers(self, name: str, servers: list[str]) -> None:
        self.request("POST", f"/1.0/provisioning/clusters/{name}/:add-servers", {"server_names": servers},
                     timeout=CLUSTER_TIMEOUT)

    def set_cluster_certificate(self, name: str, certificate: str, key: str) -> None:
        self.put(f"/1.0/provisioning/clusters/{name}/certificate",
                 {"cluster_certificate": certificate, "cluster_certificate_key": key})
