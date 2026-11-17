"""Build a reproducible Minishop release from source and a private Ed25519 seed."""

import argparse
import ast
import base64
import binascii
import hashlib
import io
import json
import os
import re
import tomllib
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "backend/minishop_blacklist"
STABLE_VERSION = re.compile(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}")


def stable_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    if not isinstance(version, str) or not STABLE_VERSION.fullmatch(version):
        raise ValueError("Expected a stable X.Y.Z project version")
    module = ast.parse((PACKAGE / "__init__.py").read_text(encoding="utf-8"))
    backend_versions = [
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
    ]
    frontend = json.loads((ROOT / "package.json").read_bytes())
    lock = json.loads((ROOT / "package-lock.json").read_bytes())
    versions = [
        *backend_versions,
        frontend["version"],
        lock["version"],
        lock["packages"][""]["version"],
    ]
    if versions != [version] * 4:
        raise ValueError("Versions in Python project metadata, plugin entry point and npm differ")
    return version


def create_key(path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))


def load_key(path: Path, environment: str | None) -> Ed25519PrivateKey:
    if environment is None:
        if path.stat().st_mode & 0o077:
            raise ValueError("Private signing key must have permissions 0600")
        raw = path.read_bytes()
    else:
        encoded = os.environ.pop(environment, "").strip()
        try:
            raw = base64.b64decode(encoded, validate=True)
        except binascii.Error:
            raise ValueError(f"{environment} must contain a base64 Ed25519 seed") from None
    if len(raw) != 32:
        raise ValueError(
            "Supply a 32-byte Ed25519 seed (base64 when passed as an environment variable)"
        )
    return Ed25519PrivateKey.from_private_bytes(raw)


def payload() -> dict[str, bytes]:
    files = {path.relative_to(ROOT).as_posix(): path for path in PACKAGE.rglob("*.py")}
    files.update({name: ROOT / name for name in ("locales/ru.json", "locales/en.json")})
    for name in ("admin/index.js", "admin/index.css"):
        files[f"frontend/{name}"] = ROOT / "frontend/dist" / name
    result = {}
    for name, path in files.items():
        if path.resolve() != path:
            raise ValueError(f"Symlink payload member: {name}")
        result[name] = path.read_bytes()
    return result


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def build_release(key: Ed25519PrivateKey, output: Path, *, verify_host: bool) -> None:
    version = stable_version()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    encoded = base64.b64encode(public).decode("ascii")
    fingerprint = hashlib.sha256(public).hexdigest()
    files = payload()
    manifest = {
        **json.loads((ROOT / "dev/package.json").read_bytes()),
        "publisher": "minishop-blacklist",
        "schema_version": 1,
        "version": version,
        "publisher_public_key": encoded,
        "publisher_fingerprint": fingerprint,
        "files": {name: hashlib.sha256(body).hexdigest() for name, body in files.items()},
    }
    manifest_body = canonical(manifest)
    members = {
        "plugin.json": manifest_body,
        "signatures/ed25519.sig": key.sign(manifest_body),
        **files,
    }
    archive = io.BytesIO()
    # A fixed file order, metadata, and no compressor keep the artifact byte-for-byte reproducible.
    with ZipFile(archive, "w", compression=ZIP_STORED) as bundle:
        for name, body in sorted(members.items()):
            item = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            item.create_system = 3
            item.external_attr = 0o100644 << 16
            bundle.writestr(item, body)
    archive_body = archive.getvalue()
    if verify_host:
        from bot.plugins.packages import inspect_archive

        inspect_archive(output / "host-validation", archive_body)
    artifact = f"packages/minishop-blacklist-{version}.zip"
    digest = hashlib.sha256(archive_body).hexdigest()
    artifact_path = output / artifact
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with artifact_path.open("xb") as stream:
            stream.write(archive_body)
    except FileExistsError:
        if artifact_path.read_bytes() != archive_body:
            raise ValueError(f"Output already contains different bytes for {artifact}") from None
    (output / "minishop-plugin.json").write_bytes(
        canonical({"schema_version": 1, "version": version, "sha256": digest, "artifact": artifact})
        + b"\n"
    )
    (output / "publisher.pub").write_text(encoded + "\n", encoding="ascii")
    (output / "SHA256SUMS").write_text(f"{digest}  {artifact}\n", encoding="ascii")
    print(f"Built signed package {artifact_path}")
    print(f"SHA-256: {digest}\nPublisher fingerprint: {fingerprint}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init-key", "build"))
    signing = parser.add_mutually_exclusive_group()
    signing.add_argument(
        "--key", type=Path, default=ROOT / ".local/publisher/minishop-blacklist.key"
    )
    signing.add_argument("--key-env", metavar="NAME")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/release")
    parser.add_argument(
        "--verify-host", action="store_true", help="Use the Minishop package verifier"
    )
    options = parser.parse_args()
    try:
        if options.action == "init-key":
            if options.key_env:
                parser.error("init-key requires a signing key path")
            create_key(options.key)
            print(f"Created private signing key {options.key}")
        else:
            build_release(
                load_key(options.key, options.key_env),
                options.output,
                verify_host=options.verify_host,
            )
    except (KeyError, OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
