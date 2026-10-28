#!/usr/bin/env python3
"""stunner-ip-sync: keeps a STUNner StaticService in sync with the public IPs of
selected nodes, then nudges the STUNner Gateway so the operator re-renders the
dataplane config (a StaticService edit alone is not picked up).

All configuration comes from environment variables; see load_config().
"""
import hashlib
import ipaddress
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import kubernetes
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

APP = "stunner-ip-sync"
MERGE_PATCH = "application/merge-patch+json"
VALID_ADDRESS_TYPES = {"ExternalIP", "InternalIP"}
RESERVED_ANNOTATION_PREFIX = "stunner.l7mp.io/"  # interpreted by STUNner

log = logging.getLogger(APP)


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
def emit(level, msg, *, exc_info=False, **fields):
    log.log(level, msg, extra={"fields": fields}, exc_info=exc_info)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record):
        ts = datetime.fromtimestamp(record.created, timezone.utc).isoformat(
            timespec="milliseconds"
        )
        line = f"{ts} {record.levelname:<8} {record.getMessage()}"
        fields = getattr(record, "fields", {})
        if fields:
            line += " " + " ".join(f"{k}={v}" for k, v in fields.items())
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging():
    """Returns the invalid LOG_LEVEL value, if any (fell back to INFO)."""
    level_name = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    fmt = (os.getenv("LOG_FORMAT") or "json").strip().lower()

    level = logging.getLevelNamesMapping().get(level_name)
    invalid = None
    if level is None:
        invalid, level = level_name, logging.INFO

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(TextFormatter() if fmt == "text" else JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.WARNING)  # keep library noise (urllib3, kubernetes) down
    log.setLevel(level)
    return invalid


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
class ConfigError(Exception):
    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class ResourceRef:
    group: str
    version: str
    plural: str
    namespace: str
    name: str

    def api_args(self):
        return asdict(self)


@dataclass(frozen=True)
class Config:
    node_selector: str
    address_types: tuple
    static_service: ResourceRef
    gateway: "ResourceRef | None"   # None when NUDGE_ENABLED=false
    nudge_annotation: str
    nudge_settle: int
    interval: int
    heartbeat: int
    max_failures: int
    kube_auth: str


def load_config():
    problems = []

    def get(name, default=None, required=False):
        raw = os.getenv(name, "").strip()
        if raw:
            return raw
        if required:
            problems.append(f"{name} is required")
        return default

    def get_int(name, default, minimum=0):
        raw = get(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            problems.append(f"{name} must be an integer, got {raw!r}")
            return default
        if value < minimum:
            problems.append(f"{name} must be >= {minimum}, got {value}")
            return default
        return value

    def get_bool(name, default):
        raw = get(name)
        if raw is None:
            return default
        if raw.lower() in ("1", "true", "yes", "on"):
            return True
        if raw.lower() in ("0", "false", "no", "off"):
            return False
        problems.append(f"{name} must be a boolean, got {raw!r}")
        return default

    static_service = ResourceRef(
        group=get("STATIC_SERVICE_GROUP", "stunner.l7mp.io"),
        version=get("STATIC_SERVICE_VERSION", "v1"),
        plural=get("STATIC_SERVICE_PLURAL", "staticservices"),
        namespace=get("STATIC_SERVICE_NAMESPACE", required=True),
        name=get("STATIC_SERVICE_NAME", required=True),
    )
    node_selector = get("NODE_SELECTOR", required=True)

    address_types = tuple(
        t.strip() for t in get("ADDRESS_TYPES", "ExternalIP").split(",") if t.strip()
    )
    bad = set(address_types) - VALID_ADDRESS_TYPES
    if bad or not address_types:
        problems.append(
            f"ADDRESS_TYPES must be a comma-separated subset of "
            f"{sorted(VALID_ADDRESS_TYPES)}, got {sorted(bad) or 'empty'}"
        )

    nudge_annotation = get("NUDGE_ANNOTATION", "ip-sync.internal/hash")
    gateway = None
    if get_bool("NUDGE_ENABLED", True):
        gateway = ResourceRef(
            group=get("GATEWAY_GROUP", "gateway.networking.k8s.io"),
            version=get("GATEWAY_VERSION", "v1"),
            plural="gateways",
            namespace=get("GATEWAY_NAMESPACE", required=True),
            name=get("GATEWAY_NAME", required=True),
        )
        if nudge_annotation.startswith(RESERVED_ANNOTATION_PREFIX):
            problems.append(
                f"NUDGE_ANNOTATION must not start with {RESERVED_ANNOTATION_PREFIX!r} "
                "(STUNner interprets that prefix)"
            )

    kube_auth = get("KUBE_AUTH", "incluster").lower()
    if kube_auth not in ("incluster", "kubeconfig"):
        problems.append(f"KUBE_AUTH must be incluster or kubeconfig, got {kube_auth!r}")

    cfg = Config(
        node_selector=node_selector,
        address_types=address_types,
        static_service=static_service,
        gateway=gateway,
        nudge_annotation=nudge_annotation,
        nudge_settle=get_int("NUDGE_SETTLE_SECONDS", 2),
        interval=get_int("SYNC_INTERVAL_SECONDS", 30, minimum=1),
        heartbeat=get_int("HEARTBEAT_SECONDS", 300),          # 0 disables
        max_failures=get_int("MAX_CONSECUTIVE_FAILURES", 0),  # 0 = never exit
        kube_auth=kube_auth,
    )
    if problems:
        raise ConfigError(problems)
    return cfg


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Discovery:
    prefixes: list
    eligible_nodes: int
    terminating_nodes: int
    skipped_nodes: list


def discover_prefixes(core, cfg):
    items = core.list_node(label_selector=cfg.node_selector).items
    nodes = [n for n in items if not n.metadata.deletion_timestamp]
    terminating = len(items) - len(nodes)

    prefixes, skipped = set(), []
    for node in nodes:
        found = False
        for addr in node.status.addresses or []:
            if addr.type not in cfg.address_types:
                continue
            try:
                ip = ipaddress.ip_address(addr.address)
            except ValueError:
                emit(logging.WARNING, "ignoring unparsable node address",
                     node=node.metadata.name, type=addr.type, address=addr.address)
                continue
            prefixes.add(f"{ip}/{ip.max_prefixlen}")
            found = True
        if not found:
            skipped.append(node.metadata.name)

    if skipped:
        emit(logging.WARNING, "skipping nodes without a usable address",
             nodes=skipped, address_types=list(cfg.address_types))
    if not nodes:
        emit(logging.WARNING, "no eligible nodes match the selector; prefixes will be cleared",
             selector=cfg.node_selector, terminating_nodes=terminating)
    elif not prefixes:
        # Never wipe a working config because addresses are temporarily missing.
        raise RuntimeError(
            "eligible nodes exist but none has a usable address; refusing to overwrite"
        )

    return Discovery(sorted(prefixes), len(nodes), terminating, skipped)


def nudge_gateway(cfg, custom, desired):
    """Annotate the Gateway with a hash of the prefixes so the operator re-renders
    the dataplane config. Idempotent: only patches when the hash differs."""
    digest = hashlib.sha256("\n".join(desired).encode()).hexdigest()[:16]
    ref = cfg.gateway.api_args()

    obj = custom.get_namespaced_custom_object(**ref)
    annotations = obj.get("metadata", {}).get("annotations") or {}
    previous = annotations.get(cfg.nudge_annotation)
    if previous == digest:
        emit(logging.DEBUG, "gateway already current",
             gateway=cfg.gateway.name, hash=digest)
        return False

    custom.patch_namespaced_custom_object(
        **ref,
        body={"metadata": {"annotations": {cfg.nudge_annotation: digest}}},
        _content_type=MERGE_PATCH,
    )
    emit(logging.INFO, "nudged gateway",
         namespace=cfg.gateway.namespace, name=cfg.gateway.name,
         annotation=cfg.nudge_annotation, previous_hash=previous, new_hash=digest)
    return True


def reconcile(cfg, core, custom, stop):
    found = discover_prefixes(core, cfg)
    desired = found.prefixes
    ref = cfg.static_service.api_args()

    current = custom.get_namespaced_custom_object(**ref)
    existing = sorted(current.get("spec", {}).get("prefixes") or [])

    changed = existing != desired
    if changed:
        custom.patch_namespaced_custom_object(
            **ref, body={"spec": {"prefixes": desired}}, _content_type=MERGE_PATCH
        )
        emit(logging.INFO, "staticservice updated",
             namespace=cfg.static_service.namespace, name=cfg.static_service.name,
             added=sorted(set(desired) - set(existing)),
             removed=sorted(set(existing) - set(desired)),
             total=len(desired), eligible_nodes=found.eligible_nodes)
        # let the operator observe the new prefixes before the Gateway event
        if cfg.gateway and cfg.nudge_settle:
            stop.wait(cfg.nudge_settle)

    nudged = nudge_gateway(cfg, custom, desired) if cfg.gateway else False
    return {"changed": changed, "nudged": nudged,
            "prefixes": len(desired), "eligible_nodes": found.eligible_nodes}


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def main():
    invalid_level = setup_logging()
    if invalid_level:
        emit(logging.WARNING, "invalid LOG_LEVEL, falling back to INFO", value=invalid_level)

    try:
        cfg = load_config()
    except ConfigError as exc:
        emit(logging.CRITICAL, "invalid configuration", problems=exc.problems)
        return 2

    stop = threading.Event()
    received = []

    def on_signal(signum, _frame):
        received.append(signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    emit(logging.INFO, "starting", app=APP, python=sys.version.split()[0],
         kubernetes_client=kubernetes.__version__, config=asdict(cfg))

    try:
        if cfg.kube_auth == "incluster":
            config.load_incluster_config()
        else:
            config.load_kube_config()
    except Exception:
        emit(logging.CRITICAL, "cannot load kubernetes client configuration",
             exc_info=True, mode=cfg.kube_auth)
        return 1

    core = client.CoreV1Api()
    custom = client.CustomObjectsApi()

    failures, cycles = 0, 0
    last_heartbeat = time.monotonic()

    while not stop.is_set():
        started = time.monotonic()
        cycles += 1
        try:
            result = reconcile(cfg, core, custom, stop)
        except ApiException as exc:
            failures += 1
            emit(logging.ERROR, "kubernetes api error", status=exc.status,
                 reason=exc.reason, body=(exc.body or "")[:500],
                 consecutive_failures=failures)
        except Exception:
            failures += 1
            emit(logging.ERROR, "reconcile failed", exc_info=True,
                 consecutive_failures=failures)
        else:
            if failures:
                emit(logging.INFO, "recovered after failures", failures=failures)
            failures = 0
            duration_ms = round((time.monotonic() - started) * 1000)
            emit(logging.DEBUG, "sync complete", duration_ms=duration_ms, **result)
            if cfg.heartbeat and time.monotonic() - last_heartbeat >= cfg.heartbeat:
                emit(logging.INFO, "heartbeat: in sync", cycles=cycles,
                     duration_ms=duration_ms, **result)
                last_heartbeat = time.monotonic()

        if cfg.max_failures and failures >= cfg.max_failures:
            emit(logging.CRITICAL, "too many consecutive failures; exiting",
                 consecutive_failures=failures, limit=cfg.max_failures)
            return 1

        stop.wait(cfg.interval)

    emit(logging.INFO, "shutting down", signal=received[0] if received else None,
         cycles=cycles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
