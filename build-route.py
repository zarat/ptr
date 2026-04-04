#!/usr/bin/env python3
import heapq
import json
import math
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


def parse_coord(text):
    lon, lat = text.split(",")
    return float(lon), float(lat)


def nearest_nodes(nodes, lon, lat, max_distance_m=800, max_results=20):
    out = []
    for n in nodes:
        d = haversine_m(lat, lon, n["lat"], n["lon"])
        if d <= max_distance_m:
            out.append((d, n))
    out.sort(key=lambda x: x[0])
    return out[:max_results]


def build_adj(edges):
    adj = defaultdict(list)
    for e in edges:
        adj[e["from"]].append(e)
    return adj


def dijkstra(adj, start_id, end_id):
    pq = [(0.0, start_id)]
    dist = {start_id: 0.0}
    prev = {}
    prev_edge = {}

    while pq:
        cur_dist, u = heapq.heappop(pq)
        if u == end_id:
            break
        if cur_dist != dist.get(u):
            continue

        for e in adj.get(u, []):
            v = e["to"]
            nd = cur_dist + float(e.get("weight", 1))
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                prev_edge[v] = e
                heapq.heappush(pq, (nd, v))

    if end_id not in dist:
        return None

    path_edges = []
    cur = end_id
    while cur != start_id:
        e = prev_edge[cur]
        path_edges.append(e)
        cur = prev[cur]
    path_edges.reverse()

    return {
        "total_weight": dist[end_id],
        "edges": path_edges,
    }


def build_weighted_edges(edges):
    adjusted = []

    for e in edges:
        e = dict(e)
        base = float(e.get("distance_m", e.get("weight", 0)))

        if e["type"] == "ride":
            rt = e.get("route_type")
            if rt == "subway":
                e["weight"] = base * 0.22
            elif rt == "train":
                e["weight"] = base * 0.28
            elif rt == "tram":
                e["weight"] = base * 0.40
            else:
                e["weight"] = base * 0.60

        elif e["type"] == "transfer":
            tk = e.get("transfer_kind")
            if tk == "same_name_near":
                e["weight"] = 240 + base * 1.8
            elif tk == "similar_name_near":
                e["weight"] = 320 + base * 2.0
            elif tk == "very_near":
                e["weight"] = 120 + base * 1.5
            else:
                e["weight"] = 350 + base * 2.0

        adjusted.append(e)

    return adjusted


def summarize(path_edges, total_weight):
    return {
        "ride_m": round(sum(e.get("distance_m", 0) for e in path_edges if e["type"] == "ride"), 1),
        "transfer_m": round(sum(e.get("distance_m", 0) for e in path_edges if e["type"] == "transfer"), 1),
        "access_m": round(sum(e.get("distance_m", 0) for e in path_edges if e["type"] == "access"), 1),
        "egress_m": round(sum(e.get("distance_m", 0) for e in path_edges if e["type"] == "egress"), 1),
        "ride_edges": sum(1 for e in path_edges if e["type"] == "ride"),
        "transfer_edges": sum(1 for e in path_edges if e["type"] == "transfer"),
        "total_weight": round(total_weight, 1),
    }


def build_route_result(graph, start_lon, start_lat, end_lon, end_lat, max_stop_distance_m=800, max_nearest_stops=20):
    nodes = graph["nodes"]
    base_edges = build_weighted_edges(graph["edges"])

    start_candidates = nearest_nodes(nodes, start_lon, start_lat, max_stop_distance_m, max_nearest_stops)
    end_candidates = nearest_nodes(nodes, end_lon, end_lat, max_stop_distance_m, max_nearest_stops)

    if not start_candidates:
        return None, "Keine Start-Stopps im Suchradius gefunden."
    if not end_candidates:
        return None, "Keine Ziel-Stopps im Suchradius gefunden."

    edges = list(base_edges)

    for dist_m, node in start_candidates:
        edges.append({
            "id": f"access:{node['id']}",
            "type": "access",
            "from": "__start__",
            "to": node["id"],
            "distance_m": round(dist_m, 1),
            "weight": dist_m * 1.4,
            "to_stop_name": node.get("name"),
        })

    for dist_m, node in end_candidates:
        edges.append({
            "id": f"egress:{node['id']}",
            "type": "egress",
            "from": node["id"],
            "to": "__end__",
            "distance_m": round(dist_m, 1),
            "weight": dist_m * 1.4,
            "from_stop_name": node.get("name"),
        })

    adj = build_adj(edges)
    result = dijkstra(adj, "__start__", "__end__")
    if result is None:
        return None, "Keine Route gefunden."

    path_edges = result["edges"]

    return {
        "type": "TransitRouteResult",
        "graph_level": graph.get("graph_level"),
        "start": {"lon": start_lon, "lat": start_lat},
        "end": {"lon": end_lon, "lat": end_lat},
        "params": {
            "max_stop_distance_m": max_stop_distance_m,
            "max_nearest_stops": max_nearest_stops,
        },
        "start_candidates": [
            {
                "node_id": node["id"],
                "name": node.get("name"),
                "distance_m": round(dist_m, 1),
                "route_type": node.get("route_type"),
                "relation_id": node.get("relation_id"),
                "route_ref": node.get("route_ref"),
                "member_kind": node.get("member_kind"),
                "stop_kind": node.get("stop_kind"),
            }
            for dist_m, node in start_candidates
        ],
        "end_candidates": [
            {
                "node_id": node["id"],
                "name": node.get("name"),
                "distance_m": round(dist_m, 1),
                "route_type": node.get("route_type"),
                "relation_id": node.get("relation_id"),
                "route_ref": node.get("route_ref"),
                "member_kind": node.get("member_kind"),
                "stop_kind": node.get("stop_kind"),
            }
            for dist_m, node in end_candidates
        ],
        "summary": summarize(path_edges, result["total_weight"]),
        "edges": path_edges,
    }, None


def main():
    args = sys.argv[1:]
    if len(args) not in {4, 6}:
        fail(
            "Aufruf: python3 route_stop_transit.py "
            "merged-stop-graph.json start_lon,start_lat end_lon,end_lat output.json "
            "[max_stop_distance_m max_nearest_stops]"
        )

    graph_path = args[0]
    start_lon, start_lat = parse_coord(args[1])
    end_lon, end_lat = parse_coord(args[2])
    output_path = args[3]

    max_stop_distance_m = float(args[4]) if len(args) >= 5 else 800.0
    max_nearest_stops = int(args[5]) if len(args) >= 6 else 20

    with open(graph_path, "r", encoding="utf-8") as f:
        graph = json.load(f)

    result, error = build_route_result(
        graph,
        start_lon,
        start_lat,
        end_lon,
        end_lat,
        max_stop_distance_m=max_stop_distance_m,
        max_nearest_stops=max_nearest_stops,
    )

    if error:
        fail(error)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Route geschrieben: {output_path}")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
