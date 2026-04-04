#!/usr/bin/env python3
import json
import subprocess
import sys
import xml.etree.ElementTree as ET


def fail(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def parse_osm_pbf_with_osmium(input_pbf, route_type):
    try:
        proc = subprocess.Popen(
            ["osmium", "cat", input_pbf, "-f", "osm"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError:
        fail("Fehler: 'osmium' wurde nicht gefunden.")

    if proc.stdout is None or proc.stderr is None:
        fail("Fehler: Konnte osmium-Streams nicht öffnen.")

    nodes = {}
    ways = {}
    route_relations = []

    try:
        for _, elem in ET.iterparse(proc.stdout, events=("end",)):
            if elem.tag == "node":
                node_id = elem.get("id")
                lat = elem.get("lat")
                lon = elem.get("lon")

                if node_id and lat and lon:
                    tags = {tag.get("k"): tag.get("v") for tag in elem.findall("tag")}
                    nodes[node_id] = {
                        "id": node_id,
                        "lat": float(lat),
                        "lon": float(lon),
                        "tags": tags,
                    }

                elem.clear()

            elif elem.tag == "way":
                way_id = elem.get("id")
                if way_id:
                    nd_refs = [nd.get("ref") for nd in elem.findall("nd") if nd.get("ref")]
                    tags = {tag.get("k"): tag.get("v") for tag in elem.findall("tag")}
                    ways[way_id] = {
                        "id": way_id,
                        "nd_refs": nd_refs,
                        "tags": tags,
                    }

                elem.clear()

            elif elem.tag == "relation":
                rel_tags = {tag.get("k"): tag.get("v") for tag in elem.findall("tag")}
                if rel_tags.get("route") == route_type:
                    members = []
                    for member in elem.findall("member"):
                        members.append({
                            "type": member.get("type"),
                            "ref": member.get("ref"),
                            "role": member.get("role", ""),
                        })

                    route_relations.append({
                        "id": elem.get("id"),
                        "tags": rel_tags,
                        "members": members,
                    })

                elem.clear()

    except ET.ParseError as e:
        fail(f"XML Parse Error: {e}")

    stderr_text = proc.stderr.read()
    return_code = proc.wait()
    if return_code != 0:
        fail(f"Fehler bei 'osmium cat':\n{stderr_text}")

    return nodes, ways, route_relations


def coords_equal(a, b):
    return a[0] == b[0] and a[1] == b[1]


def reverse_if_needed(coords, previous_end):
    if not coords:
        return coords

    if previous_end is None:
        return coords

    first = coords[0]
    last = coords[-1]

    if coords_equal(first, previous_end):
        return coords

    if coords_equal(last, previous_end):
        return list(reversed(coords))

    return coords


def way_to_coords(way, nodes):
    coords = []
    for nd_ref in way.get("nd_refs", []):
        node = nodes.get(nd_ref)
        if not node:
            continue
        coords.append([node["lon"], node["lat"]])
    return coords


def dedupe_consecutive_coords(coords):
    if not coords:
        return coords

    out = [coords[0]]
    for c in coords[1:]:
        if not coords_equal(c, out[-1]):
            out.append(c)
    return out


def relation_geometry(rel, ways, nodes):
    parts = []
    current = []

    for member in rel["members"]:
        if member.get("type") != "way":
            continue

        way = ways.get(member.get("ref"))
        if not way:
            continue

        coords = way_to_coords(way, nodes)
        if len(coords) < 2:
            continue

        if not current:
            current = coords[:]
            continue

        previous_end = current[-1]
        oriented = reverse_if_needed(coords, previous_end)

        if coords_equal(oriented[0], previous_end):
            current.extend(oriented[1:])
        else:
            current = dedupe_consecutive_coords(current)
            if len(current) >= 2:
                parts.append(current)
            current = oriented[:]

    current = dedupe_consecutive_coords(current)
    if len(current) >= 2:
        parts.append(current)

    if not parts:
        return None

    if len(parts) == 1:
        return {
            "type": "LineString",
            "coordinates": parts[0]
        }

    return {
        "type": "MultiLineString",
        "coordinates": parts
    }


def main():
    args = sys.argv[1:]
    if len(args) < 3:
        fail(
            "Aufruf: python3 osm_routes_to_geojson.py "
            "input.osm.pbf output.geojson route_type [--include-night-buses]"
        )

    input_pbf = args[0]
    output_geojson = args[1]
    route_type = args[2].strip().lower()

    include_night_buses = False

    for arg in args[3:]:
        if arg == "--include-night-buses":
            include_night_buses = True
        else:
            fail(f"Unbekanntes Argument: {arg}")

    if route_type not in {"bus", "tram", "subway", "train"}:
        fail("route_type muss einer von: bus, tram, subway, train sein.")

    nodes, ways, route_relations = parse_osm_pbf_with_osmium(input_pbf, route_type)

    features = []
    skipped_night_buses = 0
    skipped_without_geometry = 0

    for rel in route_relations:
        rel_id = str(rel["id"])
        tags = rel["tags"]

        ref = (tags.get("ref") or "").strip().upper()
        if route_type == "bus" and not include_night_buses and ref.startswith("N"):
            skipped_night_buses += 1
            continue

        geometry = relation_geometry(rel, ways, nodes)
        if geometry is None:
            skipped_without_geometry += 1
            continue

        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": {
                "osm_id": rel_id,
                "route_type": route_type,
                "ref": tags.get("ref"),
                "name": tags.get("name"),
                "from": tags.get("from"),
                "to": tags.get("to"),
                "operator": tags.get("operator"),
                "network": tags.get("network"),
            }
        })

    geojson = {
        "type": "FeatureCollection",
        "features": features
    }

    with open(output_geojson, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False, indent=2)

    print(f"GeoJSON geschrieben: {output_geojson}")
    print(f"Typ: {route_type}")
    print(f"Route-Relationen gesamt: {len(route_relations)}")
    print(f"Features geschrieben: {len(features)}")
    print(f"Übersprungene Nachtbusse: {skipped_night_buses}")
    print(f"Ohne Geometrie übersprungen: {skipped_without_geometry}")


if __name__ == "__main__":
    main()