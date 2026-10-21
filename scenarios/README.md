# Failure scenarios (pluggable layer)

Placeholder: the contract below is a proposal and will be finalised with the
experiment design.

Each scenario is a self-contained directory:

```
scenarios/<scenario-name>/
├── scenario.env   # metadata: NAME, DESCRIPTION, TARGET_APP, TIMEOUT
├── inject.sh      # introduce the failure (e.g. push a bad commit to Gitea, delete a pod)
├── verify.sh      # exit 0 once the system has recovered (used to stop the MTTR clock)
└── revert.sh      # optional: restore the baseline state after the run
```

Scripts can `source scripts/lib/common.sh` to reuse `kc`, `hlm`, `log`, and the
lab config. Adding a scenario should never require changes outside its directory.
