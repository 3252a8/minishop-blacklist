"""Exercise tag publication against a temporary Git origin."""

import hashlib
import json
import runpy
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/publish-package.py"


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(directory: Path, message: str) -> None:
    _git(directory, "add", "--", "CHANGELOG.md")
    _git(
        directory,
        "-c",
        "user.name=Release Test",
        "-c",
        "user.email=release@example.test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        message,
    )


@pytest.fixture
def release_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Callable[[], None], Mock, Path, Path, str]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "remote", "add", "origin", str(origin))

    log = source / "CHANGELOG.md"
    log.write_text("## 1.0.1\n\nTagged release notes\n", encoding="utf-8")
    _commit(source, "Versioned sources")
    _git(
        source,
        "-c",
        "user.name=Release Test",
        "-c",
        "user.email=release@example.test",
        "tag",
        "-a",
        "v1.0.1",
        "-m",
        "Version 1.0.1",
    )
    tag = _git(source, "rev-parse", "refs/tags/v1.0.1")
    log.write_text("## 1.0.1\n\nLater main content\n", encoding="utf-8")
    _commit(source, "Advance main while the tagged workflow runs")
    _git(source, "push", "origin", "main", "refs/tags/v1.0.1")

    runner = tmp_path / "runner"
    _git(tmp_path, "clone", str(origin), str(runner))
    _git(runner, "checkout", "--detach", "refs/tags/v1.0.1")

    package_dir = tmp_path / "artifacts"
    archive = "packages/minishop-blacklist-1.0.1.zip"
    archive_path = package_dir / archive
    archive_path.parent.mkdir(parents=True)
    archive_path.write_bytes(b"signed package fixture")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (package_dir / "minishop-plugin.json").write_text(
        json.dumps(
            {"schema_version": 1, "artifact": archive, "version": "1.0.1", "sha256": digest}
        ),
        encoding="utf-8",
    )
    (package_dir / "publisher.pub").write_bytes(b"publisher fixture\n")
    (package_dir / "SHA256SUMS").write_text(f"{digest}  {archive}\n", encoding="utf-8")

    main = runpy.run_path(str(_SCRIPT))["main"]
    publish_release = Mock()
    monkeypatch.setitem(main.__globals__, "publish_release", publish_release)
    monkeypatch.chdir(runner)
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--package-dir", str(package_dir)])
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/minishop-blacklist")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v1.0.1")
    monkeypatch.setenv("GITHUB_SHA", tag)
    return main, publish_release, origin, runner, tag


def test_tagged_release_preserves_tag_and_advancing_main(release_run) -> None:
    main, publish_release, origin, runner, tag = release_run
    base = _git(origin, "rev-parse", "refs/heads/main")
    main()

    published = _git(origin, "rev-parse", "refs/heads/main")
    assert _git(origin, "rev-parse", "refs/heads/main^1") == base
    assert _git(origin, "rev-parse", "refs/tags/v1.0.1") == tag
    assert _git(origin, "show", "refs/heads/main:CHANGELOG.md").endswith("Later main content")
    files = set(
        _git(origin, "diff-tree", "--no-commit-id", "--name-only", "-r", published).splitlines()
    )
    assert files == {
        "minishop-plugin.json",
        "packages/minishop-blacklist-1.0.1.zip",
        "publisher.pub",
        "SHA256SUMS",
    }
    publish_release.assert_called_once()
    notes = publish_release.call_args.args[-1]
    assert "Tagged release notes" in notes
    assert "Later main content" not in notes

    _git(runner, "checkout", "--detach", "refs/tags/v1.0.1")
    main()
    assert _git(origin, "rev-parse", "refs/heads/main") == published
    assert _git(origin, "rev-parse", "refs/tags/v1.0.1") == tag
    assert publish_release.call_count == 2


def test_publish_retries_if_main_advances_during_push(
    release_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, publish_release, origin, runner, tag = release_run
    original_run = main.__globals__["run"]
    concurrent_head = None

    def advance_main(*command: str, check: bool = True):
        nonlocal concurrent_head
        if concurrent_head is None and command == ("git", "push", "origin", "HEAD:refs/heads/main"):
            repo = runner.parent / "source"
            (repo / "CHANGELOG.md").write_text("Concurrent update\n", encoding="utf-8")
            _commit(repo, "Advance main during the release push")
            _git(repo, "push", "origin", "main")
            concurrent_head = _git(repo, "rev-parse", "HEAD")
        return original_run(*command, check=check)

    monkeypatch.setitem(main.__globals__, "run", advance_main)
    main()
    assert _git(origin, "rev-parse", "refs/heads/main^1") == concurrent_head
    assert _git(origin, "show", "refs/heads/main:CHANGELOG.md") == "Concurrent update"
    assert _git(origin, "rev-parse", "refs/tags/v1.0.1") == tag
    publish_release.assert_called_once()


@pytest.mark.parametrize(
    ("event", "ref"),
    [
        ("push", "refs/heads/main"),
        ("push", "refs/tags/v1.0.2"),
        ("workflow_dispatch", "refs/tags/v1.0.1"),
    ],
)
def test_publisher_rejects_events_without_matching_tag(
    release_run, monkeypatch: pytest.MonkeyPatch, event: str, ref: str
) -> None:
    main, publish_release, origin, _, _ = release_run
    before = _git(origin, "rev-parse", "refs/heads/main")
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_REF", ref)
    with pytest.raises(ValueError, match="tag matching the package version"):
        main()
    publish_release.assert_not_called()
    assert _git(origin, "rev-parse", "refs/heads/main") == before
