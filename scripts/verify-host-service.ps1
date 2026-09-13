<#
.SYNOPSIS
  Verifies a detached ed25519 signature (<file>.sig) for a script in this
  repo, against the public key embedded in AnroMark-Launcher.

.DESCRIPTION
  Companion to sign-host-service.ps1. AnroMark-Launcher
  (internal/repair/host_service.go, hostServiceScriptPublicKeyB64) performs
  this exact check itself before trusting a fetched copy of
  windows_host_service.py over its embedded fallback. Run this after
  sign-host-service.ps1 (or in CI, after a fresh sync + re-sign) as a sanity
  check that what's about to be committed/pushed will actually pass the
  launcher's verification, instead of discovering a broken .sig only after
  it ships.

  Shells out to a tiny throwaway Go program (Go must be on PATH) to do the
  actual ed25519 verification - same technique as sign-host-service.ps1,
  since there is no PowerShell-native ed25519 API.

.PARAMETER File
  Path to the script file to verify. Defaults to scripts\windows_host_service.py.

.PARAMETER PublicKeyB64
  Base64-encoded 32-byte ed25519 public key. Defaults to the value embedded
  in AnroMark-Launcher today (AJaEtYQrg3G3utsVYxnF1tVBVIsRJ2qaPtOvdTgNU5I=)
  so this can be run with zero arguments for the common case. Override this
  if the launcher's key is ever rotated.

.EXAMPLE
  pwsh -File scripts\verify-host-service.ps1
  pwsh -File scripts\verify-host-service.ps1 -File scripts\linux_host_service.py
#>
param(
    [string]$File = (Join-Path $PSScriptRoot 'windows_host_service.py'),
    [string]$PublicKeyB64 = 'AJaEtYQrg3G3utsVYxnF1tVBVIsRJ2qaPtOvdTgNU5I='
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $File)) {
    throw "File not found: $File"
}
$sigPath = "$File.sig"
if (-not (Test-Path -LiteralPath $sigPath)) {
    throw "Signature file not found: $sigPath"
}

$goCmd = Get-Command go -ErrorAction SilentlyContinue
if (-not $goCmd) {
    throw "Go toolchain not found on PATH - required to perform the ed25519 verification."
}

# Small standalone Go program, mirroring sign-host-service.ps1's signer:
# reads the public key + signed file + detached signature, exits 0 with
# "OK" on stdout if valid, exits non-zero otherwise.
$verifierSource = @'
package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"fmt"
	"os"
	"strings"
)

func main() {
	if len(os.Args) != 4 {
		fmt.Fprintln(os.Stderr, "usage: verifier <pubkeyB64> <datafile> <sigfile>")
		os.Exit(2)
	}
	pubRaw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(os.Args[1]))
	if err != nil || len(pubRaw) != ed25519.PublicKeySize {
		fmt.Fprintf(os.Stderr, "invalid public key: %v\n", err)
		os.Exit(1)
	}
	body, err := os.ReadFile(os.Args[2])
	if err != nil {
		fmt.Fprintln(os.Stderr, "read data:", err)
		os.Exit(1)
	}
	sigRaw, err := os.ReadFile(os.Args[3])
	if err != nil {
		fmt.Fprintln(os.Stderr, "read sig:", err)
		os.Exit(1)
	}
	sig, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(sigRaw)))
	if err != nil || len(sig) != ed25519.SignatureSize {
		fmt.Fprintf(os.Stderr, "invalid signature encoding: %v\n", err)
		os.Exit(1)
	}
	if !ed25519.Verify(ed25519.PublicKey(pubRaw), body, sig) {
		fmt.Fprintln(os.Stderr, "SIGNATURE INVALID")
		os.Exit(1)
	}
	fmt.Println("OK")
}
'@

$tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("anro-hostservice-verify-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmpDir | Out-Null
try {
    $verifierPath = Join-Path $tmpDir 'verifier.go'
    Set-Content -LiteralPath $verifierPath -Value $verifierSource -NoNewline -Encoding UTF8

    $resolvedFile = (Resolve-Path -LiteralPath $File).ProviderPath
    $resolvedSig = (Resolve-Path -LiteralPath $sigPath).ProviderPath

    $result = & $goCmd.Source run $verifierPath $PublicKeyB64 $resolvedFile $resolvedSig 2>$tmpDir\stderr.txt
    if ($LASTEXITCODE -ne 0) {
        $stderrText = Get-Content -LiteralPath (Join-Path $tmpDir 'stderr.txt') -Raw -ErrorAction SilentlyContinue
        throw "signature verification FAILED for $resolvedFile : $stderrText"
    }
    Write-Host "Verified OK: $resolvedFile  (sig: $resolvedSig)"
}
finally {
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}
