# llm-management-dist

Public, unauthenticated mirror of two files from the private
`Anro-Lab/llm-management` repo's `scripts/` directory, as they exist on that
repo's `msi` branch:

- `scripts/windows_host_service.py`
- `scripts/linux_host_service.py`

(This repo has no `msi` branch of its own — `msi` above names the *source*
repo's branch the files are synced from. The files live in this repo at the
same relative path, on this repo's own default branch, `main`.)

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
