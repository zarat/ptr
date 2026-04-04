#!/usr/bin/env python3
import json
import math
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict


def fail(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def normalize_name(name):
    name = (name or "").strip().lower()
    name = name.replace("wien ", "")
    name = name.replace("/", " ")
    name = name.replace("-", " ")
    name = re.sub(r"[()\,\.]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def display_name(tags, fallback_id):
    return (
        tags.get("name")
        or tags.get("local_ref")
        or tags.get("ref")
        or f"stop-{fallback_id}"
    )


def is_station_only(tags):
    public_transport = tags.get("public_transport")
    railway = tags.get("railway")
    station = tags.get("station")

    if public_transport == "station":
        return True
    if railway == "station":
        return True
    if station == "subway":
        return True
    return False


def detect_stop_kind(tags, route_type):
    public_transport = tags.get("public_transport")
    highway = tags.get("highway")
    railway = tags.get("railway")

    if public_transport == "stop_position":
        return "stop_position"

    if public_transport == "platform":
        return "platform"

    if route_type == "bus" and highway == "bus_stop":
        return "bus_stop"

    if route_type == "tram" and railway == "tram_stop":
        return "tram_stop"

    return None


def classify_member(member, node, route_type):
    role = (member.get("role") or "").strip().lower()
    tags = node["tags"]

    if is_station_only(tags):
        return None

    stop_kind = detect_stop_kind(tags, route_type)

    # echte Stop-Rollen bevorzugen
    if role in {"stop", "stop_entry_only", "stop_exit_only"}:
        return {
            "member_kind": "stop",
            "stop_kind": stop_kind or "unknown_stop"
        }

    if stop_kind == "stop_position":
        return {
            "member_kind": "stop",
            "stop_kind": "stop_position"
        }

    if role in {"platform", "platform_entry_only", "platform_exit_only"} and stop_kind:
        return {
            "member_kind": "platform",
            "stop_kind": stop_kind
        }

    if stop_kind in {"platform", "bus_stop", "tram_stop"}:
        return {
            "member_kind": "platform",
            "stop_kind": stop_kind
        }

    return None


def extract_relation_stops(rel, nodes, ways, route_type):
    raw = []

    for idx, member in enumerate(rel["members"]):
        member_type = member.get("type")
        member_ref = member.get("ref")

        tags = None
        lat = None
        lon = None
        osm_node_id = None
        osm_way_id = None

        if member_type == "node":
            node = nodes.get(member_ref)
            if not node:
                continue
            tags = node["tags"]
            lat = node["lat"]
            lon = node["lon"]
            osm_node_id = member_ref

        elif member_type == "way":
            way = ways.get(member_ref)
            if not way:
                continue
            tags = way["tags"]
            center = way_center(way, nodes)
            if not center:
                continue
            lat, lon = center
            osm_way_id = member_ref

        else:
            continue

        fake_node = {"tags": tags}
        info = classify_member(member, fake_node, route_type)
        if info is None:
            continue

        name = display_name(tags, member_ref)
        normalized_name = normalize_name(name)
        if not normalized_name:
            continue

        raw.append({
            "member_type": member_type,
            "node_id": member_ref if member_type == "node" else None,
            "way_id": member_ref if member_type == "way" else None,
            "name": name,
            "normalized_name": normalized_name,
            "lat": lat,
            "lon": lon,
            "member_index": idx,
            "member_role": (member.get("role") or "").strip(),
            "member_kind": info["member_kind"],
            "stop_kind": info["stop_kind"],
            "tags": tags,
            "osm_node_id": osm_node_id,
            "osm_way_id": osm_way_id,
        })

    if not raw:
        return []

    grouped = []
    current_group = [raw[0]]

    for item in raw[1:]:
        prev = current_group[-1]
        same_name = item["normalized_name"] == prev["normalized_name"]
        close = haversine_m(prev["lat"], prev["lon"], item["lat"], item["lon"]) <= 80

        if same_name and close:
            current_group.append(item)
        else:
            grouped.append(current_group)
            current_group = [item]

    grouped.append(current_group)

    cleaned = []
    for group in grouped:
        stop_candidates = [g for g in group if g["member_kind"] == "stop"]
        chosen = stop_candidates[0] if stop_candidates else group[0]
        cleaned.append(chosen)

    final = []
    for item in cleaned:
        if not final:
            final.append(item)
            continue

        prev = final[-1]
        same_name = item["normalized_name"] == prev["normalized_name"]
        close = haversine_m(prev["lat"], prev["lon"], item["lat"], item["lon"]) <= 30

        if same_name and close:
            continue

        final.append(item)

    return final


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


def way_center(way, nodes):
    coords = []
    for nd_ref in way.get("nd_refs", []):
        node = nodes.get(nd_ref)
        if not node:
            continue
        coords.append((node["lat"], node["lon"]))

    if not coords:
        return None

    lat = sum(c[0] for c in coords) / len(coords)
    lon = sum(c[1] for c in coords) / len(coords)
    return lat, lon
    

def main():
    args = sys.argv[1:]
    if len(args) < 3:
        fail(
            "Aufruf: python3 osm_route_relations_to_stop_graph.py "
            "input.osm.pbf graph.json route_type [--include-night-buses]"
        )

    input_pbf = args[0]
    graph_json_path = args[1]
    route_type = args[2].strip().lower()

    include_night_buses = False

    for arg in args[3:]:
        if arg == "--include-night-buses":
            include_night_buses = True
        else:
            fail(f"Unbekanntes Argument: {arg}")

    if route_type not in {"bus", "tram", "subway", "train"}:
        fail("route_type muss einer von: bus, tram, subway, train sein.")

    #nodes, route_relations = parse_osm_pbf_with_osmium(input_pbf, route_type)
    nodes, ways, route_relations = parse_osm_pbf_with_osmium(input_pbf, route_type)

    stop_nodes = []
    ride_edges = []
    route_variants = []

    seen_stop_ids = set()
    skipped_night_buses = 0

    for rel in route_relations:
        rel_id = str(rel["id"])
        rel_tags = rel["tags"]

        ref = (rel_tags.get("ref") or "").strip().upper()
        if route_type == "bus" and not include_night_buses and ref.startswith("N"):
            skipped_night_buses += 1
            continue

        #stop_seq = extract_relation_stops(rel, nodes, route_type)
        stop_seq = extract_relation_stops(rel, nodes, ways, route_type)
        if len(stop_seq) < 2:
            continue

        ordered_stop_ids = []

        for item in stop_seq:
            #stop_id = f"stop:{route_type}:{rel_id}:{item['member_index']}"
            member_type = item.get("member_type", "node")
            stop_id = f"stop:{route_type}:{rel_id}:{member_type}:{item['member_index']}"
            ordered_stop_ids.append(stop_id)

            if stop_id not in seen_stop_ids:
                seen_stop_ids.add(stop_id)
                stop_nodes.append({
                    "id": stop_id,
                    "type": "stop",
                    "name": item["name"],
                    "normalized_name": item["normalized_name"],
                    "lat": item["lat"],
                    "lon": item["lon"],
                    "route_type": route_type,
                    "route_ref": rel_tags.get("ref"),
                    "relation_id": rel_id,
                    "member_index": item["member_index"],
                    "member_type": item.get("member_type"),
                    "member_role": item["member_role"],
                    "member_kind": item["member_kind"],
                    "stop_kind": item["stop_kind"],
                    "osm_node_id": item["osm_node_id"],
                    "osm_way_id": item["osm_way_id"],
                    "display_group": item["normalized_name"],
                })

        route_meta = {
            "relation_id": rel_id,
            "ref": rel_tags.get("ref"),
            "route_name": rel_tags.get("name"),
            "route_from": rel_tags.get("from"),
            "route_to": rel_tags.get("to"),
            "operator": rel_tags.get("operator"),
            "network": rel_tags.get("network"),
            "route_type": route_type,
        }

        for i in range(len(stop_seq) - 1):
            a_item = stop_seq[i]
            b_item = stop_seq[i + 1]

            #a = f"stop:{route_type}:{rel_id}:{a_item['member_index']}"
            #b = f"stop:{route_type}:{rel_id}:{b_item['member_index']}"
            a = f"stop:{route_type}:{rel_id}:{a_item.get('member_type', 'node')}:{a_item['member_index']}"
            b = f"stop:{route_type}:{rel_id}:{b_item.get('member_type', 'node')}:{b_item['member_index']}"

            if a == b:
                continue

            dist = haversine_m(a_item["lat"], a_item["lon"], b_item["lat"], b_item["lon"])

            ride_edges.append({
                "id": f"ride:{rel_id}:{a_item['member_index']}:{b_item['member_index']}",
                "type": "ride",
                "from": a,
                "to": b,
                "distance_m": round(dist, 1),
                "weight": round(dist, 1),
                "from_stop_name": a_item["name"],
                "to_stop_name": b_item["name"],
                **route_meta,
            })

            

        route_variants.append({
            "relation_id": rel_id,
            "ref": rel_tags.get("ref"),
            "route_name": rel_tags.get("name"),
            "route_from": rel_tags.get("from"),
            "route_to": rel_tags.get("to"),
            "route_type": route_type,
            "ordered_stop_ids": ordered_stop_ids,
            "ordered_stop_names": [s["name"] for s in stop_seq],
            "ordered_stop_kinds": [s["stop_kind"] for s in stop_seq],
        })

    graph = {
        "type": "TransitStopGraph",
        "graph_level": "stop",
        "route_type": route_type,
        "nodes": stop_nodes,
        "edges": ride_edges,
        "route_variants": route_variants,
        "include_night_buses": include_night_buses,
    }

    with open(graph_json_path, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)

    print(f"Graph geschrieben: {graph_json_path}")
    print(f"Typ: {route_type}")
    print(f"Route-Relationen: {len(route_relations)}")
    print(f"Übersprungene Nachtbusse: {skipped_night_buses}")
    print(f"Nachtbusse eingeschlossen: {include_night_buses}")
    print(f"Stop-Knoten: {len(stop_nodes)}")
    print(f"Ride-Kanten: {len(ride_edges)}")
    print(f"Linienvarianten: {len(route_variants)}")


if __name__ == "__main__":
    main()
