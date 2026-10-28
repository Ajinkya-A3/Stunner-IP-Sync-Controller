# stunner-ip-sync controller

A small Kubernetes controller that keeps a [STUNner](https://github.com/l7mp/stunner)
`StaticService` in sync with the **public IPs of selected nodes**, then "nudges" the
STUNner `Gateway` so the operator re-renders the dataplane config.

It was built for a setup where a media server (e.g. LiveKit) runs on dedicated nodes
and STUNner must be allowed to relay traffic to those nodes' public addresses.

## How it works

Every `SYNC_INTERVAL_SECONDS` the controller:

1. Lists nodes matching `NODE_SELECTOR` (nodes being deleted are ignored).
2. Collects addresses of the configured `ADDRESS_TYPES` and converts each to a host
   prefix (`1.2.3.4/32`, or `/128` for IPv6).
3. Compares the sorted result with `spec.prefixes` of the `StaticService`. If they
   differ, it merge-patches the `StaticService`.
4. Waits `NUDGE_SETTLE_SECONDS` (only after a change) so the operator observes the new
   prefixes.
5. Patches an annotation on the `Gateway` containing a hash of the prefixes. This is
   needed because a `StaticService` edit alone is not picked up by the operator; a
   Gateway event is what triggers re-rendering. The patch is idempotent: it only
   happens when the hash differs.

### Safety behaviour

- **Never wipes a working config by accident.** If eligible nodes exist but none has a
  usable address (e.g. external IPs not yet assigned), the cycle fails with an error
  and leaves the `StaticService` untouched.
- If **no nodes** match the selector, prefixes are cleared (with a warning). This is
  intentional: a removed node's IP should stop being allowed.
- Nodes with a `deletionTimestamp` are excluded.
- Unparsable addresses are logged and skipped.
- Reserved annotation prefix `stunner.l7mp.io/` is rejected for `NUDGE_ANNOTATION`
  because STUNner interprets it.
- Handles `SIGTERM`/`SIGINT` gracefully (including during the settle wait).
- Config errors are all reported at once and exit with code `2`; Kubernetes client
  config failure exits with `1`.

## Repository layout

```
stunner-ip-sync/
├── controller.py
├── requirements.in        (+ requirements.txt generated from it)
├── Dockerfile
├── .dockerignore
├── .env                   (local development only, do not commit)
└── k8s/
    ├── stunner.yaml       (STUNner resources)
    └── ip-sync.yaml       (controller RBAC, config, Deployment)
```

## Configuration

All configuration is via environment variables. In the cluster they come from the
`stunner-ip-sync-config` ConfigMap (`envFrom`).

### Required

| Variable | Description |
|---|---|
| `STATIC_SERVICE_NAMESPACE` | Namespace of the `StaticService` |
| `STATIC_SERVICE_NAME` | Name of the `StaticService` |
| `NODE_SELECTOR` | Label selector for nodes, e.g. `livekit-dedicated=true` |
| `GATEWAY_NAMESPACE` | Namespace of the Gateway (required when `NUDGE_ENABLED=true`) |
| `GATEWAY_NAME` | Name of the Gateway (required when `NUDGE_ENABLED=true`) |

### Optional

| Variable | Default | Description |
|---|---|---|
| `ADDRESS_TYPES` | `ExternalIP` | Comma-separated subset of `ExternalIP`, `InternalIP` |
| `SYNC_INTERVAL_SECONDS` | `30` | Reconcile period (min 1) |
| `NUDGE_ENABLED` | `true` | Set `false` to skip Gateway annotation |
| `NUDGE_ANNOTATION` | `ip-sync.internal/hash` | Annotation key written on the Gateway |
| `NUDGE_SETTLE_SECONDS` | `2` | Wait between StaticService patch and Gateway nudge |
| `HEARTBEAT_SECONDS` | `300` | Info-level "in sync" log interval; `0` disables |
| `MAX_CONSECUTIVE_FAILURES` | `0` | Exit after N failed cycles; `0` = never exit |
| `LOG_LEVEL` | `INFO` | Any Python level name; invalid values fall back to INFO |
| `LOG_FORMAT` | `json` | `json` or `text` |
| `KUBE_AUTH` | `incluster` | `incluster` or `kubeconfig` (local use) |
| `STATIC_SERVICE_GROUP` / `_VERSION` / `_PLURAL` | `stunner.l7mp.io` / `v1` / `staticservices` | CRD coordinates |
| `GATEWAY_GROUP` / `GATEWAY_VERSION` | `gateway.networking.k8s.io` / `v1` | Gateway API coordinates |

> If you change `STATIC_SERVICE_NAME` or `GATEWAY_NAME`, update the `resourceNames`
> in the Role in `k8s/ip-sync.yaml` as well. RBAC is scoped to those exact objects.

## Dependencies

The only direct dependency is the official `kubernetes` client (see `requirements.in`).

The Dockerfile installs with `--require-hashes --no-deps`, so `requirements.txt` must
contain **every** transitive dependency pinned with hashes. Generate it with one of:

```bash
# pip-tools
pip install pip-tools
pip-compile --generate-hashes --allow-unsafe --strip-extras \
  --output-file requirements.txt requirements.in

# or uv
uv pip compile requirements.in --generate-hashes --python-version 3.13 \
  -o requirements.txt
```

Generate it for Python **3.13** (the runtime version) and commit the result. To
upgrade, re-run with `--upgrade` and rebuild.

## Local development

1. Create `.env` (see the sample below) and keep it out of git and the image
   (it is already in `.dockerignore`).
2. Make sure `~/.kube/config` points at the target cluster and your user has the same
   permissions as the RBAC below.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
set -a; . ./.env; set +a
python controller.py
```

Sample `.env`:

```dotenv
STATIC_SERVICE_NAMESPACE=stunner
STATIC_SERVICE_NAME=livekit-public
NODE_SELECTOR=livekit-dedicated=true
GATEWAY_NAMESPACE=stunner
GATEWAY_NAME=udp-gateway
NUDGE_ENABLED=true
NUDGE_ANNOTATION=ip-sync.internal/hash
NUDGE_SETTLE_SECONDS=2
ADDRESS_TYPES=ExternalIP
SYNC_INTERVAL_SECONDS=30
HEARTBEAT_SECONDS=300
MAX_CONSECUTIVE_FAILURES=0
LOG_LEVEL=INFO
LOG_FORMAT=json
# local only: use ~/.kube/config instead of the in-cluster service account
KUBE_AUTH=kubeconfig
```

Use `LOG_FORMAT=text LOG_LEVEL=DEBUG` for readable local output.

## Container image

Multi-stage build:

- **builder**: `python:3.13-slim-trixie`; installs hashed dependencies into
  `/app/deps` and precompiles bytecode (the runtime filesystem is read-only and
  `PYTHONDONTWRITEBYTECODE=1`).
- **runtime**: `gcr.io/distroless/python3-debian13:nonroot` (Python 3.13), user
  `65532:65532`, no shell.

The builder and runtime Python versions **must match**, because compiled
dependencies and `.pyc` files are copied across.

```bash
docker build -t <registry>/stunner-ip-sync:<tag> .
docker push <registry>/stunner-ip-sync:<tag>
# then reference it by digest in k8s/ip-sync.yaml
```

`.dockerignore` excludes `.env`, `.git`, `__pycache__`, `k8s/` and `*.md`.

## Kubernetes deployment

### Apply order

```bash
kubectl apply -f k8s/stunner.yaml      # GatewayClass, GatewayConfig, Gateway, StaticService, UDPRoute
kubectl apply -f k8s/ip-sync.yaml      # ServiceAccount, RBAC, ConfigMap, Deployment
```

`k8s/stunner.yaml` creates:

- `GatewayClass` `stunner-gatewayclass` and `GatewayConfig` `stunner-gatewayconfig`
  (realm and auth).
- `Gateway` `udp-gateway` with a `TURN-UDP` listener on port 3478. Use `UDP` if your
  operator version predates the `TURN-*` protocols.
- `StaticService` `livekit-public`, with `spec.prefixes` left empty for the
  controller to fill.
- `UDPRoute` `livekit` routing from the Gateway to that StaticService.

> **Secret:** `stunner-auth-secret` in the file contains a placeholder
> (`CHANGE_ME_SHARED_SECRET`). Do not commit a real one; create it out of band
> (Vault + External Secrets). Keep the secret short and alphanumeric, as long
> secrets with special characters have been reported to break ephemeral auth.

> **AWS NLB:** the commented `loadBalancerServiceAnnotations` in `GatewayConfig`
> are for NLB setups. UDP can't be health-checked, so use a TCP/HTTP check on the
> dataplane health port (8086, `/live`). Adjust to your load balancer controller.

### RBAC (least privilege)

| Scope | Resource | Verbs |
|---|---|---|
| ClusterRole | `nodes` | `get`, `list` |
| Role (namespaced, `resourceNames`-restricted) | `staticservices/livekit-public` | `get`, `patch` |
| Role (namespaced, `resourceNames`-restricted) | `gateways/udp-gateway` | `get`, `patch` |

### Deployment hardening

- `replicas: 1` (a single writer is enough and avoids patch races).
- `runAsNonRoot`, UID/GID 65532, `seccompProfile: RuntimeDefault`.
- `readOnlyRootFilesystem`, no privilege escalation, all capabilities dropped.
- Requests `10m` CPU / `48Mi`; memory limit `96Mi`.
- The image is pinned by digest (`<registry>/stunner-ip-sync@sha256:<digest>`).
- `reloader.stakater.com/auto: "true"` restarts the pod when the ConfigMap changes.
  Remove it if you don't run [Reloader](https://github.com/stakater/Reloader) and
  restart manually instead (`kubectl rollout restart deploy/stunner-ip-sync -n stunner`).

## Argo CD

The controller deliberately modifies two objects that Argo CD also manages. Without
configuration, Argo CD will see them as **OutOfSync** and, with `selfHeal` enabled,
**revert the controller's changes** every few minutes, wiping the live prefixes. You
need to tell Argo CD to ignore exactly those fields:

| Object | Field written by the controller | Why Argo must ignore it |
|---|---|---|
| `StaticService/livekit-public` | `/spec/prefixes` | Git holds an empty placeholder; the live value is dynamic |
| `Gateway/udp-gateway` | annotation `ip-sync.internal/hash` | Not in Git; added by the nudge |

Add this to the `Application` that deploys `k8s/stunner.yaml`:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: stunner
  namespace: argocd
spec:
  project: default
  source:
    repoURL: <git repo>
    targetRevision: main
    path: k8s
  destination:
    server: https://kubernetes.default.svc
    namespace: stunner
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - RespectIgnoreDifferences=true     # also ignore these fields when syncing, not just when diffing
  ignoreDifferences:
    - group: stunner.l7mp.io
      kind: StaticService
      name: livekit-public
      jsonPointers:
        - /spec/prefixes
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: udp-gateway
      jqPathExpressions:
        - '.metadata.annotations["ip-sync.internal/hash"]'
```

Notes:

- `RespectIgnoreDifferences=true` is the important part. Without it,
  `ignoreDifferences` only affects the *diff display*, and a sync (or self-heal) would
  still re-apply the Git version of the field and clobber the controller's value.
- If you change `NUDGE_ANNOTATION`, update the `jqPathExpressions` entry to match.
- If you change `STATIC_SERVICE_NAME` / `GATEWAY_NAME`, update the `name:` entries.
- `ignoreDifferences` only protects objects that **already exist** with those fields.
  On the very first sync Argo creates the `StaticService` with empty prefixes; the
  controller fills them within one interval. Make sure the controller is deployed
  (same Application or another) or prefixes stay empty and relayed traffic is
  rejected.
- Because Git has `prefixes: []`, check that your STUNner CRD version accepts an empty
  list on creation. If it doesn't, put a harmless placeholder in Git (for example a
  TEST-NET-1 address such as `192.0.2.1/32`); the controller replaces it, and
  Argo ignores the difference.
- Deploy `k8s/ip-sync.yaml` in the same Application or a sibling one. Keep the real
  `stunner-auth-secret` out of Git and managed by External Secrets.
- If you use Server-Side Apply (`ServerSideApply=true`), field ownership applies
  instead: the controller's merge patches take ownership of `spec.prefixes` and the
  annotation, which can produce conflicts. Client-side apply with
  `RespectIgnoreDifferences` (above) is the simpler, tested path here.

## Observability

Logs go to stdout as JSON (default) or text. Useful messages:

| Message | Level | Meaning |
|---|---|---|
| `starting` | INFO | Startup with the full effective config |
| `staticservice updated` | INFO | Prefixes changed (shows `added`/`removed`) |
| `nudged gateway` | INFO | Gateway annotation updated (previous and new hash) |
| `heartbeat: in sync` | INFO | Periodic liveness signal (`HEARTBEAT_SECONDS`) |
| `skipping nodes without a usable address` | WARNING | Selected nodes lack the configured address type |
| `no eligible nodes match the selector; prefixes will be cleared` | WARNING | StaticService will be emptied |
| `kubernetes api error` / `reconcile failed` | ERROR | A cycle failed; retried next interval |
| `recovered after failures` | INFO | First success after errors |
| `too many consecutive failures; exiting` | CRITICAL | Only if `MAX_CONSECUTIVE_FAILURES` > 0 |

Set `MAX_CONSECUTIVE_FAILURES` (e.g. `10`) if you want Kubernetes to restart the pod on
persistent failure rather than looping forever.

## Troubleshooting

- **Prefixes keep reverting to empty:** Argo CD self-heal. Check the
  `ignoreDifferences` + `RespectIgnoreDifferences=true` settings above.
- **`403 Forbidden` in logs:** the Role `resourceNames` don't match your object names,
  or the ClusterRoleBinding is missing.
- **"eligible nodes exist but none has a usable address":** nodes have no
  `ExternalIP` yet (or the cloud provider doesn't assign one). Wait, fix the node
  config, or set `ADDRESS_TYPES=InternalIP` if appropriate.
- **StaticService updates but STUNner doesn't pick up the change:** confirm
  `NUDGE_ENABLED=true`, the Gateway name matches, and the annotation isn't under
  `stunner.l7mp.io/`.
- **Config rejected at startup:** all problems are listed in one
  `invalid configuration` log line (exit code 2).
