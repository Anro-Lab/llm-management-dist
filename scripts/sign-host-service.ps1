<#
.SYNOPSIS
  Produces a detached ed25519 signature (<file>.sig) for a script in this repo.

.DESCRIPTION
  AnroMark-Launcher's InstallHostService step (internal/repair/host_service.go
  in Anro-Lab/AnroMark-Launcher) fetches windows_host_service.py from this
  repo's `main` branch and verifies it against a detached ed25519 signature
  fetched from the same raw URL with ".sig" appended, using an ed25519
  public key embedded in the launcher binary
  (hostServiceScriptPublicKeyB64). A failed or missing signature is
  non-fatal there (it just falls back to the launcher's embedded copy of
  the script) but means the "fetch latest from GitHub" path is effectively
  dead until a valid .sig is published.

  Run this EVERY time windows_host_service.py (or linux_host_service.py) is
  synced/updated in this repo, then commit + push both the updated script
  and its regenerated .sig together. There is no PowerShell-native ed25519
  API, so this script shells out to a tiny throwaway Go program (Go must be
  on PATH) to do the actual signing; nothing is written outside a temp
  directory that is cleaned up afterwards.

.PARAMETER File
  Path to the script file to sign. Defaults to
  scripts\windows_host_service.py next to this script.

.PARAMETER PrivateKeyPath
  Path to the base64-encoded 32-byte ed25519 private key SEED (a single
  line of base64 text, no PEM/SSH wrapping) — the same format produced when
  this keypair was first generated for AnroMark-Launcher. This file must
  NEVER be committed to git or pasted anywhere. On William's machine it
  lives at:
    C:\Users\William Fang\OneDrive\桌面\anrotec\host-service-signing.key
  (the same non-git-tracked folder already used for codesign.pfx / the
  Inno Setup code-signing certificate — the established "signing material
  that never goes in a repo" location on this machine).

.EXAMPLE
  pwsh -File scripts\sign-host-service.ps1 `
      -PrivateKeyPath "C:\Users\William Fang\OneDrive\桌面\anrotec\host-service-signing.key"

.EXAMPLE
  # Sign both distributed scripts after a sync:
  $Key = "C:\Users\William Fang\OneDrive\桌面\anrotec\host-service-signing.key"
  pwsh -File scripts\sign-host-service.ps1 -File scripts\windows_host_service.py -PrivateKeyPath $Key
  pwsh -File scripts\sign-host-service.ps1 -File scripts\linux_host_service.py   -PrivateKeyPath $Key
  git add scripts\*.py scripts\*.py.sig
  git commit -m "sync + resign host service scripts"
  git push
#>
param(
    [string]$File = (Join-Path $PSScriptRoot 'windows_host_service.py'),

    [Parameter(Mandatory = $true)]
    [string]$PrivateKeyPath
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $File)) {
    throw "File not found: $File"
}
if (-not (Test-Path -LiteralPath $PrivateKeyPath)) {
    throw "Private key not found: $PrivateKeyPath"
}

$goCmd = Get-Command go -ErrorAction SilentlyContinue
if (-not $goCmd) {
    throw "Go toolchain not found on PATH - required to perform the ed25519 signing (PowerShell has no built-in ed25519 API)."
}

# Small standalone Go program: reads the base64 seed + the target file,
# writes the base64-encoded 64-byte ed25519 signature to stdout. Kept
# inline (rather than a checked-in .go file) so this script has zero
# dependency on a go.mod existing anywhere near it.
$signerSource = @'
package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"fmt"
	"os"
	"strings"
)

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: signer <keyfile> <datafile>")
		os.Exit(2)
	}
	keyRaw, err := os.ReadFile(os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, "read key:", err)
		os.Exit(1)
	}
	seed, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(keyRaw)))
	if err != nil || len(seed) != ed25519.SeedSize {
		fmt.Fprintf(os.Stderr, "key file is not a valid base64-encoded %d-byte ed25519 seed: %v\n", ed25519.SeedSize, err)
		os.Exit(1)
	}
	priv := ed25519.NewKeyFromSeed(seed)

	body, err := os.ReadFile(os.Args[2])
	if err != nil {
		fmt.Fprintln(os.Stderr, "read data:", err)
		os.Exit(1)
	}

	sig := ed25519.Sign(priv, body)
	fmt.Print(base64.StdEncoding.EncodeToString(sig))
}
'@

$tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("anro-hostservice-sign-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmpDir | Out-Null
try {
    $signerPath = Join-Path $tmpDir 'signer.go'
    Set-Content -LiteralPath $signerPath -Value $signerSource -NoNewline -Encoding UTF8

    $resolvedFile = (Resolve-Path -LiteralPath $File).ProviderPath
    $resolvedKey = (Resolve-Path -LiteralPath $PrivateKeyPath).ProviderPath

    $sigB64 = & $goCmd.Source run $signerPath $resolvedKey $resolvedFile 2>$tmpDir\stderr.txt
    if ($LASTEXITCODE -ne 0) {
        $stderrText = Get-Content -LiteralPath (Join-Path $tmpDir 'stderr.txt') -Raw -ErrorAction SilentlyContinue
        throw "go run signer failed (exit $LASTEXITCODE): $stderrText"
    }
    $sigB64 = ($sigB64 -join '').Trim()

    # Sanity check before writing anything: the launcher decodes this with
    # base64.StdEncoding and requires EXACTLY 64 raw bytes
    # (ed25519.SignatureSize) - fail loudly here instead of silently
    # publishing a .sig the launcher's verifier will just reject.
    $sigBytes = [Convert]::FromBase64String($sigB64)
    if ($sigBytes.Length -ne 64) {
        throw "produced signature is $($sigBytes.Length) bytes, want 64 (ed25519.SignatureSize)"
    }

    $sigPath = "$resolvedFile.sig"
    Set-Content -LiteralPath $sigPath -Value $sigB64 -NoNewline -Encoding ascii

    Write-Host "Signed: $resolvedFile"
    Write-Host "Wrote:  $sigPath  ($($sigBytes.Length)-byte signature, base64-encoded)"
    Write-Host ""
    Write-Host "Next: git add `"$resolvedFile`" `"$sigPath`"; git commit; git push"
}
finally {
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}
