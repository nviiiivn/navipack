#!/usr/bin/env python3
"""Give every place in the list a street address, so three Goodwills can be
told apart on the search screen.

Her report, 2026-10-13: "MOST THE TIME I JUST WANT TO SEE LIKE GOODWILLS, BUT
THEY JUST SAY DISTANCE FROM ME, IS IT POSSIBEL TO ALSO SHOW LIKE.... IDK..
ADDRESS OR SOMETHING.. LIKE YOU CLEARLY HAVE GOODWILL IN THE POI BUT I'M
TRYING TO FIND ONE SPECIFIC ONE AND SO I JUST FLIP AROUND ENDLESSLY UNTIL I
GIVE UP.. THATS BAD UX". Three rows reading "charity · Oakland · 2.9 mi" are
three coin flips.

Two sources, best first:

1. OSM's own addr:housenumber + addr:street on the feature. A real address,
   "2235 Telegraph Avenue", and about half of the named places carry one.
2. The nearest named segment OF THE ROUTING GRAPH, within NEAR_STREET_M. No
   number, so it is written "off Telegraph Avenue" - true, and enough to tell
   two shops of one name apart. The graph and not the street rows from
   tools/add_streets.py: those are one point per street and town, so a shop
   halfway along a long street is nowhere near its own street's row.

Stored in a new `addr` column on `place`, which both apps read. Nothing else
in the schema moves, so an older app opening a newer list simply ignores it.

    python3 tools/add_addresses.py [--pbf X.osm.pbf] [--places Y.sqlite] [--out Z]
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)          # tools/ lives inside the app
REPO = os.path.dirname(os.path.dirname(APP))
sys.path[:0] = [os.path.join(APP, "apps", "navi"),
                os.path.join(APP, "packages", "agedashii-washi"),
                os.path.join(APP, "packages", "agedashii-qi")]

from agedashii_navi import places as P                               # noqa: E402

#: How far a place may be from a street before that street is not its address.
NEAR_STREET_M = 80.0
#: Cell size for the street lookup grid, in degrees (~1.1 km).
CELL = 0.01
#: A place and an OSM feature are the same thing within this, same name.
SAME_M = 60.0


def centre(geometry):
    """The middle of a geojson geometry, or None - as build_places.centre."""
    def walk(coords):
        if coords and isinstance(coords[0], (int, float)):
            yield coords[1], coords[0]
            return
        for part in coords or ():
            yield from walk(part)
    points = list(walk((geometry or {}).get("coordinates")))
    if not points:
        return None
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


def osm_addresses(pbf, workdir):
    """{normalised name: [(lat, lon, "12 Main Street")]} from the extract."""
    named = os.path.join(workdir, "named.osm.pbf")
    seq = os.path.join(workdir, "named.geojsonseq")
    subprocess.run(["osmium", "tags-filter", "-O", "-o", named, pbf, "nwr/name"], check=True)
    subprocess.run(["osmium", "export", "-O", "-f", "geojsonseq", "-o", seq, named], check=True)
    out = {}
    kept = 0
    with open(seq, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            try:
                feature = json.loads(line)
            except ValueError:
                continue
            props = feature.get("properties") or {}
            name, street = props.get("name"), props.get("addr:street")
            if not name or not street:
                continue
            point = centre(feature.get("geometry"))
            if point is None:
                continue
            number = (props.get("addr:housenumber") or "").strip()
            out.setdefault(" ".join(P.norm_tokens(name)), []).append(
                (point[0], point[1], f"{number} {street}".strip()))
            kept += 1
    print(f"  {kept} OSM features carry addr:street, {len(out)} distinct names")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", default=os.path.join(
        REPO, "work", "minion4", "graph-extent.osm.pbf"))
    ap.add_argument("--graph", default=os.path.join(APP, "assets/maps/bayarea.rg"))
    ap.add_argument("--places", default=os.path.join(APP, "assets/maps/places.sqlite"))
    ap.add_argument("--out", default=None, help="default: edit --places in place")
    args = ap.parse_args()

    out = args.out
    if out:
        shutil.copyfile(args.places, out)
    else:
        out = args.places
    con = sqlite3.connect(out)
    cols = {r[1] for r in con.execute("PRAGMA table_info(place)")}
    if "addr" not in cols:
        con.execute("ALTER TABLE place ADD COLUMN addr TEXT")
        print("added the addr column")
    con.execute("UPDATE place SET addr = NULL")

    # Every named segment of the graph, on a grid: the fallback address is the
    # street a place actually stands on, which needs the real geometry and not
    # one point per street.
    from agedashii_washi import routing
    graph = routing.Graph(args.graph)
    streets = {}
    held = 0
    for sid in range(graph.s):
        name = graph.name(sid)
        if not name:
            continue
        point = P.segment_point(graph, sid)
        if point is None:
            continue
        streets.setdefault((int(point[0] / CELL), int(point[1] / CELL)), []).append(
            (point[0], point[1], name))
        held += 1
    print(f"  {held} named segments to fall back on")

    def near_street(lat, lon):
        best, gap = None, NEAR_STREET_M
        cy, cx = int(lat / CELL), int(lon / CELL)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for slat, slon, name in streets.get((cy + dy, cx + dx), ()):
                    d = P.routing.haversine_m((lat, lon), (slat, slon))
                    if d < gap:
                        best, gap = name, d
        return best

    with tempfile.TemporaryDirectory(dir="/home/demo/Sandbox/tmp") as work:
        t0 = time.time()
        by_name = osm_addresses(args.pbf, work)
        print(f"  read the extract in {time.time() - t0:.0f}s")

    exact, nearby, none = [], [], 0
    rows = con.execute("SELECT id, name, kind, lat, lon FROM place "
                       "WHERE kind != 'street'").fetchall()
    for pid, name, _kind, lat, lon in rows:
        found = None
        for alat, alon, addr in by_name.get(" ".join(P.norm_tokens(name)), ()):
            # Same name AND the same spot: OSM draws a shop as a point and a
            # building, and the list keeps one of them.
            if P.routing.haversine_m((lat, lon), (alat, alon)) <= SAME_M:
                found = addr
                break
        if found:
            exact.append((found, pid))
            continue
        street = near_street(lat, lon)
        if street:
            nearby.append((f"off {street}", pid))
        else:
            none += 1
    con.executemany("UPDATE place SET addr = ? WHERE id = ?", exact)
    con.executemany("UPDATE place SET addr = ? WHERE id = ?", nearby)
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('addresses', ?)",
                (str(len(exact) + len(nearby)),))
    con.commit()
    con.execute("VACUUM")
    print(f"{len(exact)} real addresses, {len(nearby)} by nearest street, "
          f"{none} with neither, of {len(rows)} places")
    for q in ("Goodwill", "IKEA"):
        print(f"  {q}:")
        for name, addr, kind in con.execute(
                "SELECT name, addr, kind FROM place WHERE name = ? LIMIT 4", (q,)):
            print(f"    {name:10} {kind:12} {addr}")
    con.close()
    print(f"{out}: {os.path.getsize(out):,} bytes")


if __name__ == "__main__":
    main()
