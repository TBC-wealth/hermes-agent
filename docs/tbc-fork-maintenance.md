# TBC fork maintenance

This repository is TBC Wealth's maintained fork of
[`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent).

## Branch and release policy

- `main` is the canonical TBC development branch. It contains current upstream
  Hermes plus the TBC integration commits.
- Production must pin an immutable `*-tbc.*` tag and exact commit SHA. It must
  not deploy a moving branch.
- Release branches and old tags are rollback history, not the place for new
  development. Never rewrite a published release tag.
- Each TBC behavior should remain an ordinary, focused commit with its tests so
  upstream changes can supersede or conflict with it visibly.

The AgentSmith repository owns the production pin. Advancing `main` here does
not update production until AgentSmith's Hermes lock is reviewed, tested, and
deployed separately.

## Syncing upstream

Configure the remotes once:

```bash
git remote add upstream https://github.com/NousResearch/hermes-agent.git
git fetch --prune upstream
```

For an upstream refresh:

1. Create a branch from the selected, recorded `upstream/main` commit.
2. Replay the still-relevant TBC commits in chronological order.
3. Drop a TBC commit only when upstream demonstrably supersedes it; record that
   decision in the pull request.
4. Resolve conflicts in favor of upstream architecture while preserving tested
   TBC behavior.
5. Run the canonical isolated Python test runner, Ruff, web/TUI checks, and
   production builds. Let the full GitHub Actions matrix pass.
6. Merge through a pull request into `main` without force-pushing it.

After merging, cut a `*-tbc.*` release tag only for a build that is intended to
become an AgentSmith candidate. Test that exact SHA through AgentSmith before
changing its production lock.
