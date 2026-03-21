#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import subprocess
import tarfile
from pathlib import Path

import tomllib

EXCLUDED_PATHS = {
    Path("PKGBUILD"),
    Path(".SRCINFO"),
}


def project_version(repo_root: Path) -> str:
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["project"]["version"]


def tracked_files(repo_root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    return [
        repo_root / Path(entry.decode("utf-8"))
        for entry in result.stdout.split(b"\x00")
        if entry and Path(entry.decode("utf-8")) not in EXCLUDED_PATHS
    ]


def build_archive(repo_root: Path, output_path: Path, prefix: str) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sha256 = hashlib.sha256()

    with output_path.open("wb") as raw_file:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw_file,
            mtime=0,
        ) as gzip_file:
            with tarfile.open(fileobj=gzip_file, mode="w", format=tarfile.USTAR_FORMAT) as tar:
                for absolute_path in tracked_files(repo_root):
                    relative_path = absolute_path.relative_to(repo_root)
                    data = absolute_path.read_bytes()
                    info = tarfile.TarInfo(f"{prefix}/{relative_path.as_posix()}")
                    info.size = len(data)
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mode = 0o755 if absolute_path.stat().st_mode & 0o111 else 0o644
                    tar.addfile(info, io.BytesIO(data))

    with output_path.open("rb") as archive_file:
        for chunk in iter(lambda: archive_file.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo-root",
        default=Path(__file__).resolve().parents[1],
        type=Path,
    )
    parser.add_argument("--version")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    version = args.version or project_version(repo_root)
    output = args.output or repo_root / "dist" / f"spitter-{version}.tar.gz"
    checksum = build_archive(repo_root, output, f"spitter-{version}")
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
