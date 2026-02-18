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

## Desktop App Build

### Prerequisites

```bash
pip install -r requirements-desktop.txt
# Installs: libzim, PyMuPDF, pywebview, Pillow, pyinstaller
```

### Generate icons (if changed)

```bash
python assets/generate_icons.py
# Creates: assets/icon.png, assets/icon.ico, assets/icon.icns
# Requires: Pillow. Uses SF Compact Black on macOS for the Z glyph.
```

### Build .app (macOS)

```bash
pyinstaller --noconfirm zimi_desktop.spec
# Output: dist/Zimi.app (~115 MB)
# Test:   open dist/Zimi.app
```

### Build on other platforms

```bash
# Windows → dist/Zimi/ folder (zip for distribution)
pyinstaller --noconfirm zimi_desktop.spec

# Linux → dist/Zimi/ folder (tar.gz for distribution)
pyinstaller --noconfirm zimi_desktop.spec
```

### Create DMG (macOS distribution)

```bash
hdiutil create -volname Zimi -srcfolder dist/Zimi.app -ov -format UDZO dist/Zimi.dmg
# Or with create-dmg for a fancy installer: brew install create-dmg
# create-dmg --volname "Zimi" --no-internet-enable dist/Zimi.dmg dist/Zimi.app
```

### GitHub Actions

The `.github/workflows/desktop-release.yml` workflow builds for macOS, Windows, and Linux automatically when a `v*.*.*` tag is pushed. It creates a GitHub Release with:
- `Zimi.dmg` (macOS)
- `zimi-windows-amd64.zip` (Windows)
- `zimi-linux-amd64.tar.gz` (Linux)

### macOS Code Signing + Notarization

Requires an Apple Developer account ($99/yr). One-time setup:

**1. Export your certificate:**
- Open Keychain Access
- Find "Developer ID Application: Your Name (TEAMID)"
- Right-click → Export → save as `certificate.p12` (set a password)

**2. Add GitHub repo secrets** (Settings → Secrets and variables → Actions):

| Secret | Value |
|--------|-------|
| `APPLE_CERTIFICATE_BASE64` | `base64 -i certificate.p12 \| pbcopy` (paste result) |
| `APPLE_CERTIFICATE_PASSWORD` | Password you set when exporting .p12 |
| `APPLE_TEAM_ID` | 10-char team ID from developer.apple.com → Membership |
| `APPLE_ID` | Your Apple ID email |
| `APPLE_APP_PASSWORD` | App-specific password from appleid.apple.com → Sign-In and Security → App-Specific Passwords |

**3. The workflow handles the rest:**
- Imports certificate into a temporary keychain
- `codesign --deep --force --options runtime` on the .app bundle
- `xcrun notarytool submit` + `xcrun stapler staple` on the DMG
- Result: users double-click, it just works. No Gatekeeper warnings.

### Gotchas

- `dist/` and `build/` are gitignored — never commit build artifacts
- PyInstaller COPY's source at build time — rebuild after code changes
- The `.spec` file includes `templates/` and `assets/` as data files
- macOS: the BUNDLE section creates the `.app` with proper `Info.plist` (CFBundleName=Zimi, icon, bundle ID)
- The `_set_macos_app_identity()` function in `zimi_desktop.py` is a fallback for dev mode (`python zimi_desktop.py`) but the proper .app build handles Dock icon/name natively via Info.plist
- Windows build needs a Windows machine or VM (cross-compilation not supported by PyInstaller)

## Rules

- **Never commit directly to `main`** — always use feature branches + PRs
- **Never push to `main`** — merge via GitHub PR
- **Squash before merging** — one clean commit per release
- **Tag after merge** — tag on main, not on the feature branch
- **Docker Hub after tag** — ensures the image matches the tagged code
