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
