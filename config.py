#!/usr/bin/env python3
"""Shared configuration and constants for Mobile-Harness-Runtime."""

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
BUILD_DIR = ROOT_DIR / "build"
DOWNLOADS_DIR = BUILD_DIR / "downloads"
SRC_DIR = BUILD_DIR / "src"
DEPS_PREFIX = BUILD_DIR / "deps-prefix"
STAGE_DIR = BUILD_DIR / "stage"
USR_DIR = STAGE_DIR / "usr"
DIST_DIR = ROOT_DIR / "dist"

RESOLVED_VERSIONS_FILE = BUILD_DIR / "resolved_versions.json"
ARCHIVE_NAME = "mh-host-runtime-arm64.tar.zst"
MANIFEST_NAME = "mhr-manifest.json"

# Android NDK target settings (arm64-v8a, API 28 for SELinux exec compatibility, 16KB page alignment)
ANDROID_ABI = "arm64-v8a"
ANDROID_API = 28
TARGET_TRIPLE = "aarch64-linux-android"
COMPILER_TRIPLE = f"{TARGET_TRIPLE}{ANDROID_API}"
PAGE_SIZE_LDFLAGS = "-Wl,-z,max-page-size=16384"

# Upstream GitHub repositories tracked dynamically for latest stable releases
GITHUB_REPOS = {
    "cpython": "python/cpython",
    "proot_distro": "termux/proot-distro",
    "proot": "termux/proot",
    "android_shmem": "termux/libandroid-shmem",
    "zlib": "madler/zlib",
    "xz": "tukaani-project/xz",
    "zstd": "facebook/zstd",
    "libffi": "libffi/libffi",
    "openssl": "openssl/openssl",
}

# Fallback versions & URLs used if an upstream API rate-limits or is unreachable
FALLBACK_VERSIONS = {
    "cpython": "3.14.0",
    "proot_distro": "v5.10.3",
    "proot": "master",
    "android_shmem": "master",
}

FALLBACK_DEPS = {
    "zlib": {
        "version": "1.3.1",
        "url": "https://github.com/madler/zlib/releases/download/v1.3.1/zlib-1.3.1.tar.gz",
    },
    "xz": {
        "version": "5.6.4",
        "url": "https://github.com/tukaani-project/xz/releases/download/v5.6.4/xz-5.6.4.tar.gz",
    },
    "bzip2": {
        "version": "1.0.8",
        "url": "https://sourceware.org/pub/bzip2/bzip2-1.0.8.tar.gz",
    },
    "zstd": {
        "version": "1.5.7",
        "url": "https://github.com/facebook/zstd/releases/download/v1.5.7/zstd-1.5.7.tar.gz",
    },
    "libffi": {
        "version": "3.4.7",
        "url": "https://github.com/libffi/libffi/releases/download/v3.4.7/libffi-3.4.7.tar.gz",
    },
    "openssl": {
        "version": "3.4.1",
        "url": "https://github.com/openssl/openssl/releases/download/openssl-3.4.1/openssl-3.4.1.tar.gz",
    },
    "sqlite": {
        "version": "3.49.1",
        "url": "https://www.sqlite.org/2025/sqlite-autoconf-3490100.tar.gz",
    },
    "talloc": {
        "version": "2.4.3",
        "url": "https://www.samba.org/ftp/talloc/talloc-2.4.3.tar.gz",
    },
    "cacert": {
        "version": "latest",
        "url": "https://curl.se/ca/cacert.pem",
    },
}

GITHUB_REPOSITORY = os.environ.get(
    "GITHUB_REPOSITORY", "miscdevstuff/Mobile-Harness-Runtime"
)
RELEASE_TAG = os.environ.get("RELEASE_TAG", "latest")
