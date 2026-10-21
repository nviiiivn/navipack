#!/usr/bin/env python3
"""Build Navi's offline places list: every named place worth riding to.

The road graph (.rg) holds streets and nothing else, so "Chabot Space and
Science Center" could never be found without signal - and the online
geocoder is a gazetteer of towns that does not know it either. This makes the
list the app searches offline: named museums, parks, cafes, fuel, peaks,
stations and the rest, from the SAME OSM extract as the graph.

    python3 tools/build_places.py --pbf bayarea.osm.pbf --out assets/maps/places.sqlite

Needs osmium-tool on PATH. Output is plain SQLite (no FTS5 - whether the
phone's Python has it is unverified): a `place` table, a `tok` table of
(word, place id) with an index so a word PREFIX is a range scan, and `meta`.
Words come from agedashii_navi.places.norm_tokens, the same function the app
searches with, so the two can never split a name differently.
"""
import argparse
import datetime as dt
import json
import math
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile

#: The Navi app tree (see build_pack.py); NAVIPACK_APP points somewhere else.
REPO = pathlib.Path(os.environ.get("NAVIPACK_APP") or pathlib.Path(__file__).resolve().parent.parent)
sys.path[:0] = [str(REPO / "apps" / "navi"), str(REPO / "packages" / "agedashii-washi")]
from agedashii_navi.places import norm_tokens  # noqa: E402

#: The tag that says what a place IS, checked in this order. A key maps to
#: None (every value) or to the values kept. Left out on purpose: highway
#: (streets are already in the graph), bus stops and stop positions (tens of
#: thousands of "Main St & 3rd"), waterways and landuse.
KINDS = [
    ("tourism", None), ("amenity", None), ("leisure", None), ("shop", None),
    ("historic", None), ("natural", None), ("boundary", {"protected_area", "national_park"}),
    ("aeroway", {"aerodrome", "terminal"}), ("railway", {"station"}),
    ("public_transport", {"station"}), ("office", None), ("craft", None),
    ("healthcare", None), ("place", None), ("building", None),
]
#: Named, but never a destination.
SKIP_VALUES = {"bench", "waste_basket", "bicycle_parking", "parking_space", "vending_machine",
               "post_box", "telephone", "recycling", "parking_entrance", "grit_bin"}
#: Other names a place is known by; searched, never shown.
ALT_KEYS = ("name:en", "alt_name", "official_name", "short_name", "brand")
#: Two features with the same name and kind this close are one place (a
#: museum drawn as a point AND as a building outline). ~300 m.
DEDUP_DEG = 0.003


def kind_of(props):
    for key, keep in KINDS:
        value = props.get(key)
        if value is None:
            continue
        if value in SKIP_VALUES or (keep is not None and value not in keep):
            return None
        if key == "building":
            return "building"
        if key == "boundary":
            return "protected area"
        return value.replace("_", " ") if value != "yes" else key
    return None


def centre(geometry):
    """A point for a geometry: the feature itself, or its vertices' mean."""
    t, c = geometry["type"], geometry["coordinates"]
    if t == "Point":
        return c[1], c[0]
    rings = {"LineString": [c], "Polygon": c[:1], "MultiPolygon": [p[0] for p in c],
             "MultiLineString": c}.get(t)
    if not rings:
        return None
    pts = [p for ring in rings for p in ring]
    return (sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts))


#: Place nodes that name the AREA a place is in, so "berkeley stadium" finds
#: "California Memorial Stadium": OSM names the landmark, a person names the
#: city too.
#:
#: a1, the town SHOWN beside a result, is chosen by `pick_town`: the place's own
#: addr:city, else the nearest addr:city within BORROW_M, else the nearest
#: city/town node. Measured 2026-09-28 on the graph-extent extract:
#:   - 40% of named PLACES (71,678 of the 181,136 features kind_of() keeps)
#:     carry addr:city. (Over ALL named OSM features, streets included, it is
#:     7% - 76,053 of 1,046,933 - which is the wrong population for this.)
#:   - Scored against those 71,678 known answers, the nearest town node alone
#:     was right 75.6% - it names San Jose places Campbell 2,327 times, San
#:     Francisco ones Daly City 940 - and borrowing the nearest labelled
#:     neighbour's town within 300 m first was right 98.3% (measured where
#:     labels cluster, so the gain on unlabelled places may be lower). 73.7% of
#:     unlabelled places have such a neighbour.
#: Not a village node for a1: Chabot Space and Science Center is nearer the
#: village node "Canyon" than Oakland's, and "museum · Canyon" is wrong for a
#: building whose address is Oakland. a2, searchable but not shown, is the
#: nearest neighbourhood or village.
TOWNS = {"city", "town"}
VILLAGES = {"village"}
HOODS = {"suburb", "neighbourhood", "quarter"}
TOWN_M, VILLAGE_M, HOOD_M = 15000.0, 4000.0, 1500.0
BORROW_M = 300.0
TOWN_CELL = 0.2       # degrees; ~22 km, so a 3x3 block covers TOWN_M at this latitude
BORROW_CELL = 0.005   # ~0.5 km, so a 3x3 block covers BORROW_M


def area_m2(geometry, lat):
    """Size of a polygon in square metres (0 for points and lines) - how big a
    place is, which says something about how likely it is to be the one meant."""
    t, c = geometry["type"], geometry["coordinates"]
    if t == "Polygon":
        rings = c[:1]
    elif t == "MultiPolygon":
        rings = [p[0] for p in c]
    else:
        return 0.0
    kx, ky = 111320.0 * math.cos(math.radians(lat)), 110574.0
    total = 0.0
    for ring in rings:
        s = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:]))
        total += abs(s) / 2 * kx * ky
    return total


def _metres(lat1, lon1, lat2, lon2):
    dy = (lat2 - lat1) * 110574.0
    dx = (lon2 - lon1) * 111320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


class _Nearest:
    """Nearest named place node of a set of kinds, by a coarse grid."""

    def __init__(self, cell):
        self.cell = cell
        self.cells = {}

    def add(self, name, lat, lon):
        self.cells.setdefault((int(lat // self.cell), int(lon // self.cell)), []).append((name, lat, lon))

    def find(self, lat, lon, within_m):
        ci, cj = int(lat // self.cell), int(lon // self.cell)
        best, best_d = None, within_m
        for i in (ci - 1, ci, ci + 1):
            for j in (cj - 1, cj, cj + 1):
                for name, plat, plon in self.cells.get((i, j), ()):
                    d = _metres(lat, lon, plat, plon)
                    if d < best_d:
                        best, best_d = name, d
        return best


def pick_town(own, lat, lon, labelled, towns):
    """The town shown beside a place: its own addr:city, else the nearest
    labelled place's within BORROW_M, else the nearest city/town node."""
    return own or labelled.find(lat, lon, BORROW_M) or towns.find(lat, lon, TOWN_M)


def features(pbf, workdir):
    """(name, kind, lat, lon, [alt names], area m2, notable, [areas]) per place.

    Two passes over the export: the first collects the town and neighbourhood
    nodes, the second places each named thing in them.
    """
    named = os.path.join(workdir, "named.osm.pbf")
    out = os.path.join(workdir, "named.geojsonseq")
    subprocess.run(["osmium", "tags-filter", "-O", "-o", named, pbf, "nwr/name"], check=True)
    subprocess.run(["osmium", "export", "-O", "-f", "geojsonseq", "-o", out, named], check=True)
    towns, villages, hoods = _Nearest(TOWN_CELL), _Nearest(TOWN_CELL), _Nearest(TOWN_CELL)
    labelled = _Nearest(BORROW_CELL)
    with open(out, encoding="utf-8") as f:
        for line in f:
            feat = json.loads(line.strip("\x1e"))
            props, geom = feat["properties"], feat["geometry"]
            if props.get("addr:city") and props.get("name") and kind_of(props):
                point = centre(geom)
                if point is not None:
                    labelled.add(props["addr:city"], point[0], point[1])
            if geom["type"] != "Point" or not props.get("name"):
                continue
            lon, lat = geom["coordinates"]
            place = props.get("place")
            for kinds, grid in ((TOWNS, towns), (VILLAGES, villages), (HOODS, hoods)):
                if place in kinds:
                    grid.add(props["name"], lat, lon)
    with open(out, encoding="utf-8") as f:
        for line in f:
            feat = json.loads(line.strip("\x1e"))
            props = feat["properties"]
            kind = kind_of(props)
            name = (props.get("name") or "").strip()
            if kind is None or not name:
                continue
            point = centre(feat["geometry"])
            if point is None:
                continue
            lat, lon = point
            alts = [props[k] for k in ALT_KEYS if props.get(k) and props[k] != name]
            city = pick_town(props.get("addr:city"), lat, lon, labelled, towns)
            local = hoods.find(lat, lon, HOOD_M) or villages.find(lat, lon, VILLAGE_M)
            areas = [a if a != name else None for a in (city, local)]
            notable = bool(props.get("wikidata") or props.get("wikipedia"))
            yield name, kind, lat, lon, alts, area_m2(feat["geometry"], lat), notable, areas


#: Schema 2: `tok` holds NAME words only, as in schema 1. Kinds and areas are
#: few and shared, so they are indexed once each (`kind_tok`, `area` +
#: `area_tok`) and a place points at its areas by id (a1 = town, a2 =
#: neighbourhood). A token row per place for every area and kind word doubled
#: the file (34.6 MB, measured); this keeps it near schema 1's size.
SCHEMA = 2


def write_db(rows, out, meta=None):
    """Write the list; returns the count kept.

    A row is (name, kind, lat, lon, alt names) or, from `features`, also
    (area m2, notable, [area names]). The same place drawn twice (a point and a
    building outline) is kept once, at the first position, with the LARGER
    size and either copy's notability - the outline is where the size is.
    """
    tmp = str(out) + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.executescript("""
        CREATE TABLE place (id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                            lat REAL NOT NULL, lon REAL NOT NULL, area_m2 REAL NOT NULL,
                            notable INTEGER NOT NULL, a1 INTEGER, a2 INTEGER);
        CREATE TABLE tok (t TEXT NOT NULL, id INTEGER NOT NULL);
        CREATE TABLE area (aid INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE area_tok (t TEXT NOT NULL, aid INTEGER NOT NULL);
        CREATE TABLE kind_tok (t TEXT NOT NULL, kind TEXT NOT NULL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    seen, area_ids, kinds = {}, {}, set()

    def aid(area):
        if area is None:
            return None
        if area not in area_ids:
            area_ids[area] = len(area_ids) + 1
            con.execute("INSERT INTO area VALUES (?, ?)", (area_ids[area], area))
            con.executemany("INSERT INTO area_tok VALUES (?, ?)",
                            [(w, area_ids[area]) for w in set(norm_tokens(area))])
        return area_ids[area]

    n = 0
    for row in rows:
        name, kind, lat, lon, alts = row[:5]
        size, notable, areas = (row[5], row[6], row[7]) if len(row) > 5 else (0.0, False, [])
        key = (" ".join(norm_tokens(name)), kind, round(lat / DEDUP_DEG), round(lon / DEDUP_DEG))
        if key in seen:
            pid = seen[key]
            con.execute("UPDATE place SET area_m2 = max(area_m2, ?), notable = max(notable, ?) WHERE id = ?",
                        (round(size), int(notable), pid))
            continue
        n += 1
        seen[key] = n
        town, hood = (list(areas) + [None, None])[:2]
        con.execute("INSERT INTO place VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (n, name, kind, round(lat, 6), round(lon, 6), round(size), int(notable),
                     aid(town), aid(hood)))
        if kind not in kinds:
            kinds.add(kind)
            con.executemany("INSERT INTO kind_tok VALUES (?, ?)", [(w, kind) for w in set(norm_tokens(kind))])
        words = set(norm_tokens(name))
        for alt in alts:
            words.update(norm_tokens(alt))
        con.executemany("INSERT INTO tok VALUES (?, ?)", [(w, n) for w in words])
    con.execute("CREATE INDEX tok_t ON tok (t)")
    con.execute("CREATE INDEX area_tok_t ON area_tok (t)")
    con.execute("CREATE INDEX kind_tok_t ON kind_tok (t)")
    info = dict(meta or {}, count=n, schema=SCHEMA,
                built_utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    con.executemany("INSERT INTO meta VALUES (?, ?)", [(k, str(v)) for k, v in info.items()])
    con.commit()
    con.execute("VACUUM")
    con.close()
    os.replace(tmp, out)
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pbf", required=True, help="the SAME OSM extract the road graph was built from")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    stamp = subprocess.run(["osmium", "fileinfo", "-e", "-g", "data.timestamp.last", a.pbf],
                           capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory() as work:
        n = write_db(features(a.pbf, work), a.out,
                     meta={"source": os.path.basename(a.pbf), "osm_timestamp": stamp})
    print(f"{a.out}: {n} places, {os.path.getsize(a.out):,} bytes, OSM data to {stamp}")


if __name__ == "__main__":
    main()
