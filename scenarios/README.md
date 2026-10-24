# Failure scenarios (plug-in layer)

Each scenario is a directory with a declarative `scenario.yaml` and, if it
needs them, the manifests it injects. Adding a scenario never changes the
runner or the measurement: the runner alone decides when the system has
recovered and records every timestamp. Only a new injector *kind* touches
the runner (`runner/mttr/scenarios.py`, `runner/mttr/trial.py`).

```
scenarios/<name>/
├── scenario.yaml   # when and where the fault lands, how it is injected
└── *.yaml          # Chaos Mesh resources it creates (optional)
```

```yaml
name: network-partition
description: ...
category: network            # free label
surface: release             # workload | release | desired-state | control-path | node
expected_recovery: rollouts  # kubernetes | rollouts | argocd | operator | config
applies_to:
  strategies: [canary, bluegreen, rolling]
  sync_policies: [auto, manual]
trigger:
  phase: during-rollout      # during-rollout | baseline
  at: release-live           # applied | release-live | stable
target:
  selector: release-under-test   # or stable
  pick: one                      # optional: one random pod of the target release
inject:
  kind: chaos-mesh               # chaos-mesh | git-commit | kubectl-patch
  manifest: networkchaos.yaml
  stop_after: 60s                # optional: the runner deletes the chaos then
confirm:                         # proof the fault took effect (not a recovery check)
  kind: chaos-injected           # chaos-injected | pod-restarts | pod-waiting | none
  within: 30s
cleanup: {kind: chaos-mesh-delete}
timeout: 600s                    # no recovery by then: the trial is censored
```

Injector kinds:
- `git-commit`: the runner commits a new release whose app container gets
  `patch.env` and/or `patch.image`.
- `chaos-mesh`: the runner creates the manifests, substituting `${TRIAL_ID}`,
  `${NAMESPACE}`, `${TARGET_HASH}` (pod-template hash of the target release)
  and `${TARGET_POD}`.
- `kubectl-patch`: a JSON patch applied to the live `resource` (bypassing Git).

Rules:
- Faults on the release under test use `phase: during-rollout`. Argo Rollouts
  only analyses and rolls back while an update is progressing.
- `selector: stable` with `phase: baseline` is for Kubernetes-only control
  conditions.
- A release whose pods never become Ready (bad image) is caught only by the
  progress deadline, so its detection time is set by configuration.
