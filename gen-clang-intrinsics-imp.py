#!/usr/bin/env python3
"""Generate an IWYU mapping (.imp) file for Clang's intrinsics headers.

Method follows IWYU PR #545 (commit e8066f0): each private intrinsics
header in Clang's resource directory carries an #error pragma of the form

    #error "Never use <avx2intrin.h> directly; include <immintrin.h> instead."

naming the public umbrella header(s).  This script parses those pragmas
statically and emits one mapping per (private, public) pair, in the
quoted .imp style used by IWYU master since 2024.

With --verify, each private header is additionally compiled directly
(`clang -fsyntax-only`) and the emitted diagnostic is cross-checked
against the statically parsed mapping, reproducing the original
empirical derivation.

Usage:
    gen-clang-intrinsics-imp.py [--prefix /usr/lib64/llvm22] [--verify] \
        [-o clang-22.intrinsics.imp]
"""

import argparse
import os
import re
import subprocess
import sys

# "Never use <X> directly; include <A> or <B> instead."
# Variants observed in clang 18 resource headers:
#   "... directly; use <A> instead."      (amxfp16intrin.h)
#   "... directly; include <A> or <B> instead."  (prfchwintrin.h)
ERROR_RE = re.compile(
    r'#\s*error\s+"Never\s+(?:use|include)\s+<(?P<private>[^>]+)>\s+directly'
    r'[;,]?\s+(?:please\s+)?(?:use|include)\s+(?P<publics>.*?)\s+instead',
    re.IGNORECASE,
)
PUBLIC_RE = re.compile(r"<([^>]+)>")

# Same message as rendered in a compiler diagnostic:
#   path:line:col: error: "Never use <X> directly; include <A> instead."
# (no '#', and the echoed source line may contain a spliced 'NN |' gutter,
# so match the diagnostic line rather than the source echo).
DIAG_RE = re.compile(
    r'error:\s*"Never\s+(?:use|include)\s+<(?P<private>[^>]+)>\s+directly'
    r'[;,]?\s+(?:please\s+)?(?:use|include)\s+(?P<publics>.*?)\s+instead',
    re.IGNORECASE,
)


def clang_query(clang, *args):
    return subprocess.run(
        [clang, *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def join_continuations(text):
    """Splice backslash-newline, as the preprocessor does (avx512vlfp16intrin.h
    et al. split the #error directive across lines)."""
    return re.sub(r"\\\r?\n", " ", text)


def parse_header(path):
    """Return list of (private, [publics]) found in one header."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = join_continuations(f.read())
    out = []
    for m in ERROR_RE.finditer(text):
        publics = PUBLIC_RE.findall(m.group("publics"))
        if publics:
            out.append((m.group("private"), publics))
    return out


def compile_diagnostic(clang, header, lang):
    """Include HEADER directly; return the 'Never use ...' diagnostic text,
    or None if it compiles or fails for unrelated reasons."""
    proc = subprocess.run(
        [clang, "-x", lang, "-fsyntax-only", "-w", "-ferror-limit=0", "-"],
        input=f"#include <{header}>\n",
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return None
    m = DIAG_RE.search(proc.stderr)
    return m.group(0) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="/usr/lib64/llvm22",
                    help="LLVM installation prefix (default: %(default)s)")
    ap.add_argument("--verify", action="store_true",
                    help="cross-check by compiling each private header directly")
    ap.add_argument("-o", "--output", default=None,
                    help="output file (default: clang-<major>.intrinsics.imp)")
    args = ap.parse_args()

    clang = os.path.join(args.prefix, "bin", "clang")
    if not os.access(clang, os.X_OK):
        sys.exit(f"error: {clang} not found or not executable")

    version = clang_query(clang, "-dumpversion")          # e.g. 22.1.0
    major = version.split(".")[0]
    resource_dir = clang_query(clang, "-print-resource-dir")
    incdir = os.path.join(resource_dir, "include")
    if not os.path.isdir(incdir):
        sys.exit(f"error: resource include dir {incdir} does not exist")

    headers = sorted(
        f for f in os.listdir(incdir)
        if f.endswith(".h") and os.path.isfile(os.path.join(incdir, f))
    )

    mappings = {}   # private -> set of publics
    warnings = []
    for hdr in headers:
        for private, publics in parse_header(os.path.join(incdir, hdr)):
            if private != hdr:
                # e.g. avx512vp2intersectintrin.h, whose pragma misspells its
                # own name as <avx512vp2intersect.h>; the mapping must use the
                # real filename.
                warnings.append(
                    f"{hdr}: #error names <{private}>, not itself; "
                    f"mapping keyed by filename")
            mappings.setdefault(hdr, set()).update(publics)

    # Sanity: every public header named in a pragma should exist and must not
    # itself be private (no chained privacy in clang's resource headers, but
    # guard against it appearing in future versions).
    for private, publics in sorted(mappings.items()):
        for pub in sorted(publics):
            if not os.path.isfile(os.path.join(incdir, pub)):
                warnings.append(
                    f"{private}: public header <{pub}> not present in {incdir}")
            if pub in mappings:
                warnings.append(
                    f"{private}: public header <{pub}> is itself private "
                    f"(maps to {sorted(mappings[pub])})")

    if args.verify:
        for private in sorted(mappings):
            diags = {compile_diagnostic(clang, private, lang)
                     for lang in ("c", "c++")}
            diags.discard(None)
            if not diags:
                warnings.append(
                    f"{private}: direct inclusion did not reproduce the "
                    f"#error diagnostic (target-conditional guard?)")
                continue
            seen = set()
            for d in diags:
                m = DIAG_RE.search(d)
                seen.update(PUBLIC_RE.findall(m.group("publics")))
            if not seen <= mappings[private]:
                warnings.append(
                    f"{private}: compiler names {sorted(seen)}, "
                    f"parser found {sorted(mappings[private])}")

    out_path = args.output or f"clang-{major}.intrinsics.imp"
    width = max((len(p) for p in mappings), default=0) + 4  # <> + quotes align
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# These mappings based on #error pragmas in the header "
                f"files below in clang {version}\n")
        f.write(f"# Generated from {incdir}\n")
        f.write("[\n")
        for private in sorted(mappings):
            for pub in sorted(mappings[private]):
                lhs = f'"<{private}>",'.ljust(width + 2)
                f.write(f'  {{ include: [{lhs} "private", '
                        f'"<{pub}>", "public"] }},\n')
        f.write("]\n")

    n_priv = len(mappings)
    n_map = sum(len(v) for v in mappings.values())
    print(f"{out_path}: {n_map} mappings for {n_priv} private headers "
          f"(clang {version})", file=sys.stderr)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
