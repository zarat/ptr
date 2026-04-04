#!/usr/bin/env python3
import json
import math
import re
import sys
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


def build_spatial_index(nodes, cell_size_deg=0.002):
    grid = defaultdict(list)
    for node in nodes:
        gx = int(node["lon"] / cell_size_deg)
        gy = int(node["lat"] / cell_size_deg)
        grid[(gx, gy)].append(node)
    return grid


def maybe_similar_name(a, b):
    na = normalize_name(a)
    nb = normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True

    ta = set(na.split())
    tb = set(nb.split())
    if not ta or not tb:
        return False

    inter = len(ta & tb)
    union = len(ta | tb)
    score = inter / union
    return score >= 0.6


def generate_transfer_edges(nodes, same_name_radius_m, near_radius_m):
    grid = build_spatial_index(nodes)
    seen_pairs = set()
    edges = []

    # etwas größer als near_radius
    cell_size_deg = 0.002

    for node in nodes:
        gx = int(node["lon"] / cell_size_deg)
        gy = int(node["lat"] / cell_size_deg)

        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in grid.get((gx + dx, gy + dy), []):
                    if other["id"] == node["id"]:
                        continue

                    a_id, b_id = sorted((node["id"], other["id"]))
                    if (a_id, b_id) in seen_pairs:
                        continue
                    seen_pairs.add((a_id, b_id))

                    dist = haversine_m(node["lat"], node["lon"], other["lat"], other["lon"])

                    same_name = normalize_name(node.get("name")) == normalize_name(other.get("name"))
                    similar_name = maybe_similar_name(node.get("name"), other.get("name"))

                    allow = False
                    transfer_kind = None

                    if same_name and dist <= same_name_radius_m:
                        allow = True
                        transfer_kind = "same_name_near"

                    elif similar_name and dist <= near_radius_m:
                        allow = True
                        transfer_kind = "similar_name_near"

                    elif dist <= 35:
                        # sehr kurzer Fußweg als konservativer Fallback
                        allow = True
                        transfer_kind = "very_near"

                    if not allow:
                        continue

                    # keine Transfers zwischen praktisch identischen Stops derselben Relation direkt nebeneinander erzwingen
                    if (
                        node.get("relation_id") == other.get("relation_id")
                        and node.get("route_type") == other.get("route_type")
                    ):
                        continue

                    base = {
                        "type": "transfer",
                        "distance_m": round(dist, 1),
                        "weight": round(dist, 1),
                        "from_stop_name": node.get("name"),
                        "to_stop_name": other.get("name"),
                        "route_type": "transfer",
                        "transfer_kind": transfer_kind,
                    }

                    edges.append({
                        "id": f"transfer:{a_id}:{b_id}:ab",
                        "from": node["id"],
                        "to": other["id"],
                        **base,
                    })

                    edges.append({
                        "id": f"transfer:{a_id}:{b_id}:ba",
                        "from": other["id"],
                        "to": node["id"],
                        "type": "transfer",
                        "distance_m": round(dist, 1),
                        "weight": round(dist, 1),
                        "from_stop_name": other.get("name"),
                        "to_stop_name": node.get("name"),
                        "route_type": "transfer",
                        "transfer_kind": transfer_kind,
                    })

    return edges


def main():
    args = sys.argv[1:]
    if len(args) < 3:
        fail(
            "Aufruf: python3 merge_stop_graphs.py output.json input1.json input2.json ... "
            "[--same-name-transfer=120] [--near-transfer=35]"
        )

    same_name_radius_m = 120.0
    near_radius_m = 35.0
    clean_args = []

    for arg in args:
        if arg.startswith("--same-name-transfer="):
            same_name_radius_m = float(arg.split("=", 1)[1])
        elif arg.startswith("--near-transfer="):
            near_radius_m = float(arg.split("=", 1)[1])
        else:
            clean_args.append(arg)

    if len(clean_args) < 3:
        fail(
            "Aufruf: python3 merge_stop_graphs.py output.json input1.json input2.json ... "
            "[--same-name-transfer=120] [--near-transfer=35]"
        )

    output_path = clean_args[0]
    input_paths = clean_args[1:]

    merged = {
        "type": "TransitStopGraph",
        "graph_level": "stop",
        "nodes": [],
        "edges": [],
        "route_variants": [],
        "route_types": [],
        "same_name_transfer_radius_m": same_name_radius_m,
        "near_transfer_radius_m": near_radius_m,
    }

    seen_node_ids = set()
    seen_edge_ids = set()

    for path in input_paths:
        with open(path, "r", encoding="utf-8") as f:
            g = json.load(f)

        rt = g.get("route_type")
        if rt and rt not in merged["route_types"]:
            merged["route_types"].append(rt)

        for node in g.get("nodes", []):
            nid = node["id"]
            if nid in seen_node_ids:
                continue
            seen_node_ids.add(nid)
            merged["nodes"].append(node)

        for edge in g.get("edges", []):
            eid = edge["id"]
            if eid in seen_edge_ids:
                continue
            seen_edge_ids.add(eid)
            merged["edges"].append(edge)

        merged["route_variants"].extend(g.get("route_variants", []))

    transfer_edges = generate_transfer_edges(
        merged["nodes"],
        same_name_radius_m=same_name_radius_m,
        near_radius_m=near_radius_m,
    )

    for edge in transfer_edges:
        eid = edge["id"]
        if eid in seen_edge_ids:
            continue
        seen_edge_ids.add(eid)
        merged["edges"].append(edge)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    print(f"Merged Stop-Graph geschrieben: {output_path}")
    print(f"Route-Typen: {', '.join(merged['route_types'])}")
    print(f"Stop-Knoten: {len(merged['nodes'])}")
    print(f"Ride-Kanten: {sum(1 for e in merged['edges'] if e['type'] == 'ride')}")
    print(f"Transfer-Kanten: {sum(1 for e in merged['edges'] if e['type'] == 'transfer')}")
    print(f"Linienvarianten: {len(merged['route_variants'])}")
    print(f"Same-name-Radius: {same_name_radius_m} m")
    print(f"Near-Radius: {near_radius_m} m")


if __name__ == "__main__":
    main()
