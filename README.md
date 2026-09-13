# llm-management-dist

Public mirror of two files from the private `Anro-Lab/llm-management` repo
(`scripts/`, `msi` branch):

- `scripts/windows_host_service.py`
- `scripts/linux_host_service.py`

This repo intentionally keeps only the current latest copy of each file (no
version history/tags) — every sync overwrites in place. It exists so these
files can be fetched anonymously by installers/launchers at install time
(the source repo is private and cannot be read by an unauthenticated
request; these two files are the ones that must be fetchable without a
token).
