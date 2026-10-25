SHELL := /usr/bin/env bash
S := scripts

.DEFAULT_GOAL := help
.PHONY: help up vms kubeconfig platform images app-image seed runner-env trial pilot run extract analyze status down destroy lint

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  \033[36m%-11s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

up: ## Full idempotent bring-up: VMs, k3s, kubeconfig, platform
	@$(S)/start.sh

vms: ## Create or start the VMs only
	@$(S)/provision-vms.sh

kubeconfig: ## Fetch the server kubeconfig and merge it as the configured kube context
	@$(S)/fetch-kubeconfig.sh

platform: ## Install or upgrade the platform (Gitea, monitoring, Argo CD, Rollouts, Chaos Mesh)
	@$(S)/install-platform.sh

PY := .state/venv/bin/python
MTTR := cd runner && ../$(PY) -m mttr

images: ## Save every image on the running nodes to .state/images (later builds import them)
	@$(S)/images.sh save

app-image: ## Build the sample app and import it into every node
	@$(S)/build-app.sh

seed: ## Seed the GitOps repo, webhook, analysis templates and Argo CD apps
	@$(S)/seed-gitops.sh

runner-env: ## Create the Python environment for the runner and analysis
	@command -v uv >/dev/null || { echo "uv is required"; exit 1; }
	@[ -x $(PY) ] || uv venv -q -p python3 .state/venv
	@VIRTUAL_ENV=.state/venv uv pip install -q -r runner/requirements.txt

trial: ## One trial: make trial STRATEGY=canary SCENARIO=faulty-release SYNC=auto REVERT=webhook
	@$(MTTR) trial --strategy $(STRATEGY) --scenario $(SCENARIO) --sync $(or $(SYNC),auto) --revert-path $(or $(REVERT),webhook)

pilot: ## Run or resume the randomised trial plan (REPS=3 SEED=...)
	@$(MTTR) pilot --reps $(or $(REPS),3) $(if $(SEED),--seed $(SEED))

run: ## Run or resume the main design (DESIGN=experiment/design.yaml REPS=7 SEED=... DRY=1)
	@$(MTTR) run --design $(or $(DESIGN),$(CURDIR)/experiment/design.yaml) --reps $(or $(REPS),7) $(if $(SEED),--seed $(SEED)) $(if $(DRY),--dry-run)

extract: ## Re-extract every recorded trial into all-trials.csv
	@$(MTTR) extract

analyze: ## Statistics, figures and report.md from the recorded trials
	@$(MTTR) analyze

status: ## Show VMs, nodes, platform pods and access URLs
	@$(S)/status.sh

down: ## Gracefully shut down the VMs (keeps everything)
	@$(S)/down.sh

destroy: ## Delete the VMs, disks and kube context (FORCE=1 skips the prompt)
	@$(S)/destroy.sh

lint: ## Syntax-check (and shellcheck, if installed) all scripts
	@for f in $(S)/*.sh $(S)/lib/*.sh; do bash -n $$f || exit 1; done
	@if command -v shellcheck >/dev/null; then shellcheck -x --severity=warning $(S)/*.sh $(S)/lib/*.sh; else echo "shellcheck not installed; ran bash -n only"; fi
