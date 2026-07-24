# omnigent-update — daily update for a fork-based checkout.
#
# Model: this checkout lives on $(BRANCH) (ops/makefile), which is always
# upstream main + one commit adding this Makefile. `make` keeps it current:
# fetch upstream -> rebase onto upstream/main -> push the branch to the fork
# (force-with-lease) -> fast-forward the fork's main -> and only if upstream
# moved (or FORCE=1): build web UI -> version smoke test -> reinstall the
# omnigent CLI -> print installed version.
#
# Remotes: origin = the fork (edespino), upstream = omnigent-ai. A missing
# upstream remote is added automatically (fresh clone of the fork).
#
# New machine:
#     git clone git@github.com:edespino/omnigent.git
#     cd omnigent && git switch ops/makefile && make
#
# Editing this file: edit -> git commit -> make (a dirty tree fails the
# guard; the push steps then back the commit up on the fork). A rebase
# conflict aborts cleanly and leaves the branch at its pre-rebase state.
# REPO must not contain spaces (make splits words on whitespace).
#
# Common use:
#     make             # = make update
#     make FORCE=1     # build/test/install even when already up to date
#     make help        # list targets

REPO         ?= $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
UPSTREAM     ?= upstream
UPSTREAM_URL ?= git@github.com:omnigent-ai/omnigent.git
ORIGIN       ?= origin
BRANCH       ?= ops/makefile
MAIN         ?= main
PYTHON       ?= 3.12
FORCE        ?=

SELF := $(abspath $(lastword $(MAKEFILE_LIST)))
GIT  := git -C "$(REPO)"
# `make -n` force-runs any recipe line containing the literal `$(MAKE)`;
# update calls the sub-make through this alias so dry runs stay side-effect
# free (fetch/rebase/push must never execute under -n).
SUBMAKE := $(MAKE)

.DEFAULT_GOAL := update
.NOTPARALLEL:
.PHONY: update remotes guard build test install version help start status stop

## update: rebase onto upstream main, sync the fork, build+test+install if changed (default)
update: remotes guard
	@set -e; \
	$(GIT) fetch $(UPSTREAM) $(MAIN); \
	old=$$($(GIT) rev-parse HEAD); \
	$(GIT) rebase $(UPSTREAM)/$(MAIN) || { \
		$(GIT) rebase --abort; \
		echo "update: rebase of $(BRANCH) onto $(UPSTREAM)/$(MAIN) conflicted; branch restored, resolve manually" >&2; \
		exit 1; }; \
	$(GIT) push $(ORIGIN) $(BRANCH) --force-with-lease; \
	$(GIT) push $(ORIGIN) $(UPSTREAM)/$(MAIN):refs/heads/$(MAIN); \
	new=$$($(GIT) rev-parse HEAD); \
	if [ "$$old" != "$$new" ] || [ -n "$(FORCE)" ]; then \
		$(SUBMAKE) -f "$(SELF)" build test install version; \
	else \
		echo "already up to date: $$($(GIT) log --oneline -1)"; \
	fi

## remotes: add the $(UPSTREAM) remote if missing (fresh clone of the fork)
remotes:
	@$(GIT) remote get-url $(UPSTREAM) >/dev/null 2>&1 || { \
		echo "==> adding missing '$(UPSTREAM)' remote: $(UPSTREAM_URL)"; \
		$(GIT) remote add $(UPSTREAM) "$(UPSTREAM_URL)"; }

## guard: abort unless the checkout is on $(BRANCH) with no local changes (untracked ignored)
guard:
	@cur=$$($(GIT) symbolic-ref --short -q HEAD) || { \
		echo "guard: $(REPO) is in detached HEAD state; switch to $(BRANCH) first" >&2; exit 1; }; \
	if [ "$$cur" != "$(BRANCH)" ]; then \
		echo "guard: $(REPO) is on '$$cur', not '$(BRANCH)'; switch back before updating" >&2; exit 1; \
	fi
	@if [ -n "$$($(GIT) status --porcelain --untracked-files=no)" ]; then \
		echo "guard: $(REPO) has local changes (staged or unstaged); commit or stash them first:" >&2; \
		$(GIT) status --short --untracked-files=no >&2; exit 1; \
	fi

## build: build the web UI bundle (same npm ci invocation as CI's e2e-ui job)
build:
	cd "$(REPO)/web" && npm ci --legacy-peer-deps --no-audit --no-fund && npm run build

## test: run the version smoke test
test:
	cd "$(REPO)" && uv run --with pytest pytest -q tests/test_version.py

## install: reinstall the omnigent CLI over the existing install (uv tool)
install:
	cd "$(REPO)" && uv tool install --force --python $(PYTHON) .

## version: print the installed CLI version (warns instead of failing if not on PATH)
version:
	@if command -v omnigent >/dev/null 2>&1; then \
		omnigent --version; \
	else \
		echo "omnigent installed but not on PATH (uv tool bin dir, e.g. ~/.local/bin)" >&2; \
	fi

## start: start the local omnigent server in the background, reusing one already running (wraps `omnigent server --background`)
start:
	@omnigent server --background

## status: report whether the local omnigent server is running; exits 0 if up, 1 if not (wraps `omnigent server status`)
status:
	@out="$$(omnigent server status)"; \
	printf '%s\n' "$$out"; \
	case "$$out" in \
		"Background server: not running."*) exit 1 ;; \
		*) exit 0 ;; \
	esac

## stop: stop the local omnigent server, no-op if it isn't running (wraps `omnigent server stop`)
stop:
	@omnigent server stop

## help: list available targets
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /'
