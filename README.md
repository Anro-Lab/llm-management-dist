# llm-management-dist

Public, unauthenticated mirror of two files from the private
`Anro-Lab/llm-management` repo's `scripts/` directory, as they exist on that
repo's `msi` branch:

- `scripts/windows_host_service.py`
- `scripts/linux_host_service.py`

This repo intentionally keeps only the current latest copy of each file (no
version history/tags) — every sync overwrites in place. It exists so these
files can be fetched anonymously by installers/launchers at install time
(the source repo is private and cannot be read by an unauthenticated
request; these two files are the ones that must be fetchable without a
token).

## Known consumers

- `Anro-Lab/AnroMark-Launcher` (Windows launcher, repo
  `AnroMark-Launcher-win-01`): `internal/repair/host_service.go`'s
  `InstallHostService` fetches
  `https://raw.githubusercontent.com/Anro-Lab/llm-management-dist/main/scripts/windows_host_service.py`
  at install time so it installs whatever is currently mirrored here instead
  of only the copy embedded in the launcher binary at its own build time. If
  the fetch fails for any reason (offline, rate-limited, unexpected content),
  it falls back to that embedded copy — this repo being briefly stale or
  unreachable must never break an offline install.

## Signatures (added 2026-09)

`windows_host_service.py` is fetched over plain, unauthenticated HTTP by the
AnroMark launcher (`internal/repair/host_service.go`,
`fetchLatestHostServiceScript`) as a live-update mirror for its embedded
fallback copy. Since anyone can serve content at this GitHub URL's cache
layer or a MITM position, the launcher requires a valid ed25519 signature
before trusting a fetched copy over its embedded one — an unsigned or
badly-signed fetch silently falls back to the embedded version (same
fail-safe behavior as any other fetch error).

- `scripts/windows_host_service.py.sig` — base64-encoded 64-byte detached
  ed25519 signature over the exact bytes of `windows_host_service.py`.
- `scripts/linux_host_service.py.sig` — same, for `linux_host_service.py`
  (not currently signature-checked by any consumer, but signed for
  consistency and to be ready if a Linux launcher adds the same check).
- Public key (embedded in the launcher as
  `hostServiceScriptPublicKeyB64`): `AJaEtYQrg3G3utsVYxnF1tVBVIsRJ2qaPtOvdTgNU5I=`
- Private signing key: `C:\Users\William Fang\OneDrive\桌面\anrotec\host-service-signing.key`
  (raw 32-byte ed25519 seed, base64 — same non-git storage location as
  `codesign.pfx`). **Never commit this key.**

**Every time you sync `windows_host_service.py` (or `linux_host_service.py`)
from the private source repo, re-sign it before pushing:**

```powershell
.\scripts\sign-host-service.ps1 -File scripts\windows_host_service.py -PrivateKeyPath "C:\Users\William Fang\OneDrive\桌面\anrotec\host-service-signing.key"
.\scripts\sign-host-service.ps1 -File scripts\linux_host_service.py -PrivateKeyPath "C:\Users\William Fang\OneDrive\桌面\anrotec\host-service-signing.key"
git add scripts/windows_host_service.py scripts/windows_host_service.py.sig scripts/linux_host_service.py scripts/linux_host_service.py.sig
git commit -m "sync: update host service scripts + signatures"
git push
```

A push of an updated `.py` file **without** re-signing it will make the
launcher's fetch fail signature verification and silently fall back to its
(older) embedded copy — the sync will have no effect on end users until the
`.sig` is fixed.

## Automated sync (added 2026-09-13)

`.github/workflows/sync-host-service.yml` runs on a schedule (every 6 hours)
and on manual `workflow_dispatch`. Each run: fetches the current
`scripts/windows_host_service.py` / `scripts/linux_host_service.py` from the
private source repo's `msi` branch via the GitHub Contents API, compares
them byte-for-byte against what's currently committed here, and — only if
something actually changed — copies the new content in, re-signs it with
`scripts/sign-host-service.ps1`, verifies the result with
`scripts/verify-host-service.ps1` (same check the launcher itself performs),
and commits + pushes to `main`. A run where nothing changed does nothing
(no empty commits).

Required repo secrets (**Settings → Secrets and variables → Actions**):

- `SOURCE_REPO_TOKEN` — a PAT that can read `Anro-Lab/llm-management`'s
  contents. A fine-grained PAT scoped to just that one repo with
  **Contents: Read-only** is sufficient and is the recommended choice over a
  broad classic PAT.
- `HOST_SERVICE_SIGNING_KEY` — the same base64 ed25519 private key seed used
  for local manual signing (see above). It only ever touches a `0600` temp
  file for the duration of a single job run and is deleted before the job
  ends; it is never logged or echoed.

The manual steps above still work and remain the documented fallback (e.g.
for an out-of-band emergency fix, or if the workflow is ever disabled) — the
workflow is just automation of the exact same procedure.

## GitHub Releases (added 2026-09-13)

This repo's main distribution channel is still the raw file URLs on `main`
(see above) - that is what the launcher actually fetches, and it has no
notion of versions/tags. Separately, though,
`.github/workflows/sync-host-service.yml` also publishes a human-facing
GitHub Release whose tag mirrors whichever tag on the private
`Anro-Lab/llm-management` repo currently points at the `msi` branch's
HEAD commit (e.g. source `msi` HEAD == source tag `v0.12.12` => a Release
here named `v0.12.12`, with `windows_host_service.py`,
`windows_host_service.py.sig`, `linux_host_service.py`, and
`linux_host_service.py.sig` attached as downloadable assets).

- If `msi`'s HEAD commit does not exactly match a tag in the source repo
  (an untagged commit landed there), the Release step is skipped for that
  run - the Release simply stays at whatever version it last reached until
  a tagged source commit shows up on `msi`. The raw-file mirror on `main`
  is unaffected either way.
- Re-running the workflow when the matching Release already exists is a
  no-op (it does not recreate/re-upload existing assets).
- These Releases are a convenience download surface only; nothing in this
  repo or the launcher reads from them.

## Line endings

`.gitattributes` forces LF for everything in this repo. Both mirrored `.py`
files are signed as committed here; a client with Windows `git`'s common
`core.autocrlf=true` default checking this repo out **without**
`.gitattributes` would silently rewrite them to CRLF on checkout, which
changes their bytes and breaks the signature check on next re-sign/re-push
from that checkout. Do not remove `.gitattributes`.
