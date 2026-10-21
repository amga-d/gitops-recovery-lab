SHELL := /usr/bin/env bash
S := scripts

.DEFAULT_GOAL := help
.PHONY: help up vms kubeconfig platform status down destroy lint

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  \033[36m%-11s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

up: ## Full idempotent bring-up: VMs, k3s, kubeconfig, Gitea, Argo CD
	@$(S)/start.sh

vms: ## Create or start the VMs only
	@$(S)/provision-vms.sh

kubeconfig: ## Fetch the server kubeconfig and merge it as context "k3s"
	@$(S)/fetch-kubeconfig.sh

platform: ## Install or upgrade Gitea and Argo CD
	@$(S)/install-platform.sh

status: ## Show VMs, nodes, platform pods and access URLs
	@$(S)/status.sh

down: ## Gracefully shut down the VMs (keeps everything)
	@source $(S)/lib/common.sh && load_config && \
	for vm in $$AGENT_VM $$SERVER_VM; do \
	  [[ "$$(vm_state $$vm)" == running ]] && "$${VIRSH[@]}" shutdown $$vm || true; \
	done

destroy: ## Delete the VMs, disks and kube context (FORCE=1 skips the prompt)
	@$(S)/destroy.sh

lint: ## Syntax-check (and shellcheck, if installed) all scripts
	@for f in $(S)/*.sh $(S)/lib/*.sh; do bash -n $$f || exit 1; done
	@if command -v shellcheck >/dev/null; then shellcheck -x --severity=warning $(S)/*.sh $(S)/lib/*.sh; else echo "shellcheck not installed; ran bash -n only"; fi
