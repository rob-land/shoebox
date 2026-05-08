#!/usr/bin/env python3
"""
fix-flatpak-deps.py — Replace PyPI source tarballs in python3-deps.json
with pre-built wheels.

Usage:
    python3 fix-flatpak-deps.py [--arch x86_64,aarch64] build-aux/flatpak/python3-deps.json

By default emits a multi-arch manifest: each tarball is replaced with one
wheel source per target architecture, each tagged with `only-arches` so
flatpak-builder picks the right one for `--arch`. Pure-Python wheels
(`-none-any.whl`) replace the tarball with a single arch-agnostic source.

Pass `--arch x86_64` (or `aarch64`) to produce a single-arch manifest
without `only-arches` tags (legacy behavior).

flatpak-pip-generator produces a nested module tree:
  {
    "name": "python3-deps",        <-- top-level wrapper module
    "modules": [
      {
        "name": "python3-cryptography",
        "sources": [               <-- tarballs are HERE, one level down
          { "url": "...cryptography-46.0.7.tar.gz" }
        ]
      }, ...
    ]
  }

The script walks this tree recursively, finds tarball sources from PyPI,
queries the PyPI JSON API for matching pre-built wheels, and rewrites the
sources list in-place.
"""

import argparse, json, re, sys, urllib.request
from pathlib import Path

_ABI_PREFERENCE = [
    "cp311-abi3",
    "cp38-abi3",
    "cp37-abi3",
    "cp313-cp313",
    "cp313-abi3",
    "cp312-cp312",
    "cp312-abi3",
    "cp311-cp311",
]

_DEFAULT_ARCHES = ["x86_64", "aarch64"]


def _compatible_with_cp313(filename):
    """Return True if the wheel is usable by CPython 3.13."""
    stem = filename[:-4]  # remove .whl
    parts = stem.split("-")
    if len(parts) < 5:
        return False
    python_tag, abi_tag = parts[2], parts[3]
    if abi_tag == "none":
        return True
    if abi_tag == "abi3":
        m = re.match(r"cp3(\d+)$", python_tag)
        if m and int(m.group(1)) <= 13:
            return True
        return False
    if python_tag == "cp313" and abi_tag == "cp313":
        return True
    return False


def _rank(wheel):
    fn = wheel["filename"]
    for i, tag in enumerate(_ABI_PREFERENCE):
        if tag in fn:
            return i
    return len(_ABI_PREFERENCE)


def pypi_wheels(name, version):
    """Return all PyPI wheels for name+version (any arch, any platform)."""
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as exc:
        print(f"    WARNING: could not query PyPI for {name} {version}: {exc}")
        return []
    return [u for u in data.get("urls", []) if u["filename"].endswith(".whl")]


def parse_tarball(source):
    url = source.get("url", "")
    if not url.startswith("https://files.pythonhosted.org/"):
        return None
    fn = url.split("/")[-1]
    if fn.endswith(".whl"):
        return None
    m = re.match(r"^([A-Za-z0-9_.-]+?)-(\d[^-]*)\.(tar\.gz|zip)$", fn)
    if not m:
        return None
    name = re.sub(r"[-_.]+", "-", m.group(1)).lower()
    return name, m.group(2)


def _wheel_source(wheel, only_arches=None):
    src = {
        "type": "file",
        "url": wheel["url"],
        "sha256": wheel["digests"]["sha256"],
    }
    if only_arches:
        src["only-arches"] = list(only_arches)
    return src


def replacements_for(src, parsed, arches):
    """
    Return a list of source dicts to replace `src`. May include one
    pure-Python wheel, or one wheel per arch, or per-arch tarball
    fallbacks where no wheel exists.
    """
    name, version = parsed
    fn = src["url"].split("/")[-1]
    print(f"    tarball : {fn}")
    wheels = pypi_wheels(name, version)

    pure = [w for w in wheels
            if w["filename"].endswith("-none-any.whl")
            and _compatible_with_cp313(w["filename"])]
    if pure:
        best = sorted(pure, key=_rank)[0]
        print(f"    wheel   : {best['filename']} (pure-Python)")
        return [_wheel_source(best)]

    tag_arches = len(arches) > 1
    out = []
    for arch in arches:
        arch_wheels = [w for w in wheels
                       if arch in w["filename"]
                       and "linux" in w["filename"]
                       and _compatible_with_cp313(w["filename"])]
        if arch_wheels:
            best = sorted(arch_wheels, key=_rank)[0]
            print(f"    wheel   : {best['filename']}"
                  + (f"  [{arch}]" if tag_arches else ""))
            out.append(_wheel_source(best, [arch] if tag_arches else None))
        else:
            print(f"    WARNING : no {arch} wheel — keeping tarball for {arch}")
            fallback = dict(src)
            if tag_arches:
                fallback["only-arches"] = [arch]
            out.append(fallback)
    return out


def fix_sources(module, arches):
    """Rewrite module['sources'] in place. Returns count of replaced tarballs."""
    new_sources = []
    replaced = 0
    for src in module.get("sources", []):
        parsed = parse_tarball(src)
        if parsed is None:
            new_sources.append(src)
            continue
        new_sources.extend(replacements_for(src, parsed, arches))
        replaced += 1
    module["sources"] = new_sources
    return replaced


def walk(obj, arches, depth=0):
    total = 0
    if isinstance(obj, list):
        for item in obj:
            total += walk(item, arches, depth)
        return total
    name = obj.get("name", "<unnamed>")
    print("  " * depth + f"module: {name}")
    total += fix_sources(obj, arches)
    for sub in obj.get("modules", []):
        total += walk(sub, arches, depth + 1)
    return total


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("json_file")
    p.add_argument(
        "--arch",
        default=",".join(_DEFAULT_ARCHES),
        help="Comma-separated target arches. Default: x86_64,aarch64. "
             "Pass a single arch to omit only-arches tags.",
    )
    args = p.parse_args()

    arches = [a.strip() for a in args.arch.split(",") if a.strip()]
    if not arches:
        sys.exit("Error: --arch must list at least one architecture")

    path = Path(args.json_file)
    if not path.exists():
        sys.exit(f"Error: {path} not found")

    original = path.read_text()
    data = json.loads(original)

    print(f"Scanning {path} for tarball sources (arches: {', '.join(arches)}) ...\n")
    replaced = walk(data, arches)

    if replaced == 0:
        print("\nNo tarballs needed replacing — nothing to do.")
        return

    backup = path.with_suffix(".json.bak")
    backup.write_text(original)
    print(f"\nBackup : {backup}")
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Patched {replaced} tarball source(s) in {path}")
    print("\nDone. Re-run flatpak-builder now.")


if __name__ == "__main__":
    main()
