#!/usr/bin/env python3
"""Build a swappable map pack: one file holding a whole region.

Her request: "miami/keys/ft lauderdale, greater seattle area (redmond to
tacoma?), bay area, NYC area" and "premade packs for others to use".

A pack is a plain zip. The first entry is always the MANIFEST, so the app can
read it while streaming the rest and never needs a 200 MB temporary copy:

    MANIFEST                        <- first, always
    bayarea.pmtiles                 the map
    bayarea-valhalla-3.6.3.tar      the router's tiles
    places.sqlite                   the searchable list

The three filenames do NOT change per region. The app looks for exactly these
names and the pack only changes what is inside them, so one pack format works
for every region and nothing in the app has to learn a naming scheme. The
region's name travels in the MANIFEST instead:

    version <hash of the three hashes>
    region Greater Seattle
    bayarea.pmtiles\t<size>\t<sha256>
    ...

`version` is what makes a phone unpack: it changes whenever any file does, so
importing a different region always replaces the data, and importing the same
pack twice costs nothing.

    python3 tools/make_pack.py out/seattle.navipack \\
        --region "Greater Seattle" \\
        --map seattle.pmtiles --tiles seattle-valhalla-3.6.3.tar \\
        --places seattle-places.sqlite
"""
import argparse
import hashlib
import os
import sys
import zipfile

#: The names the app looks for. Not per region - see the note above.
NAMES = {"map": "bayarea.pmtiles",
         "tiles": "bayarea-valhalla-3.6.3.tar",
         "places": "places.sqlite"}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out", help="the .navipack to write")
    ap.add_argument("--region", required=True, help='shown in Settings, e.g. "Greater Seattle"')
    for key in NAMES:
        ap.add_argument(f"--{key}", required=True)
    args = ap.parse_args()

    files = []
    for key, name in NAMES.items():
        src = getattr(args, key)
        if not os.path.isfile(src):
            sys.exit(f"{key}: {src} is not a file")
        files.append((name, src, os.path.getsize(src), sha256(src)))

    version = hashlib.sha256("".join(f[3] for f in files).encode()).hexdigest()[:16]
    manifest = f"version {version}\nregion {args.region}\n" + "".join(
        f"{name}\t{size}\t{digest}\n" for name, _src, size, digest in files)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with zipfile.ZipFile(args.out, "w", allowZip64=True) as z:
        # MANIFEST first, so the app can read it from a stream.
        z.writestr("MANIFEST", manifest)
        for name, src, _size, _digest in files:
            # Stored, not deflated: the tiles and the archive are compressed
            # already, and deflating 200 MB again costs minutes on both ends
            # for nothing. The zip here is an envelope, not a squeeze.
            z.write(src, name, compress_type=zipfile.ZIP_STORED)
    print(manifest.rstrip())
    print(f"{args.out}: {os.path.getsize(args.out):,} bytes")


if __name__ == "__main__":
    main()
