#!/usr/bin/env python3
"""Build a whole map pack for a region, from a bounding box.

Her request: "create a builder (if one doesn't exist) to make ones own
'background map's kinda maybe as a pwa... you tel lme .. recommenda soltiopm".

THE RECOMMENDATION, written down so the reason survives. A browser cannot do
this. The router's tiles alone are a multi-gigabyte graph build, and the OSM
extract for one metro area is hundreds of megabytes. So the builder is a
script on the machine that has the disk and the docker daemon, and the "PWA"
is a thin page that draws a box and asks this script for a pack. The page is
the handle; this is the tool.

Four things go into a pack, and only one of them is expensive:

  map        Extracted, not built. The Protomaps basemap is published daily as
             one global PMTiles archive, and `pmtiles extract` pulls a bounding
             box out of it over HTTP range requests - a Seattle-sized region in
             about 5 seconds. The app's shipped map is this same schema at
             zoom 0-14, so an extract is a drop-in replacement.
  osm        A Geofabrik regional extract, cached between runs, cut to the box
             with `osmium extract`.
  valhalla   The router's tiles, built in the official valhalla-scripted
             container that is already running on this machine. This is the
             slow stage.
  places     build_places.py, then add_streets.py --pbf (so no RGRAPH graph is
             needed - that file is the Flet router's and Kotlin uses Valhalla),
             then add_addresses.py for the street addresses on each row.

Stages are skipped when their output is already there, so a run that dies in
the middle carries on where it stopped. --only runs one stage.

    python3 tools/build_pack.py seattle \\
        --region "Greater Seattle" \\
        --bbox -122.52,47.15,-121.95,47.75 \\
        --geofabrik north-america/us/washington
"""
import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
#: The Navi app tree whose packages these tools import. tools/ normally lives
#: inside it; NAVIPACK_APP points somewhere else.
APP = os.environ.get("NAVIPACK_APP") or os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(APP))
#: Downloads, the Valhalla build and finished packs land here.
WORK = os.path.expanduser(os.environ.get("NAVIPACK_WORK", "~/.cache/navipack"))
#: The go-pmtiles CLI.
PMTILES = (os.environ.get("PMTILES") or shutil.which("pmtiles")
           or os.path.expanduser("~/.local/bin/go-pmtiles"))
#: The container already running here, so no image is pulled for a build.
VALHALLA_IMAGE = "ghcr.io/valhalla/valhalla-scripted:latest"
#: What the app calls the router's tiles - the version is part of the name.
TILES_NAME = "bayarea-valhalla-3.6.3.tar"


def run(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd[:6]), "…" if len(cmd) > 6 else "", flush=True)
    return subprocess.run(cmd, check=True, **kw)


def have(path, what):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"  {what}: already there ({os.path.getsize(path):,} bytes)")
        return True
    return False


def protomaps_build():
    """The most recent daily global build that answers, as YYYYMMDD.

    Asked with curl and not urllib: the host is behind Cloudflare, which turns
    urllib's default user agent away, so every probe "failed" while the same
    URL fetched perfectly from the shell.
    """
    for back in range(1, 8):
        day = (dt.date.today() - dt.timedelta(days=back)).strftime("%Y%m%d")
        out = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "-r", "0-0",
             f"https://build.protomaps.com/{day}.pmtiles"],
            capture_output=True, text=True, timeout=90).stdout.strip()
        if out in ("200", "206"):
            return day
        print(f"  {day}: {out}")
    sys.exit("no Protomaps daily build answered in the last week")


def stage_map(args, out):
    if have(out, "map"):
        return
    day = protomaps_build()
    print(f"  extracting the map from the {day} global build")
    run([PMTILES, "extract", f"https://build.protomaps.com/{day}.pmtiles",
         f"--bbox={args.bbox}", "--maxzoom=14", out])


def stage_osm(args, out):
    if have(out, "osm"):
        return
    src = os.path.join(WORK, "geofabrik", args.geofabrik.replace("/", "_") + "-latest.osm.pbf")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    if not have(src, "geofabrik extract"):
        url = f"https://download.geofabrik.de/{args.geofabrik}-latest.osm.pbf"
        print(f"  downloading {url}")
        # -s: curl's progress meter writes a carriage-return bar that fills a
        # log with tens of thousands of characters and tells nobody anything.
        run(["curl", "-fsSL", "--retry", "3", "-o", src + ".part", url])
        os.rename(src + ".part", src)
    run(["osmium", "extract", "--bbox", args.bbox, "--set-bounds",
         "-O", "-o", out, src])


def stage_valhalla(args, out):
    if have(out, "valhalla tiles"):
        return
    build = os.path.join(WORK, args.slug, "valhalla")
    os.makedirs(build, exist_ok=True)
    pbf = os.path.join(WORK, args.slug, f"{args.slug}.osm.pbf")
    shutil.copy(pbf, os.path.join(build, "input.osm.pbf"))
    # The official scripted image: it builds the tiles and the tar, then stops.
    # --build-tar leaves exactly the one file the app wants.
    run(["docker", "run", "--rm",
         "-v", f"{build}:/custom_files",
         "-e", "tile_urls=",
         "-e", "build_elevation=False",
         "-e", "build_admins=False",
         "-e", "build_time_zones=False",
         "-e", "build_tar=True",
         "-e", "serve_tiles=False",
         "-e", "force_rebuild=False",
         VALHALLA_IMAGE])
    made = os.path.join(build, "valhalla_tiles.tar")
    if not os.path.isfile(made):
        sys.exit(f"the container left no valhalla_tiles.tar in {build}")
    shutil.move(made, out)


def stage_places(args, out):
    if have(out, "places"):
        return
    pbf = os.path.join(WORK, args.slug, f"{args.slug}.osm.pbf")
    env = dict(os.environ)
    env["PYTHONPATH"] = ":".join([
        os.path.join(APP, "apps", "navi"),
        os.path.join(APP, "packages", "agedashii-washi"),
        os.path.join(APP, "packages", "agedashii-qi")])
    run([sys.executable, os.path.join(HERE, "build_places.py"),
         "--pbf", pbf, "--out", out], env=env)
    # The streets, from the extract: a Kotlin pack has no RGRAPH graph.
    run([sys.executable, os.path.join(HERE, "add_streets.py"),
         "--pbf", pbf, "--places", out], env=env)
    # The addresses on each row, so three Goodwills can be told apart.
    run([sys.executable, os.path.join(HERE, "add_addresses.py"),
         "--pbf", pbf, "--places", out], env=env)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug", help="short name for the files, e.g. seattle")
    ap.add_argument("--region", required=True, help='shown in Settings, e.g. "Greater Seattle"')
    ap.add_argument("--bbox", required=True, help="minlon,minlat,maxlon,maxlat")
    ap.add_argument("--geofabrik", required=True,
                    help="the Geofabrik path, e.g. north-america/us/washington")
    ap.add_argument("--only", choices=["map", "osm", "valhalla", "places", "pack"],
                    help="run one stage and stop")
    args = ap.parse_args()

    out = os.path.join(WORK, args.slug)
    os.makedirs(out, exist_ok=True)
    paths = {
        "map": os.path.join(out, f"{args.slug}.pmtiles"),
        "osm": os.path.join(out, f"{args.slug}.osm.pbf"),
        "valhalla": os.path.join(out, TILES_NAME),
        "places": os.path.join(out, "places.sqlite"),
        "pack": os.path.join(out, f"{args.slug}.navipack"),
    }
    stages = [("map", stage_map), ("osm", stage_osm),
              ("valhalla", stage_valhalla), ("places", stage_places)]
    for name, fn in stages:
        if args.only and args.only != name:
            continue
        print(f"\n########## {name}", flush=True)
        t0 = time.time()
        fn(args, paths[name])
        print(f"  {name}: {time.time() - t0:.0f}s", flush=True)

    if args.only and args.only != "pack":
        return
    print("\n########## pack", flush=True)
    run([sys.executable, os.path.join(HERE, "make_pack.py"), paths["pack"],
         "--region", args.region, "--map", paths["map"],
         "--tiles", paths["valhalla"], "--places", paths["places"]])
    print(f"\n{paths['pack']}: {os.path.getsize(paths['pack']):,} bytes")


if __name__ == "__main__":
    main()
