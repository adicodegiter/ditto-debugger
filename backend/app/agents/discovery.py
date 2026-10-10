from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml


DITTO_ROLES = {
    "policies": "Policies",
    "things": "Things",
    "things-search": "Things-Search",
    "gateway": "Gateway",
    "connectivity": "Connectivity",
}

RELATION_IMPACT_DIRECTIONS = {
    "compose_depends_on": "reverse",
    "configured_database_host": "reverse",
    "kubernetes_service_selector": "reverse",
    "pekko_cluster_communication": "bidirectional",
    "documented_persistence": "reverse",
    "documented_search_projection": "forward",
}

SAFE_KUBERNETES_LABELS = {
    "app",
    "app.kubernetes.io/name",
    "app.kubernetes.io/component",
    "app.kubernetes.io/part-of",
}


@dataclass(frozen=True)
class Component:
    id: str
    name: str
    kind: str
    source: str
    confidence: float = 1.0
    image: str | None = None
    ports: tuple[dict[str, Any], ...] = ()
    health_url: str | None = None
    metrics_url: str | None = None
    health_checks: tuple[dict[str, Any], ...] = ()
    labels: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "source": self.source,
            "confidence": self.confidence,
            "health_url": self.health_url,
            "metrics_url": self.metrics_url,
            "image": self.image,
            "ports": list(self.ports),
            "health_checks": list(self.health_checks),
            "labels": dict(self.labels),
        }


@dataclass(frozen=True)
class Relationship:
    source: str
    target: str
    relation: str
    provenance: str
    confidence: float
    evidence_type: str = "static"
    runtime_observed: bool = False

    def to_dict(self) -> dict[str, Any]:
        evidence_types = {"static": "configured", "runtime": "runtime-observed", "documented": "documented", "operator": "manual"}
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "provenance": self.provenance,
            "provenance_type": evidence_types.get(self.evidence_type, "manual"),
            "impact_direction": RELATION_IMPACT_DIRECTIONS.get(self.relation, "none"),
            "confidence": self.confidence,
            "evidence_type": self.evidence_type,
            "runtime_observed": self.runtime_observed,
        }


@dataclass(frozen=True)
class NetworkZone:
    id: str
    members: tuple[str, ...]
    source: str
    evidence_type: str = "static"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "members": list(self.members), "source": self.source, "evidence_type": self.evidence_type}


@dataclass
class DiscoveryReport:
    status: str
    components: list[Component] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    network_zones: list[NetworkZone] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "status": self.status,
            "components": [component.to_dict() for component in self.components],
            "relationships": [relationship.to_dict() for relationship in self.relationships],
            "network_zones": [zone.to_dict() for zone in self.network_zones],
            "source_files": list(self.source_files),
            "warnings": list(self.warnings),
        }


def _port_record(container_port: Any, protocol: Any = "tcp", host_ip: Any = None, host_port: Any = None) -> dict[str, Any] | None:
    try:
        parsed_container_port = int(container_port)
        parsed_host_port = int(host_port) if host_port is not None else None
    except (TypeError, ValueError):
        return None
    if not 1 <= parsed_container_port <= 65535 or (parsed_host_port is not None and not 1 <= parsed_host_port <= 65535):
        return None
    return {
        "container_port": parsed_container_port,
        "protocol": str(protocol or "tcp").lower(),
        "host_ip": str(host_ip) if host_ip else None,
        "host_port": parsed_host_port,
    }


def _compose_port(value: Any) -> dict[str, Any] | None:
    if isinstance(value, int):
        return _port_record(value)
    if not isinstance(value, str):
        return None
    mapping, _, protocol = value.partition("/")
    parts = mapping.split(":")
    if len(parts) == 1:
        return _port_record(parts[0], protocol or "tcp")
    if len(parts) == 2:
        return _port_record(parts[1], protocol or "tcp", host_port=parts[0])
    if len(parts) == 3:
        return _port_record(parts[2], protocol or "tcp", host_ip=parts[0], host_port=parts[1])
    return None


def _environment_map(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        output: dict[str, str] = {}
        for entry in value:
            if isinstance(entry, str) and "=" in entry:
                key, item = entry.split("=", 1)
                output[key] = item
        return output
    return {}


def _service_kind(name: str, image: str | None) -> tuple[str, str | None]:
    normalized_image = (image or "").lower()
    normalized_name = name.lower()
    role = DITTO_ROLES.get(normalized_name)
    image_name = normalized_image.rsplit("/", 1)[-1].split(":", 1)[0]
    for prefix in ("eclipse-ditto-", "ditto-"):
        if image_name.startswith(prefix):
            role = role or DITTO_ROLES.get(image_name.removeprefix(prefix))
            break
    if role or "eclipse/ditto-" in normalized_image:
        return "ditto_service", role
    if "mongo" in normalized_image or normalized_name in {"mongodb", "mongo"}:
        return "database", None
    if "nginx" in normalized_image or normalized_name == "nginx":
        return "proxy", None
    if normalized_name in {"ditto-ui", "swagger-ui"}:
        return "support", None
    return "support", None


def _health_metadata(name: str, definition: dict[str, Any]) -> tuple[str | None, tuple[dict[str, Any], ...]]:
    healthcheck = definition.get("healthcheck")
    if not isinstance(healthcheck, dict):
        return None, ()
    test = healthcheck.get("test", [])
    joined = " ".join(str(part) for part in test) if isinstance(test, list) else str(test)
    url = None
    if "/alive" in joined and "7626" in joined:
        url = f"http://{name}:7626/alive"
    record: dict[str, Any] = {"type": "compose_healthcheck", "url": url, "evidence_type": "static"}
    for key in ("interval", "timeout", "retries", "start_period"):
        if key in healthcheck and isinstance(healthcheck[key], (int, float, str)):
            record[key] = healthcheck[key]
    return url, (record,)


def _safe_kubernetes_labels(labels: Any) -> dict[str, str]:
    if not isinstance(labels, dict):
        return {}
    return {key: str(labels[key]) for key in SAFE_KUBERNETES_LABELS if key in labels}


class DittoDiscoveryAgent:
    """Read Compose and Kubernetes YAML without executing or contacting workloads."""

    name = "Ditto Discovery Agent"

    def __init__(self, compose_file: Path, kubernetes_manifests: Iterable[Path] = ()) -> None:
        self.compose_file = Path(compose_file)
        self.kubernetes_manifests = tuple(Path(path) for path in kubernetes_manifests)

    def discover(self) -> DiscoveryReport:
        paths = ([self.compose_file] if self.compose_file.is_file() else []) + [
            path for path in self.kubernetes_manifests if path.is_file()
        ]
        if not paths:
            return DiscoveryReport(
                status="unavailable",
                warnings=["No Ditto Compose file or Kubernetes manifest was found; no topology was fabricated."],
            )

        components: dict[str, Component] = {}
        relationships: list[Relationship] = []
        network_members: dict[str, set[str]] = {}
        workload_labels: list[tuple[str, dict[str, str]]] = []
        kubernetes_services: list[tuple[Component, dict[str, str], str]] = []
        warnings: list[str] = []
        source_files: list[str] = []

        for path in paths:
            source_files.append(path.name)
            try:
                with path.open("r", encoding="utf-8") as stream:
                    documents = list(yaml.safe_load_all(stream))
            except (OSError, yaml.YAMLError) as error:
                warnings.append(f"Could not parse {path.name}: {type(error).__name__}")
                continue
            for document in documents:
                if not isinstance(document, dict):
                    continue
                if isinstance(document.get("services"), dict):
                    self._read_compose(document, components, relationships, network_members, warnings)
                else:
                    self._read_kubernetes(document, components, relationships, workload_labels, kubernetes_services, warnings)

        for service_component, selector, provenance in kubernetes_services:
            if not selector:
                continue
            for workload_id, labels in workload_labels:
                if all(labels.get(key) == value for key, value in selector.items()):
                    relationships.append(
                        Relationship(
                            source=service_component.id,
                            target=workload_id,
                            relation="kubernetes_service_selector",
                            provenance=provenance,
                            confidence=1.0,
                        )
                    )

        zones = [NetworkZone(zone_id, tuple(sorted(members)), "compose:services.<name>.networks") for zone_id, members in sorted(network_members.items())]
        status = "available" if components else "unavailable"
        if len(paths) and not components:
            warnings.append("Manifest files were readable, but no supported service/workload resources were found.")
        return DiscoveryReport(
            status=status,
            components=sorted(components.values(), key=lambda item: item.id),
            relationships=relationships,
            network_zones=zones,
            source_files=source_files,
            warnings=warnings,
        )

    def _read_compose(
        self,
        document: dict[str, Any],
        components: dict[str, Component],
        relationships: list[Relationship],
        network_members: dict[str, set[str]],
        warnings: list[str],
    ) -> None:
        services = document["services"]
        for name, definition in services.items():
            if not isinstance(definition, dict):
                warnings.append(f"Ignored Compose service {name!s}: service definition is not a mapping.")
                continue
            service_id = str(name)
            image = definition.get("image") if isinstance(definition.get("image"), str) else None
            kind, role = _service_kind(service_id, image)
            ports = tuple(record for item in definition.get("ports", []) if (record := _compose_port(item)) is not None)
            health_url, health_checks = _health_metadata(service_id, definition)
            labels = {"manifest_kind": "compose"}
            if image:
                labels["image"] = image
            if role:
                labels["ditto_role"] = role
            metrics_url = f"http://{service_id}:9095/" if kind == "ditto_service" else None
            if metrics_url:
                labels["metrics_endpoint_scope"] = "compose_network"
            components[service_id] = Component(
                id=service_id,
                name=service_id,
                kind=kind,
                source="compose",
                image=image,
                ports=ports,
                health_url=health_url,
                metrics_url=metrics_url,
                health_checks=health_checks,
                labels=labels,
            )

        for name, definition in services.items():
            if not isinstance(definition, dict):
                continue
            service_id = str(name)
            declared = definition.get("depends_on", [])
            dependencies = declared.keys() if isinstance(declared, dict) else declared if isinstance(declared, list) else []
            for dependency in dependencies:
                dependency_id = str(dependency)
                if dependency_id in services:
                    relationships.append(
                        Relationship(service_id, dependency_id, "compose_depends_on", f"compose:services.{service_id}.depends_on", 1.0)
                    )
            environment = _environment_map(definition.get("environment"))
            for variable in ("MONGO_DB_HOSTNAME", "MONGODB_HOST", "MONGO_HOSTNAME"):
                target = environment.get(variable)
                if target in services:
                    relationships.append(
                        Relationship(service_id, target, "configured_database_host", f"compose:services.{service_id}.environment.{variable}", 1.0)
                    )
            declared_networks = definition.get("networks", ["default"])
            network_names = declared_networks.keys() if isinstance(declared_networks, dict) else declared_networks
            if isinstance(network_names, list) or hasattr(network_names, "__iter__"):
                for network_name in network_names:
                    network_members.setdefault(str(network_name), set()).add(service_id)

    def _read_kubernetes(
        self,
        document: dict[str, Any],
        components: dict[str, Component],
        relationships: list[Relationship],
        workload_labels: list[tuple[str, dict[str, str]]],
        kubernetes_services: list[tuple[Component, dict[str, str], str]],
        warnings: list[str],
    ) -> None:
        if document.get("kind") == "List":
            items = document.get("items", [])
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict):
                    self._read_kubernetes(item, components, relationships, workload_labels, kubernetes_services, warnings)
            return

        kind = document.get("kind")
        metadata = document.get("metadata", {})
        spec = document.get("spec", {})
        if not isinstance(metadata, dict) or not isinstance(spec, dict):
            return
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            return
        namespace = str(metadata.get("namespace", "default"))

        if kind == "Service":
            selector = spec.get("selector", {})
            service_id = f"{namespace}/{name}"
            raw_ports = spec.get("ports", [])
            service_ports = []
            for item in raw_ports if isinstance(raw_ports, list) else []:
                if not isinstance(item, dict):
                    continue
                record = _port_record(item.get("targetPort", item.get("port")), item.get("protocol", "tcp"), host_port=item.get("nodePort"))
                if record:
                    record["service_port"] = item.get("port") if isinstance(item.get("port"), int) else None
                    record["target_port"] = item.get("targetPort") if isinstance(item.get("targetPort"), (int, str)) else None
                    record["node_port"] = item.get("nodePort") if isinstance(item.get("nodePort"), int) else None
                    service_ports.append(record)
            component = Component(
                id=service_id,
                name=name,
                kind="kubernetes_service",
                source="kubernetes",
                ports=tuple(service_ports),
                labels={"manifest_kind": "kubernetes", "namespace": namespace},
            )
            components[service_id] = component
            if isinstance(selector, dict):
                kubernetes_services.append((component, {str(key): str(value) for key, value in selector.items()}, f"kubernetes:Service/{namespace}/{name}.spec.selector"))
            return

        if kind not in {"Deployment", "StatefulSet", "DaemonSet", "Pod", "Job", "CronJob"}:
            return
        pod_spec, pod_metadata = self._pod_template(kind, spec)
        if not isinstance(pod_spec, dict):
            return
        if kind == "Pod":
            pod_metadata = metadata
        template_labels = pod_metadata.get("labels", {}) if isinstance(pod_metadata, dict) else {}
        safe_labels = _safe_kubernetes_labels(template_labels)
        matching_labels = {str(key): str(value) for key, value in template_labels.items()} if isinstance(template_labels, dict) else {}
        selector = spec.get("selector", {})
        selector_labels = selector.get("matchLabels", {}) if isinstance(selector, dict) else {}
        if isinstance(selector_labels, dict):
            matching_labels.update({str(key): str(value) for key, value in selector_labels.items()})
        containers = pod_spec.get("containers", [])
        if not isinstance(containers, list):
            return
        for container in containers:
            if not isinstance(container, dict) or not isinstance(container.get("name"), str):
                continue
            container_name = container["name"]
            component_id = f"{namespace}/{name}/{container_name}"
            image = container.get("image") if isinstance(container.get("image"), str) else None
            kind_value, role = _service_kind(name, image)
            ports: list[dict[str, Any]] = []
            for item in container.get("ports", []) if isinstance(container.get("ports", []), list) else []:
                if isinstance(item, dict):
                    record = _port_record(item.get("containerPort"), item.get("protocol", "TCP"), host_port=item.get("hostPort"))
                    if record:
                        ports.append(record)
            checks = []
            for probe_name in ("startupProbe", "readinessProbe", "livenessProbe"):
                probe = container.get(probe_name)
                if isinstance(probe, dict):
                    http_get = probe.get("httpGet", {})
                    checks.append({
                        "type": probe_name,
                        "http_path": http_get.get("path") if isinstance(http_get, dict) else None,
                        "http_port": http_get.get("port") if isinstance(http_get, dict) else None,
                        "evidence_type": "static",
                    })
            labels = {"manifest_kind": "kubernetes", "namespace": namespace, **safe_labels}
            if role:
                labels["ditto_role"] = role
            components[component_id] = Component(
                id=component_id,
                name=f"{name}/{container_name}",
                kind=kind_value,
                source="kubernetes",
                image=image,
                ports=tuple(ports),
                health_checks=tuple(checks),
                labels=labels,
            )
            workload_labels.append((component_id, matching_labels))

    @staticmethod
    def _pod_template(kind: str, spec: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        if kind == "Pod":
            return spec, {}
        if kind == "CronJob":
            job_template = spec.get("jobTemplate", {}).get("spec", {}).get("template", {})
        else:
            job_template = spec.get("template", {})
        if not isinstance(job_template, dict):
            return None, {}
        template_spec = job_template.get("spec")
        template_metadata = job_template.get("metadata", {})
        return (template_spec if isinstance(template_spec, dict) else None, template_metadata if isinstance(template_metadata, dict) else {})