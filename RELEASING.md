# Release Process

## Pre-release

- [ ] All changes on a feature branch (e.g. `v1.3`), NOT on `main`
- [ ] Tests pass: `python3 tests.py`
- [ ] README updated (features, endpoints, screenshots)
- [ ] CHANGELOG.md updated with new version section
- [ ] Deployed and verified on NAS
- [ ] Screenshots current

## Release

- [ ] Squash feature branch to single commit
- [ ] Open PR from feature branch → `main`
- [ ] Review PR diff
- [ ] Merge PR on GitHub
- [ ] Pull main locally: `git checkout main && git pull`
- [ ] Tag: `git tag v1.X.0`
- [ ] Push tag: `git push origin v1.X.0`
- [ ] Create GitHub release from tag (copy CHANGELOG section as notes)
- [ ] Docker Hub multi-arch build runs automatically (GitHub Actions triggers on tag push)
- [ ] Verify build passed: `gh run list --repo epheterson/Zimi --limit 3`

## Post-release

- [ ] Verify Docker Hub image: `docker pull epheterson/zimi:latest`
- [ ] Update PLAN.md (mark release complete, start next version section)
- [ ] Sync to vault: `cp zimi.py ~/vault/infra/zim-reader/ && cp templates/index.html ~/vault/infra/zim-reader/templates/`

## Rules

- **Never commit directly to `main`** — always use feature branches + PRs
- **Never push to `main`** — merge via GitHub PR
- **Squash before merging** — one clean commit per release
- **Tag after merge** — tag on main, not on the feature branch
- **Docker Hub after tag** — ensures the image matches the tagged code
