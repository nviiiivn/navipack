#!/usr/bin/env python3
"""Put the road graph's street names into the places list, one row per street
AND TOWN, so an address can be searched offline.

Why this exists. Her report, 2026-10-12: "I TRIED LITERALLY PUTTING IN AN
ADDRESS AND THE APP WOULDN'T TAKE ME THERE -- I COULDN'T FIGURE OUT WHICH
GOODWILL WAS THE ONE I WANTED SO I PUT IN THE ADDRESS AND IT WOULDNT' LET ME
DO THAT". The Kotlin app - the one she actually rides with - searches
places.sqlite and nothing else. It has no street index at all, and Valhalla
routes without geocoding, so a street name matched nothing on the phone.

Rather than build a second search in Kotlin, the streets join the list the
search already reads: kind "street", tokenised in `tok` like every other row,
with the town in `a1`. Both apps then find them with no change to their search
code, and "Main Street" in Walnut Creek is a DIFFERENT ROW from "Main Street"
in Oakland - which the routing graph's own name index could never express,
because it keeps one segment per name for the whole Bay Area.

A street is placed at the segment of that name nearest its town's middle, so
routing to the row lands in the right part of the right street.

    python3 tools/add_streets.py [--graph X.rg] [--places Y.sqlite] [--out Z.sqlite]
"""
import argparse
import math
import os
import shutil
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
#: The Navi app tree whose packages these tools import. tools/ normally lives
#: inside it; NAVIPACK_APP points somewhere else.
APP = os.environ.get("NAVIPACK_APP") or os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(APP))
sys.path[:0] = [os.path.join(APP, "apps", "navi"),
                os.path.join(APP, "packages", "agedashii-washi"),
                os.path.join(APP, "packages", "agedashii-qi")]

from agedashii_washi import routing                                  # noqa: E402
from agedashii_navi import places as P                               # noqa: E402

#: Where a street is said to be: the settlement kinds, biggest first. A street
#: whose nearest settlement is further than this is still kept, with no town -
#: a county road has no town and is still a road she may want.
SETTLEMENT = ("city", "town", "village", "suburb", "neighbourhood",
              "locality", "hamlet")
#: Beyond this, a street is not "in" that town.
TOWN_REACH_M = 12000.0
#: Cell size for the settlement lookup grid, in degrees (~5.5 km).
CELL = 0.05


def norm(text):
    return " ".join(P.norm_tokens(text))


def streets_from_graph(path):
    """(name, lat, lon) for every named segment of an RGRAPH file."""
    from agedashii_washi import routing as R
    graph = R.Graph(path)
    print(f"graph: {graph.s} segments")
    for sid in range(graph.s):
        name = graph.name(sid)
        if not name:
            continue
        point = P.segment_point(graph, sid)
        if point is not None:
            yield name, point[0], point[1]


def streets_from_pbf(pbf):
    """(name, lat, lon) for every named road in an OSM extract.

    One point per WAY, which is finer than one per street and coarser than
    every node - enough for "the piece of Main Street nearest Walnut Creek".
    """
    import json
    import subprocess
    import tempfile
    # NAVIPACK_TMP: osmium's export is large, and /tmp is often a small tmpfs.
    with tempfile.TemporaryDirectory(dir=os.environ.get("NAVIPACK_TMP")) as work:
        roads = os.path.join(work, "roads.osm.pbf")
        seq = os.path.join(work, "roads.geojsonseq")
        subprocess.run(["osmium", "tags-filter", "-O", "-o", roads, pbf, "w/highway"],
                       check=True)
        subprocess.run(["osmium", "export", "-O", "-f", "geojsonseq", "-o", seq, roads],
                       check=True)
        with open(seq, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip().lstrip("\x1e")
                if not line:
                    continue
                try:
                    feature = json.loads(line)
                except ValueError:
                    continue
                name = (feature.get("properties") or {}).get("name")
                if not name:
                    continue
                point = middle(feature.get("geometry"))
                if point is not None:
                    yield name, point[0], point[1]


def middle(geometry):
    """The mean of a geometry's points, or None."""
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", default=os.path.join(APP, "assets/maps/bayarea.rg"))
    # A region built for the Kotlin app has no .rg graph: that file is the Flet
    # router's, and Kotlin routes with Valhalla. The street names are in the
    # OSM extract either way, so a pack can be built from the extract alone.
    ap.add_argument("--pbf", default=None,
                    help="read streets from this OSM extract instead of --graph")
    ap.add_argument("--places", default=os.path.join(APP, "assets/maps/places.sqlite"))
    ap.add_argument("--out", default=None, help="default: edit --places in place")
    args = ap.parse_args()

    out = args.out
    if out:
        shutil.copyfile(args.places, out)
    else:
        out = args.places
    con = sqlite3.connect(out)

    # Streets already added by a previous run go, so this is repeatable.
    old = [r[0] for r in con.execute("SELECT id FROM place WHERE kind = 'street'")]
    if old:
        con.executemany("DELETE FROM tok WHERE id = ?", [(i,) for i in old])
        con.execute("DELETE FROM place WHERE kind = 'street'")
        con.commit()
        print(f"removed {len(old)} street rows from a previous run")

    # The settlements, on a grid, so 467k segments can each find their town.
    grid = {}
    towns = 0
    for name, kind, lat, lon in con.execute(
            "SELECT name, kind, lat, lon FROM place WHERE kind IN "
            f"({','.join('?' * len(SETTLEMENT))})", SETTLEMENT):
        grid.setdefault((int(lat / CELL), int(lon / CELL)), []).append(
            (lat, lon, name, SETTLEMENT.index(kind)))
        towns += 1
    # Two areas can normalise to one name ("Oakland" and "oakland" both exist
    # in OSM). The row the search SHOWS is this one, so prefer the one spelled
    # like a place name over a lowercase duplicate.
    area_id = {}
    for aid, name in con.execute("SELECT aid, name FROM area"):
        key = norm(name)
        if key not in area_id or (name[:1].isupper() and not area_id[key][1]):
            area_id[key] = (aid, name[:1].isupper())
    area_id = {k: v[0] for k, v in area_id.items()}
    print(f"{towns} settlements, {len(area_id)} named areas")

    def town_of(lat, lon):
        """(name, distance) of the settlement this point belongs to, or None."""
        cy, cx = int(lat / CELL), int(lon / CELL)
        best = None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for tlat, tlon, name, rank in grid.get((cy + dy, cx + dx), ()):
                    gap = routing.haversine_m((lat, lon), (tlat, tlon))
                    if gap > TOWN_REACH_M:
                        continue
                    # A bigger kind wins a tie of 2 km, so a street in a named
                    # neighbourhood of Oakland is still in Oakland.
                    key = (rank, gap // 2000, gap)
                    if best is None or key < best[0]:
                        best = (key, name, gap)
        return (best[1], best[2]) if best else None

    # (street name, town) -> the piece of it nearest that town's middle.
    best = {}
    t0 = time.time()
    seen = 0
    for name, lat, lon in (streets_from_pbf(args.pbf) if args.pbf
                           else streets_from_graph(args.graph)):
        seen += 1
        found = town_of(lat, lon)
        town, gap = found if found else ("", 0.0)
        key = (name, town)
        if key not in best or gap < best[key][0]:
            best[key] = (gap, (lat, lon))
    print(f"{len(best)} street rows from {seen} pieces in {time.time() - t0:.0f}s")

    next_id = con.execute("SELECT max(id) FROM place").fetchone()[0] + 1
    rows, toks = [], []
    for (name, town), (_gap, (lat, lon)) in sorted(best.items()):
        aid = area_id.get(norm(town)) if town else None
        rows.append((next_id, name, "street", lat, lon, 0.0, 0, aid, None))
        # Tokenised exactly as the search tokenises what she types, through the
        # one shared norm_tokens, so the two can never drift apart.
        for word in dict.fromkeys(P.norm_tokens(name)):
            toks.append((word, next_id))
        next_id += 1
    con.executemany("INSERT INTO place (id, name, kind, lat, lon, area_m2, notable, a1, a2) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.executemany("INSERT INTO tok (t, id) VALUES (?, ?)", toks)
    total = con.execute("SELECT count(*) FROM place").fetchone()[0]
    con.execute("UPDATE meta SET value = ? WHERE key = 'count'", (str(total),))
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('streets', ?)",
                (str(len(rows)),))
    con.commit()
    con.execute("VACUUM")
    con.close()
    print(f"{len(rows)} streets, {len(toks)} tokens; place is now {total} rows")
    print(f"{out}: {os.path.getsize(out):,} bytes")


if __name__ == "__main__":
    main()
