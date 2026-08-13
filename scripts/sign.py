"""Authenticode signing for the packaged build.

    python scripts/sign.py --make-cert          # one-off: a test certificate
    python scripts/sign.py                      # sign everything in dist/
    python scripts/sign.py --verify

**A self-signed certificate does not make SmartScreen quiet.** It proves the
files were not modified after signing and gives the publisher a stable
identity, which is what internal deployment and update checks need; getting rid
of the "unknown publisher" warning needs a certificate from a CA that Windows
already trusts (OV/EV on a hardware token, or a managed service such as Azure
Trusted Signing) plus reputation built up over downloads.

Everything below works the same with a real certificate: import the .pfx, or
pass its thumbprint, and nothing else changes.

Signing runs through PowerShell's `Set-AuthenticodeSignature` rather than
`signtool.exe`, which lives in the Windows SDK and is not installed by default.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST = PROJECT_ROOT / "dist"
APP_DIR = DIST / "Boltdown"

#: honest subject: nobody should mistake this for a validated identity
TEST_SUBJECT = "CN=Boltdown Test Signing (self-signed), O=Boltdown"
TIMESTAMP_URL = "http://timestamp.digicert.com"


def powershell(script: str, timeout: float = 300) -> tuple[int, str, str]:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=timeout,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _json(text: str):
    try:
        return json.loads(text) if text else None
    except json.JSONDecodeError:
        return None


def make_cert(subject: str = TEST_SUBJECT, years: int = 3) -> str | None:
    """Create a code-signing certificate in the user's personal store.

    It stays in `CurrentUser\\My`: signing needs nothing more. Adding it to the
    trusted roots is a separate, deliberate act - the script prints the command
    but never runs it, because that changes what the whole machine trusts.
    """
    code, out, err = powershell(
        "$c = New-SelfSignedCertificate -Type CodeSigningCert "
        f"-Subject '{subject}' -CertStoreLocation Cert:\\CurrentUser\\My "
        f"-NotAfter (Get-Date).AddYears({years}) "
        "-KeyUsage DigitalSignature -KeyExportPolicy Exportable; "
        "$c.Thumbprint"
    )
    if code != 0 or not out:
        print(f"could not create the certificate: {err or out}", file=sys.stderr)
        return None
    thumbprint = out.splitlines()[-1].strip()
    print(f"created certificate {thumbprint}\n  subject: {subject}")
    print(
        "\nTo make Windows treat signatures from it as valid on THIS machine "
        "(testing only - it tells your account to trust anything signed with "
        "this key):\n"
        "  powershell -Command \"$c = Get-Item Cert:\\CurrentUser\\My\\"
        f"{thumbprint}; "
        "$s = Get-Item Cert:\\CurrentUser\\Root; $s.Open('ReadWrite'); "
        "$s.Add($c); $s.Close()\""
    )
    return thumbprint


def find_cert(thumbprint: str | None = None) -> dict | None:
    """Pick the signing certificate: the given thumbprint, or the only one."""
    query = (
        f"Get-Item Cert:\\CurrentUser\\My\\{thumbprint}"
        if thumbprint
        else "Get-ChildItem Cert:\\CurrentUser\\My -CodeSigningCert"
    )
    code, out, _err = powershell(
        f"{query} | Select-Object -First 1 "
        "| Select-Object Thumbprint, Subject, @{n='NotAfter';e={$_.NotAfter.ToString('u')}} "
        "| ConvertTo-Json -Compress"
    )
    data = _json(out) if code == 0 else None
    return data if isinstance(data, dict) else None


def targets() -> list[Path]:
    """Everything worth signing: the executables, then the installer."""
    found = sorted(APP_DIR.glob("*.exe")) if APP_DIR.is_dir() else []
    found += sorted(DIST.glob("BoltdownSetup-*.exe"))
    return found


def sign(paths: list[Path], thumbprint: str, timestamp: str | None = TIMESTAMP_URL) -> int:
    if not paths:
        print("nothing to sign", file=sys.stderr)
        return 1
    listing = ",".join(f"'{p}'" for p in paths)
    stamp = f" -TimestampServer '{timestamp}'" if timestamp else ""
    code, out, err = powershell(
        f"$c = Get-Item Cert:\\CurrentUser\\My\\{thumbprint}; "
        f"@({listing}) | ForEach-Object {{ "
        f"  $r = Set-AuthenticodeSignature -FilePath $_ -Certificate $c{stamp} "
        "    -HashAlgorithm SHA256; "
        "  '{0,-24} {1}' -f (Split-Path $_ -Leaf), $r.Status "
        "}"
    )
    print(out or err)
    if code != 0:
        return code
    # A self-signed chain reports UnknownError until the root is trusted; the
    # signature itself is attached, so that is not a failure here. Anything
    # else (HashMismatch, NotSigned...) is.
    bad = [
        line for line in out.splitlines()
        if line.strip() and not line.endswith(("Valid", "UnknownError"))
    ]
    return 1 if bad else 0


def verify(paths: list[Path]) -> int:
    if not paths:
        print("nothing to verify", file=sys.stderr)
        return 1
    listing = ",".join(f"'{p}'" for p in paths)
    code, out, err = powershell(
        f"@({listing}) | ForEach-Object {{ "
        "  $s = Get-AuthenticodeSignature $_; "
        "  '{0,-24} {1,-14} {2} {3}' -f (Split-Path $_ -Leaf), $s.Status, "
        "    $(if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { 'unsigned' }), "
        "    $(if ($s.TimeStamperCertificate) { '(timestamped)' } else { '' }) "
        "}"
    )
    print(out or err)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--make-cert", action="store_true",
                        help="create a self-signed code-signing certificate")
    parser.add_argument("--thumbprint", default=None,
                        help="certificate to sign with (default: the only one)")
    parser.add_argument("--verify", action="store_true", help="report signatures only")
    parser.add_argument("--no-timestamp", action="store_true",
                        help="skip the timestamp server (offline builds)")
    parser.add_argument("files", nargs="*", help="override what gets signed")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("Authenticode signing is a Windows feature", file=sys.stderr)
        return 2

    if args.make_cert:
        return 0 if make_cert() else 1

    paths = [Path(f).resolve() for f in args.files] or targets()
    missing = [p for p in paths if not p.is_file()]
    if missing:
        print(f"not found: {', '.join(str(p) for p in missing)}", file=sys.stderr)
        return 2

    if args.verify:
        return verify(paths)

    cert = find_cert(args.thumbprint)
    if cert is None:
        print(
            "no code-signing certificate found - run "
            "`python scripts/sign.py --make-cert` for a test one, or import "
            "your .pfx into Cert:\\CurrentUser\\My",
            file=sys.stderr,
        )
        return 2
    print(f"signing with {cert['Thumbprint']}  ({cert['Subject']}, until {cert['NotAfter']})")
    code = sign(paths, cert["Thumbprint"],
                None if args.no_timestamp else TIMESTAMP_URL)
    if code == 0:
        verify(paths)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
