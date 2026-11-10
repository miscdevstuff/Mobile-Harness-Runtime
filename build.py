#!/usr/bin/env python3
"""Cross-compiles proot, Python 3.14, and proot-distro into mh-host-runtime-arm64.tar.zst."""

import hashlib
import json
import multiprocessing
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from config import (
    ARCHIVE_NAME,
    BUILD_DIR,
    COMPILER_TRIPLE,
    DEPS_PREFIX,
    DIST_DIR,
    DOWNLOADS_DIR,
    GITHUB_REPOSITORY,
    MANIFEST_NAME,
    PAGE_SIZE_LDFLAGS,
    RESOLVED_VERSIONS_FILE,
    ROOT_DIR,
    SRC_DIR,
    STAGE_DIR,
    TARGET_TRIPLE,
    USR_DIR,
)

JOBS = str(max(2, multiprocessing.cpu_count()))


def log(msg: str) -> None:
    print(msg, flush=True)


def find_ndk_toolchain() -> Path:
    ndk_home = (
        os.environ.get("ANDROID_NDK_HOME")
        or os.environ.get("ANDROID_NDK_ROOT")
        or os.environ.get("ANDROID_NDK")
    )
    if not ndk_home:
        sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        if sdk and (Path(sdk) / "ndk").is_dir():
            versions = sorted((Path(sdk) / "ndk").iterdir())
            if versions:
                ndk_home = str(versions[-1])
    assert ndk_home and Path(ndk_home).is_dir(), (
        "ANDROID_NDK_HOME is not set or missing"
    )
    toolchain = Path(ndk_home) / "toolchains/llvm/prebuilt/linux-x86_64"
    assert toolchain.is_dir(), f"NDK toolchain not found at {toolchain}"
    return toolchain


def make_ndk_env(toolchain: Path) -> dict[str, str]:
    bin_dir = toolchain / "bin"
    sysroot = toolchain / "sysroot"
    cc = str(bin_dir / f"{COMPILER_TRIPLE}-clang")
    cxx = str(bin_dir / f"{COMPILER_TRIPLE}-clang++")
    ar = str(bin_dir / "llvm-ar")
    ranlib = str(bin_dir / "llvm-ranlib")
    strip = str(bin_dir / "llvm-strip")
    readelf = str(bin_dir / "llvm-readelf")

    cflags = f"-O2 -fPIC -ffunction-sections -fdata-sections -I{DEPS_PREFIX}/include"
    ldflags = f"-L{DEPS_PREFIX}/lib -Wl,--gc-sections {PAGE_SIZE_LDFLAGS}"

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            "CC": cc,
            "CXX": cxx,
            "AR": ar,
            "RANLIB": ranlib,
            "STRIP": strip,
            "READELF": readelf,
            "CFLAGS": cflags,
            "CXXFLAGS": cflags,
            "CPPFLAGS": f"-I{DEPS_PREFIX}/include",
            "LDFLAGS": ldflags,
            "PKG_CONFIG_PATH": f"{DEPS_PREFIX}/lib/pkgconfig",
            "PKG_CONFIG_LIBDIR": f"{DEPS_PREFIX}/lib/pkgconfig",
            "SYSROOT": str(sysroot),
        }
    )
    return env


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    log(f"[cmd] ({cwd.name}) {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(cwd), env=env, check=True)


def write_pkgconfig(name: str, desc: str, ver: str, libs: str) -> None:
    pc_dir = DEPS_PREFIX / "lib/pkgconfig"
    pc_dir.mkdir(parents=True, exist_ok=True)
    (pc_dir / f"{name}.pc").write_text(
        f"prefix={DEPS_PREFIX}\n"
        "exec_prefix=${prefix}\n"
        "libdir=${exec_prefix}/lib\n"
        "includedir=${prefix}/include\n\n"
        f"Name: {name}\n"
        f"Description: {desc}\n"
        f"Version: {ver}\n"
        f"Libs: -L${{libdir}} {libs}\n"
        "Cflags: -I${includedir}\n",
        encoding="utf-8",
    )


def build_static_deps(env: dict[str, str]) -> None:
    (DEPS_PREFIX / "include").mkdir(parents=True, exist_ok=True)
    (DEPS_PREFIX / "lib/pkgconfig").mkdir(parents=True, exist_ok=True)

    # 1. zlib
    zlib_src = SRC_DIR / "zlib"
    zlib_env = env.copy()
    zlib_env["CHOST"] = TARGET_TRIPLE
    run(
        ["./configure", f"--prefix={DEPS_PREFIX}", "--static"],
        cwd=zlib_src,
        env=zlib_env,
    )
    run(["make", f"-j{JOBS}"], cwd=zlib_src, env=zlib_env)
    run(["make", "install"], cwd=zlib_src, env=zlib_env)

    # 2. xz (liblzma)
    xz_src = SRC_DIR / "xz"
    run(
        [
            "./configure",
            f"--host={TARGET_TRIPLE}",
            f"--prefix={DEPS_PREFIX}",
            "--enable-static",
            "--disable-shared",
            "--disable-xz",
            "--disable-xzdec",
            "--disable-lzmadec",
            "--disable-lzmainfo",
            "--disable-scripts",
            "--disable-doc",
        ],
        cwd=xz_src,
        env=env,
    )
    run(["make", f"-j{JOBS}"], cwd=xz_src, env=env)
    run(["make", "install"], cwd=xz_src, env=env)

    # 3. bzip2
    bz2_src = SRC_DIR / "bzip2"
    run(
        [
            "make",
            f"-j{JOBS}",
            "libbz2.a",
            f"CC={env['CC']}",
            f"AR={env['AR']}",
            f"RANLIB={env['RANLIB']}",
            "CFLAGS=-O2 -fPIC -ffunction-sections -fdata-sections -D_FILE_OFFSET_BITS=64",
        ],
        cwd=bz2_src,
        env=env,
    )
    shutil.copy2(bz2_src / "bzlib.h", DEPS_PREFIX / "include/bzlib.h")
    shutil.copy2(bz2_src / "libbz2.a", DEPS_PREFIX / "lib/libbz2.a")
    write_pkgconfig("bzip2", "bzip2 compression library", "1.0.8", "-lbz2")

    # 4. zstd (enables Python 3.14 native compression.zstd & tarfile .tar.zst support)
    zstd_src = SRC_DIR / "zstd"
    run(
        [
            "make",
            "-C",
            "lib",
            f"-j{JOBS}",
            "libzstd.a",
            "libzstd.pc",
            f"CC={env['CC']}",
            f"AR={env['AR']}",
            "CFLAGS=-O3 -fPIC -ffunction-sections -fdata-sections",
            f"PREFIX={DEPS_PREFIX}",
        ],
        cwd=zstd_src,
        env=env,
    )
    run(
        [
            "make",
            "-C",
            "lib",
            "install-static",
            "install-includes",
            "install-pc",
            f"PREFIX={DEPS_PREFIX}",
        ],
        cwd=zstd_src,
        env=env,
    )

    # 5. libffi
    ffi_src = SRC_DIR / "libffi"
    run(
        [
            "./configure",
            f"--host={TARGET_TRIPLE}",
            f"--prefix={DEPS_PREFIX}",
            "--enable-static",
            "--disable-shared",
            "--disable-docs",
        ],
        cwd=ffi_src,
        env=env,
    )
    run(["make", f"-j{JOBS}"], cwd=ffi_src, env=env)
    run(["make", "install"], cwd=ffi_src, env=env)

    # 6. sqlite3 (direct amalgamation build)
    sqlite_src = SRC_DIR / "sqlite"
    run(
        [
            env["CC"],
            "-O2",
            "-fPIC",
            "-ffunction-sections",
            "-fdata-sections",
            "-DSQLITE_ENABLE_FTS5",
            "-DSQLITE_ENABLE_RTREE",
            "-DSQLITE_ENABLE_JSON1",
            "-DSQLITE_ENABLE_MATH_FUNCTIONS",
            "-DSQLITE_THREADSAFE=1",
            "-c",
            str(sqlite_src / "sqlite3.c"),
            "-o",
            str(sqlite_src / "sqlite3.o"),
        ],
        cwd=sqlite_src,
        env=env,
    )
    run(
        [
            env["AR"],
            "rcs",
            str(DEPS_PREFIX / "lib/libsqlite3.a"),
            str(sqlite_src / "sqlite3.o"),
        ],
        cwd=sqlite_src,
        env=env,
    )
    shutil.copy2(sqlite_src / "sqlite3.h", DEPS_PREFIX / "include/sqlite3.h")
    shutil.copy2(sqlite_src / "sqlite3ext.h", DEPS_PREFIX / "include/sqlite3ext.h")
    write_pkgconfig("sqlite3", "SQLite database engine", "3.49.1", "-lsqlite3 -lm")

    # 7. OpenSSL 3.x (static libcrypto.a & libssl.a with -ffunction-sections -fdata-sections)
    ssl_src = SRC_DIR / "openssl"
    run(
        [
            "./Configure",
            "linux-aarch64",
            f"--prefix={DEPS_PREFIX}",
            f"--openssldir={DEPS_PREFIX}/ssl",
            "no-shared",
            "no-async",
            "no-tests",
            "no-ui-console",
            "no-docs",
            "no-legacy",
            "-fPIC",
            "-ffunction-sections",
            "-fdata-sections",
        ],
        cwd=ssl_src,
        env=env,
    )
    run(["make", f"-j{JOBS}", "build_sw"], cwd=ssl_src, env=env)
    run(["make", "install_sw"], cwd=ssl_src, env=env)
    if (DEPS_PREFIX / "lib64").is_dir():
        for item in (DEPS_PREFIX / "lib64").iterdir():
            if item.is_file():
                shutil.copy2(item, DEPS_PREFIX / "lib" / item.name)
            elif item.is_dir() and item.name == "pkgconfig":
                for pc in item.iterdir():
                    txt = pc.read_text(encoding="utf-8").replace("/lib64", "/lib")
                    (DEPS_PREFIX / "lib/pkgconfig" / pc.name).write_text(
                        txt, encoding="utf-8"
                    )


def patch_proot_sources(proot_src: Path) -> None:
    """Applies /proc/self/exe relative path fallbacks to proot for loader and temp dir."""
    rel_tmp_helper = r"""
/* MH_REL_TMP_PATCH */
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <limits.h>
#include <sys/stat.h>
static const char *mh_default_tmp_dir(void) {
    static char buf[PATH_MAX];
    const char *env_tmp = getenv("PROOT_TMP_DIR");
    if (env_tmp && env_tmp[0] != '\0') return env_tmp;
    ssize_t len = readlink("/proc/self/exe", buf, sizeof(buf) - 1);
    if (len > 0) {
        buf[len] = '\0';
        char *slash = strrchr(buf, '/');
        if (slash) {
            *slash = '\0';
            slash = strrchr(buf, '/');
            if (slash && strlen(buf) + 5 < sizeof(buf)) {
                strcpy(slash + 1, "tmp");
                mkdir(buf, 0777);
                return buf;
            }
        }
    }
    return "/tmp";
}
"""
    for rel_path in ["src/path/temp.c", "src/path/f2fs-bug.c"]:
        target_c = proot_src / rel_path
        if target_c.is_file():
            txt = target_c.read_text(encoding="utf-8")
            if "MH_REL_TMP_PATCH" not in txt:
                txt = (
                    rel_tmp_helper
                    + "\n"
                    + txt.replace("P_tmpdir", "mh_default_tmp_dir()").replace(
                        '"/tmp"', "mh_default_tmp_dir()"
                    )
                )
                target_c.write_text(txt, encoding="utf-8")

    loader_helper = r"""
/* MH_REL_LOADER_PATCH */
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <limits.h>
static const char *mh_resolve_bundled_loader(void) {
    static char buf[PATH_MAX];
    const char *env_loader = getenv("PROOT_LOADER");
    if (env_loader && env_loader[0] != '\0') return env_loader;
    ssize_t len = readlink("/proc/self/exe", buf, sizeof(buf) - 1);
    if (len > 0) {
        buf[len] = '\0';
        char *slash = strrchr(buf, '/');
        if (slash) {
            *slash = '\0';
            slash = strrchr(buf, '/');
            if (slash && strlen(buf) + 24 < sizeof(buf)) {
                strcpy(slash + 1, "libexec/proot/loader");
                if (access(buf, X_OK) == 0) return buf;
            }
        }
    }
    return NULL;
}
"""
    for exec_c in (proot_src / "src/execve").glob("*.c"):
        txt = exec_c.read_text(encoding="utf-8")
        if "MH_REL_LOADER_PATCH" not in txt and 'getenv("PROOT_LOADER")' in txt:
            txt = (
                loader_helper
                + "\n"
                + txt.replace('getenv("PROOT_LOADER")', "mh_resolve_bundled_loader()")
            )
            exec_c.write_text(txt, encoding="utf-8")


def parse_talloc_version(talloc_src: Path, resolved: dict) -> tuple[int, int, int]:
    talloc_h = (talloc_src / "talloc.h").read_text(encoding="utf-8")
    m_maj = re.search(r"#define\s+TALLOC_VERSION_MAJOR\s+(\d+)", talloc_h)
    m_min = re.search(r"#define\s+TALLOC_VERSION_MINOR\s+(\d+)", talloc_h)
    ver_str = resolved.get("deps", {}).get("talloc", "2.4.3")
    parts = [int(x) for x in re.findall(r"\d+", ver_str)]
    major = int(m_maj.group(1)) if m_maj else (parts[0] if len(parts) > 0 else 2)
    minor = int(m_min.group(1)) if m_min else (parts[1] if len(parts) > 1 else 5)
    release = parts[2] if len(parts) > 2 else 0
    return major, minor, release


def build_proot(env: dict[str, str], resolved: dict) -> None:
    """Builds static-linked proot + 16KB-aligned prootloader."""
    proot_src = SRC_DIR / "proot"
    shmem_src = SRC_DIR / "libandroid-shmem"
    talloc_src = SRC_DIR / "talloc"
    patch_proot_sources(proot_src)

    out_bin = USR_DIR / "bin"
    out_libexec = USR_DIR / "libexec/proot"
    out_bin.mkdir(parents=True, exist_ok=True)
    out_libexec.mkdir(parents=True, exist_ok=True)

    cc = env["CC"]
    ar = env["AR"]
    readelf = env["READELF"]
    strip = env["STRIP"]

    work = BUILD_DIR / "proot-build"
    work.mkdir(parents=True, exist_ok=True)

    # 1. Create standalone POSIX/C23 replace.h shim for talloc.c and compile libtalloc.a
    t_maj, t_min, t_rel = parse_talloc_version(talloc_src, resolved)
    (work / "replace.h").write_text(
        "#ifndef _REPLACE_H\n#define _REPLACE_H\n"
        "#include <stdlib.h>\n#include <stdio.h>\n#include <string.h>\n"
        "#include <stdarg.h>\n#include <stdint.h>\n#include <stdbool.h>\n"
        "#include <errno.h>\n#include <unistd.h>\n#include <sys/types.h>\n"
        "#include <limits.h>\n#include <dlfcn.h>\n"
        "#define HAVE_VA_COPY 1\n"
        "#define HAVE_CONSTRUCTOR_ATTRIBUTE 1\n"
        "#ifndef _PUBLIC_\n#define _PUBLIC_\n#endif\n"
        "#ifndef _PRINTF_ATTRIBUTE\n#define _PRINTF_ATTRIBUTE(a,b)\n#endif\n"
        "#ifndef MIN\n#define MIN(a,b) ((a)<(b)?(a):(b))\n#endif\n"
        "#ifndef MAX\n#define MAX(a,b) ((a)>(b)?(a):(b))\n#endif\n"
        "#ifndef discard_const_p\n#define discard_const_p(type, ptr) ((type *)((intptr_t)(ptr)))\n#endif\n"
        "static inline void *mh_memset_explicit(void *s, int c, size_t n) {\n"
        "    volatile unsigned char *p = (volatile unsigned char *)s;\n"
        "    while (n--) *p++ = (unsigned char)c;\n"
        "    return s;\n"
        "}\n"
        "#ifndef memset_explicit\n#define memset_explicit mh_memset_explicit\n#endif\n"
        "#endif\n",
        encoding="utf-8",
    )
    run(
        [
            cc,
            "-O2",
            "-fPIC",
            "-D_GNU_SOURCE",
            "-D__STDC_WANT_LIB_EXT1__=1",
            f"-DTALLOC_BUILD_VERSION_MAJOR={t_maj}",
            f"-DTALLOC_BUILD_VERSION_MINOR={t_min}",
            f"-DTALLOC_BUILD_VERSION_RELEASE={t_rel}",
            f"-I{work}",
            f"-I{talloc_src}",
            "-c",
            str(talloc_src / "talloc.c"),
            "-o",
            str(work / "talloc.o"),
        ],
        cwd=work,
        env=env,
    )
    run(
        [ar, "rcs", str(work / "libtalloc.a"), str(work / "talloc.o")],
        cwd=work,
        env=env,
    )

    # 2. Compile static libandroid-shmem.a
    shm_h = next(shmem_src.rglob("shm.h"))
    shm_inc_dirs = [f"-I{shm_h.parent}", f"-I{shm_h.parent.parent}", f"-I{shmem_src}"]
    c_sources = sorted(shmem_src.rglob("*.c"))
    shmem_objs = []
    for idx, c_file in enumerate(c_sources):
        obj = work / f"shmem_{idx}.o"
        run(
            [
                cc,
                "-O2",
                "-fPIC",
                *shm_inc_dirs,
                '-D_PATH_TMP="/tmp/"',
                "-c",
                str(c_file),
                "-o",
                str(obj),
            ],
            cwd=work,
            env=env,
        )
        shmem_objs.append(str(obj))
    run([ar, "rcs", str(work / "libandroid-shmem.a"), *shmem_objs], cwd=work, env=env)

    # 3. Build 16KB page-aligned static prootloader
    loader_bin = out_libexec / "loader"
    loader_arch_cflags = ["-fPIC", "-ffreestanding", "-O2", f"-I{proot_src / 'src'}"]
    run(
        [
            cc,
            *loader_arch_cflags,
            str(proot_src / "src/loader/loader.c"),
            str(proot_src / "src/loader/assembly.S"),
            "-static",
            "-nostdlib",
            "-Wl,--build-id=none,--rosegment,-Ttext=0x2000000000,-z,noexecstack,-u,pokedata_workaround",
            PAGE_SIZE_LDFLAGS,
            "-o",
            str(loader_bin),
        ],
        cwd=work,
        env=env,
    )

    # 4. Generate build.h and loader-info.c (extracting offset_to_pokedata_workaround before stripping loader)
    sym_out = subprocess.check_output(
        [readelf, "-s", "-W", str(loader_bin)], text=True, env=env
    )
    syms: dict[str, int] = {}
    for line in sym_out.splitlines():
        parts = line.split()
        if len(parts) >= 8 and parts[7] in ("pokedata_workaround", "_start"):
            syms[parts[7]] = int(parts[1], 16)
    assert "pokedata_workaround" in syms and "_start" in syms, (
        f"Failed to find pokedata_workaround / _start in loader symbol table:\n{sym_out}"
    )
    pokedata_offset = syms["pokedata_workaround"] - syms["_start"]
    log(f"[proot] Computed offset_to_pokedata_workaround = {pokedata_offset}")

    (work / "build.h").write_text(
        '#ifndef BUILD_H\n#define BUILD_H\n#define VERSION "5.1.107"\n'
        "#define HAVE_PROCESS_VM 1\n#define HAVE_SECCOMP_FILTER 1\n#endif\n",
        encoding="utf-8",
    )
    (work / "loader-info.c").write_text(
        "#include <sys/types.h>\n"
        f"const ssize_t offset_to_pokedata_workaround = {pokedata_offset};\n"
        "const unsigned char _binary_loader_exe_start[] = { 0 };\n"
        "const unsigned char _binary_loader_exe_end[] = { 0 };\n"
        "const unsigned char loader_exe[] = { 0 };\n"
        "const unsigned int loader_exe_len = 0;\n",
        encoding="utf-8",
    )

    # 5. Collect proot C sources
    gnumakefile = proot_src / "src/GNUmakefile"
    src_paths: list[str] = []
    if gnumakefile.is_file():
        mk_txt = gnumakefile.read_text(encoding="utf-8")
        for m in re.finditer(r"([a-zA-Z0-9_/-]+)\.o\b", mk_txt):
            rel_base = m.group(1)
            if "loader" in rel_base:
                continue
            candidate = proot_src / "src" / f"{rel_base}.c"
            if candidate.is_file() and str(candidate) not in src_paths:
                src_paths.append(str(candidate))

    if not src_paths:
        for c_path in sorted((proot_src / "src").rglob("*.c")):
            if "loader" not in c_path.parts:
                src_paths.append(str(c_path))

    src_paths.append(str(work / "loader-info.c"))

    proot_bin = out_bin / "proot"
    run(
        [
            cc,
            "-O2",
            "-D_FILE_OFFSET_BITS=64",
            "-D_GNU_SOURCE",
            "-DARG_MAX=131072",
            "-DWITH_LIBANDROID_SHMEM",
            '-DPROOT_UNBUNDLE_LOADER="."',
            f"-I{proot_src / 'src'}",
            f"-I{work}",
            f"-I{talloc_src}",
            *shm_inc_dirs,
            "-include",
            "string.h",
            "-include",
            str(shm_h),
            *src_paths,
            str(work / "libtalloc.a"),
            str(work / "libandroid-shmem.a"),
            "-llog",
            "-landroid",
            "-Wl,-z,noexecstack",
            PAGE_SIZE_LDFLAGS,
            "-o",
            str(proot_bin),
        ],
        cwd=work,
        env=env,
    )

    run([strip, str(proot_bin), str(loader_bin)], cwd=work, env=env)
    run([readelf, "-d", str(proot_bin)], cwd=work, env=env)


def build_python314(env: dict[str, str]) -> None:
    """Cross-compiles Python 3.14.x with static OpenSSL 3.x/Zstd/LZMA/Bzip2/FFI/SQLite."""
    py_src = SRC_DIR / "cpython"
    build_py = shutil.which("python3.14") or shutil.which("python3")
    assert build_py, "Host python3.14 is required for cross-compiling CPython 3.14"

    config_site = py_src / "config.site-android"
    config_site.write_text(
        "ac_cv_file__dev_ptmx=yes\n"
        "ac_cv_file__dev_ptc=no\n"
        "ac_cv_buggy_getaddrinfo=no\n"
        "ac_cv_func_getaddrinfo=yes\n"
        "ac_cv_func_wcsftime=no\n"
        "ac_cv_func_gethostbyaddr_r=no\n"
        "ac_cv_func_gethostbyname_r=no\n",
        encoding="utf-8",
    )

    pkg_config_static = BUILD_DIR / "pkg-config-static"
    pkg_config_static.write_text(
        '#!/bin/sh\nexec /usr/bin/pkg-config --static "$@"\n',
        encoding="utf-8",
    )
    pkg_config_static.chmod(0o755)

    py_env = env.copy()
    py_env["CONFIG_SITE"] = str(config_site)
    py_env["PKG_CONFIG"] = str(pkg_config_static)
    py_env["LDFLAGS"] = f"-L{DEPS_PREFIX}/lib -Wl,--gc-sections {PAGE_SIZE_LDFLAGS}"

    run(
        [
            "./configure",
            f"--host={TARGET_TRIPLE}",
            "--build=x86_64-pc-linux-gnu",
            f"--with-build-python={build_py}",
            f"--prefix={USR_DIR}",
            "--enable-shared",
            "--enable-ipv6",
            f"--with-openssl={DEPS_PREFIX}",
            "--without-ensurepip",
            "--disable-test-modules",
        ],
        cwd=py_src,
        env=py_env,
    )

    run(["make", f"-j{JOBS}"], cwd=py_src, env=py_env)
    run(["make", "install"], cwd=py_src, env=py_env)

    py3_link = USR_DIR / "bin/python3"
    if not py3_link.exists():
        py3_link.symlink_to("python3.14")

    # Remove unneeded build-time headers, manpages, pkgconfig, and scripts with hardcoded build-host shebangs
    for rel_remove in ["include", "share", "lib/pkgconfig"]:
        target = USR_DIR / rel_remove
        if target.exists():
            shutil.rmtree(target)

    for script_name in [
        "idle3",
        "idle3.14",
        "pydoc3",
        "pydoc3.14",
        "python3-config",
        "python3.14-config",
    ]:
        p = USR_DIR / "bin" / script_name
        if p.exists() or p.is_symlink():
            p.unlink()

    py_lib = USR_DIR / "lib/python3.14"
    for bloat in [
        "idlelib",
        "tkinter",
        "turtledemo",
        "test",
        "pydoc_data",
        "ensurepip",
        "unittest",
        "venv",
    ]:
        target = py_lib / bloat
        if target.is_dir():
            shutil.rmtree(target)
    for cfg_dir in py_lib.glob("config-3.14*"):
        if cfg_dir.is_dir():
            shutil.rmtree(cfg_dir)

    # Remove duplicate .opt-1.pyc and .opt-2.pyc bytecode files
    for opt_pyc in py_lib.rglob("*.opt-[12].pyc"):
        opt_pyc.unlink()

    # Scrub /home/runner build paths from _sysconfigdata and build-details.json
    for meta_file in list(py_lib.glob("_sysconfig*")) + list(
        py_lib.glob("build-details.json")
    ):
        if meta_file.is_file():
            txt = meta_file.read_text(encoding="utf-8", errors="replace")
            txt = (
                txt.replace(str(USR_DIR), "/usr")
                .replace(str(DEPS_PREFIX), "/usr")
                .replace(str(BUILD_DIR), "/tmp")
                .replace("/home/runner", "/root")
            )
            meta_file.write_text(txt, encoding="utf-8")

    # Set clean $ORIGIN RUNPATHs via patchelf
    patchelf = shutil.which("patchelf")
    assert patchelf, "patchelf is required on the build host"
    run(
        [patchelf, "--set-rpath", "$ORIGIN/../lib", str(USR_DIR / "bin/python3.14")],
        cwd=ROOT_DIR,
    )
    for lib_so in ["libpython3.14.so", "libpython3.so"]:
        p = USR_DIR / "lib" / lib_so
        if p.is_file() and not p.is_symlink():
            run([patchelf, "--set-rpath", "$ORIGIN", str(p)], cwd=ROOT_DIR)
    for ext_so in (py_lib / "lib-dynload").glob("*.so"):
        if ext_so.is_file() and not ext_so.is_symlink():
            run([patchelf, "--set-rpath", "$ORIGIN/../..", str(ext_so)], cwd=ROOT_DIR)

    # Strip libpython and C extensions
    strip = env["STRIP"]
    for so_file in USR_DIR.rglob("*.so*"):
        if so_file.is_file() and not so_file.is_symlink():
            subprocess.run([strip, "--strip-unneeded", str(so_file)], check=False)
    for exe_file in [USR_DIR / "bin/python3.14"]:
        if exe_file.is_file():
            subprocess.run([strip, "--strip-unneeded", str(exe_file)], check=False)


def install_proot_distro_and_certs(resolved: dict) -> None:
    """Installs proot-distro v5.x, __main__.py entrypoint, CA certificates, and relocatable launcher."""
    site_pkgs = USR_DIR / "lib/python3.14/site-packages"
    site_pkgs.mkdir(parents=True, exist_ok=True)

    pd_src = SRC_DIR / "proot-distro/proot_distro"
    assert pd_src.is_dir(), f"Missing {pd_src}"
    dest_pd = site_pkgs / "proot_distro"
    if dest_pd.exists():
        shutil.rmtree(dest_pd)
    shutil.copytree(pd_src, dest_pd)

    # Add __main__.py so both `python3 -m proot_distro` and console entrypoints invoke cli.main()
    (dest_pd / "__main__.py").write_text(
        "import sys\nfrom proot_distro.cli import main\n\nif __name__ == '__main__':\n    sys.exit(main())\n",
        encoding="utf-8",
    )

    pd_ver = resolved["proot_distro"]
    dist_info = site_pkgs / f"proot_distro-{pd_ver}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: proot-distro\nVersion: {pd_ver}\n",
        encoding="utf-8",
    )

    tls_dir = USR_DIR / "etc/tls"
    tls_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DOWNLOADS_DIR / "cacert.pem", tls_dir / "cert.pem")

    for rel in ["tmp", "var/lib/proot-distro/containers", "var/lib/proot-distro/cache"]:
        (USR_DIR / rel).mkdir(parents=True, exist_ok=True)

    pd_bin = USR_DIR / "bin/proot-distro"
    pd_bin.write_text(
        "#!/system/bin/sh\n"
        "set -e\n"
        'SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
        'USR_PREFIX="$(CDPATH= cd -- "$SELF_DIR/.." && pwd)"\n'
        'export TERMUX__PREFIX="${TERMUX__PREFIX:-$USR_PREFIX}"\n'
        'export TERMUX_APP__PACKAGE_NAME="${TERMUX_APP__PACKAGE_NAME:-com.jarves.mh}"\n'
        'export TERMUX_VERSION="${TERMUX_VERSION:-0.118.1}"\n'
        'export PD_PROOT_BIN="${PD_PROOT_BIN:-$USR_PREFIX/bin/proot}"\n'
        'export PROOT_TMP_DIR="${PROOT_TMP_DIR:-$USR_PREFIX/tmp}"\n'
        'export PROOT_LOADER="${PROOT_LOADER:-$USR_PREFIX/libexec/proot/loader}"\n'
        'export SSL_CERT_FILE="${SSL_CERT_FILE:-$USR_PREFIX/etc/tls/cert.pem}"\n'
        'export PYTHONHOME="$USR_PREFIX"\n'
        'export PYTHONUTF8="1"\n'
        'export LD_LIBRARY_PATH="$USR_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n'
        'exec "$USR_PREFIX/bin/python3" -c "import sys; from proot_distro.cli import main; sys.exit(main())" "$@"\n',
        encoding="utf-8",
    )
    pd_bin.chmod(0o755)

    pd_alias = USR_DIR / "bin/pd"
    if pd_alias.exists() or pd_alias.is_symlink():
        pd_alias.unlink()
    pd_alias.symlink_to("proot-distro")


def package_and_manifest(resolved: dict) -> None:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = DIST_DIR / ARCHIVE_NAME
    if archive_path.exists():
        archive_path.unlink()

    version_str = f"{datetime.now(timezone.utc).strftime('%Y.%m.%d')}-py{resolved['python']}-pd{resolved['proot_distro']}"
    (USR_DIR / ".mhr-version").write_text(version_str + "\n", encoding="utf-8")

    run(
        [
            "tar",
            "--zstd",
            "-cf",
            str(archive_path),
            "-C",
            str(STAGE_DIR),
            "usr",
        ],
        cwd=ROOT_DIR,
    )

    sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    size_bytes = archive_path.stat().st_size
    download_url = f"https://github.com/{GITHUB_REPOSITORY}/releases/latest/download/{ARCHIVE_NAME}"

    manifest = {
        "version": version_str,
        "pythonVersion": resolved["python"],
        "prootDistroVersion": resolved["proot_distro"],
        "prootVersion": resolved["proot"],
        "dependencies": resolved.get("deps", {}),
        "fileName": ARCHIVE_NAME,
        "url": download_url,
        "sha256": sha256,
        "sizeBytes": size_bytes,
    }
    manifest_path = DIST_DIR / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log(
        f"[ok] Built {archive_path.name} ({size_bytes / 1_048_576:.2f} MB, sha256={sha256})"
    )
    log(f"[ok] Wrote {manifest_path.name}:\n{json.dumps(manifest, indent=2)}")


def main() -> None:
    assert RESOLVED_VERSIONS_FILE.is_file(), "Run fetch.py first"
    resolved = json.loads(RESOLVED_VERSIONS_FILE.read_text(encoding="utf-8"))

    if STAGE_DIR.exists():
        shutil.rmtree(STAGE_DIR)
    USR_DIR.mkdir(parents=True, exist_ok=True)

    toolchain = find_ndk_toolchain()
    env = make_ndk_env(toolchain)

    build_static_deps(env)
    build_proot(env, resolved)
    build_python314(env)
    install_proot_distro_and_certs(resolved)
    package_and_manifest(resolved)


if __name__ == "__main__":
    main()
