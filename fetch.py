#!/usr/bin/env python3
"""Dynamically resolves latest upstream stable versions and downloads all source archives."""

import json
import os
import re
import shutil
import tarfile
import urllib.request
from pathlib import Path

from config import (
    BUILD_DIR,
    DOWNLOADS_DIR,
    FALLBACK_DEPS,
    FALLBACK_VERSIONS,
    GITHUB_REPOS,
    RESOLVED_VERSIONS_FILE,
    SRC_DIR,
)


def http_get_text(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mobile-Harness-Runtime-Builder"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def github_api_get(url: str):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Mobile-Harness-Runtime-Builder",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_ver_tuple(ver_str: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", ver_str))


def resolve_latest_cpython_314() -> str:
    """Finds the latest stable 3.14.x release tag from python/cpython."""
    try:
        tags = github_api_get(
            f"https://api.github.com/repos/{GITHUB_REPOS['cpython']}/tags?per_page=100"
        )
        stable_314 = []
        for item in tags:
            name = item.get("name", "")
            m = re.match(r"^v(3\.14\.(\d+))$", name)
            if m:
                stable_314.append((int(m.group(2)), m.group(1)))
        if stable_314:
            stable_314.sort()
            return stable_314[-1][1]
    except Exception as exc:
        print(f"[warn] Could not query cpython tags ({exc}), using fallback")
    return FALLBACK_VERSIONS["cpython"]


def resolve_latest_proot_distro() -> str:
    """Finds the latest stable v5.x release tag from termux/proot-distro."""
    try:
        rel = github_api_get(
            f"https://api.github.com/repos/{GITHUB_REPOS['proot_distro']}/releases/latest"
        )
        tag = rel.get("tag_name", "")
        if tag.startswith("v5."):
            return tag
    except Exception as exc:
        print(f"[warn] Could not query proot-distro release ({exc}), using fallback")
    return FALLBACK_VERSIONS["proot_distro"]


def resolve_branch_head_sha(repo: str, branch: str = "master") -> str:
    try:
        data = github_api_get(f"https://api.github.com/repos/{repo}/commits/{branch}")
        return data.get("sha", branch)[:12]
    except Exception as exc:
        print(f"[warn] Could not query {repo} HEAD ({exc}), using fallback")
        return branch


def resolve_github_release_tarball(
    name: str, repo: str, asset_prefix: str
) -> dict[str, str]:
    """Resolves the latest GitHub release and prefers uploaded .tar.gz release assets."""
    try:
        rel = github_api_get(f"https://api.github.com/repos/{repo}/releases/latest")
        tag = rel.get("tag_name", "")
        ver = re.sub(r"^(?:v|openssl-|zstd-|xz-|zlib-|libffi-)", "", tag)
        for asset in rel.get("assets", []):
            aname = asset.get("name", "")
            if (
                aname.startswith(asset_prefix)
                and aname.endswith(".tar.gz")
                and "doc" not in aname
            ):
                return {"version": ver, "url": asset["browser_download_url"]}
    except Exception as exc:
        print(
            f"[warn] Could not resolve latest GitHub release for {name} ({exc}), using fallback"
        )
    return FALLBACK_DEPS[name]


def resolve_latest_openssl_3x() -> dict[str, str]:
    """Resolves the latest stable OpenSSL 3.x release (CPython 3.14 requires OpenSSL 3.x symbols)."""
    try:
        releases = github_api_get(
            f"https://api.github.com/repos/{GITHUB_REPOS['openssl']}/releases?per_page=30"
        )
        candidates = []
        for rel in releases:
            if rel.get("draft") or rel.get("prerelease"):
                continue
            tag = rel.get("tag_name", "")
            m = re.match(r"^openssl-(3\.\d+\.\d+)$", tag)
            if not m:
                continue
            ver = m.group(1)
            for asset in rel.get("assets", []):
                aname = asset.get("name", "")
                if aname == f"openssl-{ver}.tar.gz":
                    candidates.append(
                        (parse_ver_tuple(ver), ver, asset["browser_download_url"])
                    )
        if candidates:
            candidates.sort()
            _, ver, url = candidates[-1]
            return {"version": ver, "url": url}
    except Exception as exc:
        print(f"[warn] Could not resolve OpenSSL 3.x release ({exc}), using fallback")
    return FALLBACK_DEPS["openssl"]


def resolve_latest_sqlite() -> dict[str, str]:
    """Parses sqlite.org's machine-readable download table for the latest sqlite-autoconf tarball."""
    try:
        html = http_get_text("https://www.sqlite.org/download.html")
        m = re.search(
            r"PRODUCT,(\d+\.\d+\.\d+),(\d{4}/sqlite-autoconf-\d+\.tar\.gz)", html
        )
        if m:
            return {
                "version": m.group(1),
                "url": f"https://www.sqlite.org/{m.group(2)}",
            }
    except Exception as exc:
        print(f"[warn] Could not resolve latest sqlite ({exc}), using fallback")
    return FALLBACK_DEPS["sqlite"]


def resolve_latest_talloc() -> dict[str, str]:
    """Parses samba.org's talloc index for the newest talloc-2.x.y.tar.gz."""
    try:
        html = http_get_text("https://www.samba.org/ftp/talloc/")
        matches = re.findall(r'href="(talloc-(\d+\.\d+\.\d+)\.tar\.gz)"', html)
        if matches:
            matches.sort(key=lambda item: parse_ver_tuple(item[1]))
            fname, ver = matches[-1]
            return {"version": ver, "url": f"https://www.samba.org/ftp/talloc/{fname}"}
    except Exception as exc:
        print(f"[warn] Could not resolve latest talloc ({exc}), using fallback")
    return FALLBACK_DEPS["talloc"]


def resolve_latest_bzip2() -> dict[str, str]:
    """Parses sourceware.org's bzip2 index for the newest bzip2-1.x.y.tar.gz."""
    try:
        html = http_get_text("https://sourceware.org/pub/bzip2/")
        matches = re.findall(r'href="(bzip2-(\d+\.\d+\.\d+)\.tar\.gz)"', html)
        if matches:
            matches.sort(key=lambda item: parse_ver_tuple(item[1]))
            fname, ver = matches[-1]
            return {"version": ver, "url": f"https://sourceware.org/pub/bzip2/{fname}"}
    except Exception as exc:
        print(f"[warn] Could not resolve latest bzip2 ({exc}), using fallback")
    return FALLBACK_DEPS["bzip2"]


def resolve_all_static_deps() -> dict[str, dict[str, str]]:
    return {
        "zlib": resolve_github_release_tarball("zlib", GITHUB_REPOS["zlib"], "zlib-"),
        "xz": resolve_github_release_tarball("xz", GITHUB_REPOS["xz"], "xz-"),
        "zstd": resolve_github_release_tarball("zstd", GITHUB_REPOS["zstd"], "zstd-"),
        "libffi": resolve_github_release_tarball(
            "libffi", GITHUB_REPOS["libffi"], "libffi-"
        ),
        "openssl": resolve_latest_openssl_3x(),
        "sqlite": resolve_latest_sqlite(),
        "talloc": resolve_latest_talloc(),
        "bzip2": resolve_latest_bzip2(),
        "cacert": FALLBACK_DEPS["cacert"],
    }


def download_file(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        print(f"[cached] {dest.name}")
        return dest
    print(f"[download] {url} -> {dest.name}")
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mobile-Harness-Runtime-Builder"}
    )
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out)
    tmp.replace(dest)
    return dest


def extract_tar(archive: Path, dest_dir: Path) -> None:
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    print(f"[extract] {archive.name} -> {dest_dir.relative_to(BUILD_DIR)}")
    with tarfile.open(archive, "r:*") as tar:
        members = tar.getmembers()
        top_dirs = {
            m.name.split("/")[0]
            for m in members
            if m.name and not m.name.startswith("/")
        }
        strip_prefix = f"{next(iter(top_dirs))}/" if len(top_dirs) == 1 else ""
        for member in members:
            if strip_prefix and member.name.startswith(strip_prefix):
                member.name = member.name[len(strip_prefix):]
            if (
                not member.name
                or member.name.startswith("/")
                or ".." in member.name.split("/")
            ):
                continue
            tar.extract(member, dest_dir, filter="fully_trusted")


def main() -> None:
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    SRC_DIR.mkdir(parents=True, exist_ok=True)

    py_ver = resolve_latest_cpython_314()
    pd_tag = resolve_latest_proot_distro()
    proot_sha = resolve_branch_head_sha(GITHUB_REPOS["proot"], "master")
    shmem_sha = resolve_branch_head_sha(GITHUB_REPOS["android_shmem"], "master")
    deps = resolve_all_static_deps()

    resolved = {
        "python": py_ver,
        "proot_distro": pd_tag.lstrip("v"),
        "proot_distro_tag": pd_tag,
        "proot": f"5.1.107-{proot_sha}",
        "proot_ref": proot_sha,
        "android_shmem_ref": shmem_sha,
        "deps": {k: v["version"] for k, v in deps.items()},
    }
    print(f"[resolved] {json.dumps(resolved, indent=2)}")

    # 1. Fetch CPython 3.14.x
    py_archive = download_file(
        f"https://www.python.org/ftp/python/{py_ver}/Python-{py_ver}.tar.xz",
        DOWNLOADS_DIR / f"Python-{py_ver}.tar.xz",
    )
    extract_tar(py_archive, SRC_DIR / "cpython")

    # 2. Fetch termux/proot-distro v5.x
    pd_archive = download_file(
        f"https://github.com/{GITHUB_REPOS['proot_distro']}/archive/refs/tags/{pd_tag}.tar.gz",
        DOWNLOADS_DIR / f"proot-distro-{pd_tag}.tar.gz",
    )
    extract_tar(pd_archive, SRC_DIR / "proot-distro")

    # 3. Fetch termux/proot & termux/libandroid-shmem
    proot_archive = download_file(
        f"https://github.com/{GITHUB_REPOS['proot']}/archive/{proot_sha}.tar.gz",
        DOWNLOADS_DIR / f"proot-{proot_sha}.tar.gz",
    )
    extract_tar(proot_archive, SRC_DIR / "proot")

    shmem_archive = download_file(
        f"https://github.com/{GITHUB_REPOS['android_shmem']}/archive/{shmem_sha}.tar.gz",
        DOWNLOADS_DIR / f"libandroid-shmem-{shmem_sha}.tar.gz",
    )
    extract_tar(shmem_archive, SRC_DIR / "libandroid-shmem")

    # 4. Fetch dynamically resolved static C library dependencies
    for name, info in deps.items():
        url = info["url"]
        filename = url.split("/")[-1]
        downloaded = download_file(url, DOWNLOADS_DIR / filename)
        if filename.endswith((".tar.gz", ".tar.xz", ".tgz")):
            extract_tar(downloaded, SRC_DIR / name)

    RESOLVED_VERSIONS_FILE.write_text(
        json.dumps(resolved, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[ok] Wrote {RESOLVED_VERSIONS_FILE}")


if __name__ == "__main__":
    main()
