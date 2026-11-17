"""Publish an existing version tag's package to main and GitHub Releases."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

MAIN = "refs/heads/main"
STABLE_VERSION = re.compile(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}")


def run(*command: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, capture_output=True, check=check)


def git(*arguments: str) -> str:
    return run("git", *arguments).stdout.decode().strip()


def remote_ref(ref: str) -> str | None:
    result = git("ls-remote", "--refs", "origin", ref)
    return result.split()[0] if result else None


def git_file(commit: str, name: str) -> bytes | None:
    result = run("git", "show", f"{commit}:{name}", check=False)
    return result.stdout if result.returncode == 0 else None


@dataclass(frozen=True)
class Package:
    version: str
    artifact: str
    files: dict[str, bytes]

    @classmethod
    def load(cls, directory: Path) -> "Package":
        index_path = directory / "minishop-plugin.json"
        index = json.loads(index_path.read_bytes())
        version = index.get("version")
        if (
            index.get("schema_version") != 1
            or not isinstance(version, str)
            or not STABLE_VERSION.fullmatch(version)
        ):
            raise ValueError("Invalid Minishop source index")
        artifact = f"packages/minishop-blacklist-{version}.zip"
        if index.get("artifact") != artifact:
            raise ValueError("Index points to an unexpected artifact")
        names = ("minishop-plugin.json", artifact, "publisher.pub", "SHA256SUMS")
        files = {name: (directory / name).read_bytes() for name in names}
        digest = hashlib.sha256(files[artifact]).hexdigest()
        if (
            index.get("sha256") != digest
            or files["SHA256SUMS"] != f"{digest}  {artifact}\n".encode()
        ):
            raise ValueError("Published files do not match the package checksum")
        return cls(version, artifact, files)

    def matches(self, commit: str) -> bool:
        return all(git_file(commit, name) == body for name, body in self.files.items())


def check_immutable_files(package: Package, source: str) -> None:
    for name in (package.artifact, "publisher.pub"):
        already_published = git_file(source, name)
        if already_published is not None and already_published != package.files[name]:
            raise ValueError(f"Published file {name} would change")
    old_index = git_file(source, "minishop-plugin.json")
    if old_index is not None:
        latest = json.loads(old_index)["version"]
        if not isinstance(latest, str) or not STABLE_VERSION.fullmatch(latest):
            raise ValueError("Existing index does not have a stable version")
        if tuple(map(int, package.version.split("."))) < tuple(map(int, latest.split("."))):
            raise ValueError(f"Current repository source already points to version {latest}")


def commit_package(package: Package, main: str) -> None:
    git("checkout", "--detach", main)
    for name, data in package.files.items():
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    git("add", "--", *package.files)
    git(
        "-c",
        "user.name=github-actions[bot]",
        "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--only",
        "-m",
        f"build: publish Minishop Blacklist {package.version}",
        "--",
        *package.files,
    )


def publish_files(package: Package, source: str) -> None:
    for attempt in range(3):
        git("fetch", "--no-tags", "origin", MAIN)
        main = git("rev-parse", "FETCH_HEAD^{commit}")
        ancestor = run("git", "merge-base", "--is-ancestor", source, main, check=False)
        if ancestor.returncode == 1:
            raise ValueError("The tagged commit must be reachable from origin/main")
        ancestor.check_returncode()
        check_immutable_files(package, main)
        if package.matches(main):
            print("Package files already present on main.")
            return

        commit_package(package, main)
        push = run("git", "push", "origin", f"HEAD:{MAIN}", check=False)
        if push.returncode == 0:
            return
        if attempt == 2 or remote_ref(MAIN) == main:
            push.check_returncode()
        print("Main advanced during publication; retrying against its latest commit.")


def release_notes(version: str, repository: str) -> str:
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    heading = re.search(rf"^## {re.escape(version)}[ \t]*$", changelog, flags=re.MULTILINE)
    if heading is None:
        raise ValueError(f"Missing heading '## {version}' in CHANGELOG.md")
    excerpt = re.split(r"^## ", changelog[heading.end() :], maxsplit=1, flags=re.MULTILINE)[
        0
    ].strip()
    if not excerpt:
        raise ValueError(f"Release notes for {version} are empty")
    url = f"https://github.com/{repository}/blob/v{version}/CHANGELOG.md#{version.replace('.', '')}"
    return f"{excerpt}\n\n[Журнал изменений]({url})\n"


def verify_tag(ref: str, source: str) -> None:
    git("fetch", "--no-tags", "origin", ref)
    if git("rev-parse", "FETCH_HEAD^{commit}") != source:
        raise ValueError("The remote tag no longer points to the source commit")


def release_record(repository: str, tag: str) -> dict[str, object] | None:
    result = run(
        "gh",
        "api",
        "--paginate",
        f"repos/{repository}/releases?per_page=100",
        "--jq",
        f'.[] | select(.tag_name == "{tag}")',
    ).stdout.strip()
    return json.loads(result) if result else None


def asset_names(record: dict[str, object]) -> set[str]:
    assets = record.get("assets")
    if not isinstance(assets, list):
        raise ValueError("Expected release assets from GitHub")
    names = [asset["name"] for asset in assets]
    if len(set(names)) != len(names):
        raise ValueError("GitHub returned duplicate release asset names")
    return set(names)


def wait_for_release(repository: str, tag: str, assets: set[str]) -> dict[str, object]:
    for delay in (0, 1, 2, 4, 8):
        if delay:
            time.sleep(delay)
        record = release_record(repository, tag)
        if record is not None and assets <= asset_names(record):
            return record
    raise ValueError(f"Release {tag} is not yet visible with its assets; rerun the publish job")


def verify_assets(
    package: Package, record: dict[str, object], repository: str, tag: str
) -> list[str]:
    names = asset_names(record)
    if not names <= {Path(name).name for name in package.files}:
        raise ValueError(f"Unexpected assets on {tag}")
    missing = []
    for path, expected in package.files.items():
        name = Path(path).name
        if name not in names:
            missing.append(path)
            continue
        downloaded = run(
            "gh",
            "release",
            "download",
            tag,
            "--repo",
            repository,
            "--pattern",
            name,
            "--output",
            "-",
        ).stdout
        if downloaded != expected:
            raise ValueError(f"Published release asset {name} contains different bytes")
    return missing


def publish_release(package: Package, directory: Path, repository: str, notes: str) -> None:
    tag = f"v{package.version}"
    record = release_record(repository, tag)
    if record is None:
        with TemporaryDirectory(dir="/tmp") as temp:
            notes_file = Path(temp) / "notes.md"
            notes_file.write_text(notes, encoding="utf-8")
            run(
                "gh",
                "release",
                "create",
                tag,
                "--repo",
                repository,
                "--draft",
                "--verify-tag",
                "--title",
                f"Minishop Blacklist {package.version}",
                "--notes-file",
                str(notes_file),
            )
        record = wait_for_release(repository, tag, set())
    if record.get("prerelease") is not False or type(record.get("draft")) is not bool:
        raise ValueError(f"Unexpected release status for {tag}")
    missing = verify_assets(package, record, repository, tag)
    if record["draft"] is False:
        if missing:
            raise ValueError(f"Published release {tag} has missing assets")
        print(f"Verified existing release {tag}.")
        return
    if missing:
        run(
            "gh",
            "release",
            "upload",
            tag,
            "--repo",
            repository,
            *(str(directory / name) for name in missing),
        )
        record = wait_for_release(repository, tag, {Path(name).name for name in package.files})
        if record.get("draft") is not True or record.get("prerelease") is not False:
            raise ValueError(f"The release draft changed during upload: {tag}")
        if verify_assets(package, record, repository, tag):
            raise ValueError(f"Release draft {tag} still has missing assets")
    run("gh", "release", "edit", tag, "--repo", repository, "--draft=false", "--latest")
    print(f"Published release {tag}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir", type=Path, default=Path("dist/release"))
    args = parser.parse_args()
    package = Package.load(args.package_dir)
    repository = os.environ["GITHUB_REPOSITORY"]
    ref = os.environ["GITHUB_REF"]
    sha = os.environ["GITHUB_SHA"]
    if (
        os.environ["GITHUB_EVENT_NAME"] != "push"
        or ref != f"refs/tags/v{package.version}"
        or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository)
        or not re.fullmatch(r"[a-f0-9]{40}", sha)
    ):
        raise ValueError("Publication requires a push of the tag matching the package version")
    source = git("rev-parse", f"{sha}^{{commit}}")
    if git("rev-parse", "HEAD^{commit}") != source:
        raise ValueError("Checkout does not match the source commit")

    notes = release_notes(package.version, repository)
    verify_tag(ref, source)
    publish_files(package, source)
    verify_tag(ref, source)
    publish_release(package, args.package_dir, repository, notes)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stderr.decode(errors="replace"), file=sys.stderr)
        sys.exit(error.returncode)
    except (KeyError, OSError, ValueError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
