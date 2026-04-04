#
# pip install flask flask_cors requests
#
from flask import Flask, request, jsonify
from flask_cors import CORS
import json
import math
import heapq
import os
import re
from collections import defaultdict

try:
    import requests
except ImportError:
    requests = None


app = Flask(__name__)
CORS(app)

# =========================
# KONFIGURATION
# =========================

GRAPH_PATH = os.environ.get("PT_GRAPH_PATH", "merged-stop-graph.json")

USE_OSRM_WALKING = os.environ.get("PT_USE_OSRM", "0").strip().lower() in {
    "1", "true", "yes", "on"
}

OSRM_WALK_BASE_URL = os.environ.get(
    "PT_OSRM_WALK_URL",
    "http://192.168.0.122:5000/route/v1/foot"
).strip()

OSRM_TIMEOUT_SECONDS = float(os.environ.get("PT_OSRM_TIMEOUT", "6"))

DEFAULT_MAX_CANDIDATE_DISTANCE_M = float(os.environ.get("PT_MAX_STOP_DISTANCE", "800"))
DEFAULT_MAX_CANDIDATES = int(os.environ.get("PT_MAX_CANDIDATES", "20"))

FALLBACK_TO_STRAIGHT_LINES = True

ROUTE_GEOJSON_PATHS = {
    "bus": os.environ.get("PT_BUS_ROUTES_GEOJSON", "bus-routes.geojson"),
    "tram": os.environ.get("PT_TRAM_ROUTES_GEOJSON", "tram-routes.geojson"),
    "subway": os.environ.get("PT_SUBWAY_ROUTES_GEOJSON", "subway-routes.geojson"),
    "train": os.environ.get("PT_TRAIN_ROUTES_GEOJSON", "train-routes.geojson"),
}


# =========================
# LADEN
# =========================

with open(GRAPH_PATH, "r", encoding="utf-8") as f:
    GRAPH = json.load(f)

NODES = GRAPH["nodes"]
EDGES = GRAPH["edges"]
NODE_BY_ID = {n["id"]: n for n in NODES}


def load_route_geojson_index():
    index = {}

    for route_type, path in ROUTE_GEOJSON_PATHS.items():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            index[route_type] = {}
            continue

        rel_map = {}
        for feature in data.get("features", []):
            props = feature.get("properties", {}) or {}
            rel_id = str(props.get("osm_id", "")).strip()
            if rel_id:
                rel_map[rel_id] = feature

        index[route_type] = rel_map

    return index


ROUTE_GEOMETRY_INDEX = load_route_geojson_index()


# =========================
# HILFSFUNKTIONEN
# =========================

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


def parse_lonlat(text):
    parts = text.split(",")
    if len(parts) != 2:
        raise ValueError("expected lon,lat")
    return float(parts[0].strip()), float(parts[1].strip())


def nearest_nodes(lon, lat, max_distance_m=800, max_results=20):
    out = []
    for n in NODES:
        d = haversine_m(lat, lon, n["lat"], n["lon"])
        if d <= max_distance_m:
            out.append((d, n))
    out.sort(key=lambda x: x[0])
    return out[:max_results]


def build_weighted_edges():
    adjusted = []

    for e in EDGES:
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


def build_adjacency(edges):
    adj = defaultdict(list)
    for e in edges:
        adj[e["from"]].append(e)
    return adj


BASE_EDGES = build_weighted_edges()
BASE_ADJ = build_adjacency(BASE_EDGES)


def dijkstra_with_extra_edges(base_adj, extra_edges, start_id="__start__", end_id="__end__"):
    adj = defaultdict(list)
    for k, v in base_adj.items():
        adj[k].extend(v)
    for e in extra_edges:
        adj[e["from"]].append(e)

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

        for edge in adj.get(u, []):
            v = edge["to"]
            w = float(edge.get("weight", 1.0))
            nd = cur_dist + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                prev_edge[v] = edge
                heapq.heappush(pq, (nd, v))

    if end_id not in dist:
        return None

    path_edges = []
    cur = end_id
    while cur != start_id:
        edge = prev_edge[cur]
        path_edges.append(edge)
        cur = prev[cur]
    path_edges.reverse()

    return {
        "total_weight": dist[end_id],
        "edges": path_edges
    }


def straight_line_geometry(start_lon, start_lat, end_lon, end_lat):
    return {
        "type": "LineString",
        "coordinates": [
            [start_lon, start_lat],
            [end_lon, end_lat]
        ]
    }


def osrm_walk_geometry(start_lon, start_lat, end_lon, end_lat):
    if not USE_OSRM_WALKING:
        raise RuntimeError("OSRM walking disabled")
    if requests is None:
        raise RuntimeError("requests not installed")

    url = (
        f"{OSRM_WALK_BASE_URL}/"
        f"{start_lon},{start_lat};{end_lon},{end_lat}"
        f"?overview=full&geometries=geojson"
    )

    r = requests.get(url, timeout=OSRM_TIMEOUT_SECONDS)
    r.raise_for_status()
    data = r.json()
    routes = data.get("routes") or []
    if not routes:
        raise RuntimeError("OSRM returned no route")
    return routes[0]["geometry"]


def walking_geometry(start_lon, start_lat, end_lon, end_lat):
    if USE_OSRM_WALKING:
        try:
            return osrm_walk_geometry(start_lon, start_lat, end_lon, end_lat)
        except Exception:
            if not FALLBACK_TO_STRAIGHT_LINES:
                raise
    return straight_line_geometry(start_lon, start_lat, end_lon, end_lat)


def flatten_linestringish(geometry):
    if not geometry:
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if gtype == "LineString":
        return coords[:]
    if gtype == "MultiLineString":
        out = []
        for part in coords:
            out.extend(part)
        return out
    return []


def geometry_parts(geometry):
    if not geometry:
        return []

    gtype = geometry.get("type")
    coords = geometry.get("coordinates", [])

    if gtype == "LineString":
        return [coords] if len(coords) >= 2 else []

    if gtype == "MultiLineString":
        return [part for part in coords if len(part) >= 2]

    return []


def coord_distance_to_point(coord, lon, lat):
    return haversine_m(lat, lon, coord[1], coord[0])


def part_score(part, start_lon, start_lat, end_lon, end_lat):
    if len(part) < 2:
        return float("inf")

    start_dists = [coord_distance_to_point(c, start_lon, start_lat) for c in part]
    end_dists = [coord_distance_to_point(c, end_lon, end_lat) for c in part]

    best_start = min(start_dists)
    best_end = min(end_dists)

    return best_start + best_end

def nearest_coord_index(coords, lon, lat):
    best_idx = None
    best_dist = None
    for i, c in enumerate(coords):
        d = haversine_m(lat, lon, c[1], c[0])
        if best_dist is None or d < best_dist:
            best_dist = d
            best_idx = i
    return best_idx


def clip_coords_between_points(coords, start_lon, start_lat, end_lon, end_lat):
    if len(coords) < 2:
        return coords

    i1 = nearest_coord_index(coords, start_lon, start_lat)
    i2 = nearest_coord_index(coords, end_lon, end_lat)

    if i1 is None or i2 is None:
        return coords

    if i1 <= i2:
        clipped = coords[i1:i2 + 1]
    else:
        clipped = list(reversed(coords[i2:i1 + 1]))

    if len(clipped) < 2:
        return coords

    return clipped


def ride_geometry_from_route_geojson(edge):
    route_type = edge.get("route_type")
    relation_id = str(edge.get("relation_id", "")).strip()

    if not route_type or not relation_id:
        return None

    feature = ROUTE_GEOMETRY_INDEX.get(route_type, {}).get(relation_id)
    if not feature:
        return None

    geometry = feature.get("geometry")
    coords = flatten_linestringish(geometry)
    if len(coords) < 2:
        return None

    from_node = NODE_BY_ID.get(edge["from"])
    to_node = NODE_BY_ID.get(edge["to"])
    if not from_node or not to_node:
        return None

    clipped = clip_coords_between_points(
        coords,
        from_node["lon"], from_node["lat"],
        to_node["lon"], to_node["lat"]
    )

    if len(clipped) < 2:
        return None

    return {
        "type": "LineString",
        "coordinates": clipped
    }


def make_access_edge(distance_m, node):
    return {
        "id": f"access:{node['id']}",
        "type": "access",
        "from": "__start__",
        "to": node["id"],
        "distance_m": round(distance_m, 1),
        "weight": distance_m * 1.4,
        "to_stop_name": node.get("name"),
    }


def make_egress_edge(distance_m, node):
    return {
        "id": f"egress:{node['id']}",
        "type": "egress",
        "from": node["id"],
        "to": "__end__",
        "distance_m": round(distance_m, 1),
        "weight": distance_m * 1.4,
        "from_stop_name": node.get("name"),
    }


def edge_to_feature(edge, index, start_lon, start_lat, end_lon, end_lat):
    etype = edge["type"]

    if etype == "ride":
        geom = ride_geometry_from_route_geojson(edge)
        if geom is None:
            a = NODE_BY_ID[edge["from"]]
            b = NODE_BY_ID[edge["to"]]
            geom = straight_line_geometry(a["lon"], a["lat"], b["lon"], b["lat"])

        return {
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "index": index,
                "leg_type": "ride",
                "route_type": edge.get("route_type"),
                "ref": edge.get("ref"),
                "route_name": edge.get("route_name"),
                "route_from": edge.get("route_from"),
                "route_to": edge.get("route_to"),
                "from_stop_name": edge.get("from_stop_name"),
                "to_stop_name": edge.get("to_stop_name"),
                "distance_m": edge.get("distance_m"),
                "relation_id": edge.get("relation_id"),
                "from_node_id": edge.get("from"),
                "to_node_id": edge.get("to"),
            }
        }

    if etype == "access":
        b = NODE_BY_ID[edge["to"]]
        geom = walking_geometry(start_lon, start_lat, b["lon"], b["lat"])
        return {
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "index": index,
                "leg_type": "access",
                "distance_m": edge.get("distance_m"),
                "to_stop_name": edge.get("to_stop_name"),
            }
        }

    if etype == "egress":
        a = NODE_BY_ID[edge["from"]]
        geom = walking_geometry(a["lon"], a["lat"], end_lon, end_lat)
        return {
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "index": index,
                "leg_type": "egress",
                "distance_m": edge.get("distance_m"),
                "from_stop_name": edge.get("from_stop_name"),
            }
        }

    if etype == "transfer":
        a = NODE_BY_ID[edge["from"]]
        b = NODE_BY_ID[edge["to"]]
        geom = walking_geometry(a["lon"], a["lat"], b["lon"], b["lat"])
        return {
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "index": index,
                "leg_type": "transfer",
                "distance_m": edge.get("distance_m"),
                "from_stop_name": edge.get("from_stop_name"),
                "to_stop_name": edge.get("to_stop_name"),
                "transfer_kind": edge.get("transfer_kind"),
            }
        }

    return None


def build_point_features(path_edges):
    used_node_ids = set()
    for edge in path_edges:
        for key in ("from", "to"):
            nid = edge.get(key)
            if nid in NODE_BY_ID:
                used_node_ids.add(nid)

    features = []
    for nid in sorted(used_node_ids):
        n = NODE_BY_ID[nid]
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [n["lon"], n["lat"]]
            },
            "properties": {
                "node_id": nid,
                "name": n.get("name"),
                "route_type": n.get("route_type"),
                "route_ref": n.get("route_ref"),
                "relation_id": n.get("relation_id"),
                "member_kind": n.get("member_kind"),
                "stop_kind": n.get("stop_kind"),
            }
        })
    return features


def build_summary(path_edges, total_weight):
    return {
        "ride_m": round(sum(e.get("distance_m", 0) for e in path_edges if e["type"] == "ride"), 1),
        "transfer_m": round(sum(e.get("distance_m", 0) for e in path_edges if e["type"] == "transfer"), 1),
        "access_m": round(sum(e.get("distance_m", 0) for e in path_edges if e["type"] == "access"), 1),
        "egress_m": round(sum(e.get("distance_m", 0) for e in path_edges if e["type"] == "egress"), 1),
        "ride_edges": sum(1 for e in path_edges if e["type"] == "ride"),
        "transfer_edges": sum(1 for e in path_edges if e["type"] == "transfer"),
        "total_weight": round(total_weight, 1),
    }


@app.route("/pt-route")
def pt_route():
    start = request.args.get("start")
    end = request.args.get("end")

    if not start or not end:
        return jsonify({"error": "missing start or end"}), 400

    try:
        start_lon, start_lat = parse_lonlat(start)
        end_lon, end_lat = parse_lonlat(end)
    except Exception:
        return jsonify({"error": "invalid start or end format, expected lon,lat"}), 400

    max_distance_m = float(request.args.get("max_distance_m", DEFAULT_MAX_CANDIDATE_DISTANCE_M))
    max_candidates = int(request.args.get("max_candidates", DEFAULT_MAX_CANDIDATES))

    start_candidates = nearest_nodes(start_lon, start_lat, max_distance_m, max_candidates)
    end_candidates = nearest_nodes(end_lon, end_lat, max_distance_m, max_candidates)

    if not start_candidates:
        return jsonify({"error": "Keine Start-Stopps im Suchradius gefunden."}), 404
    if not end_candidates:
        return jsonify({"error": "Keine Ziel-Stopps im Suchradius gefunden."}), 404

    extra_edges = []
    for dist_m, node in start_candidates:
        extra_edges.append(make_access_edge(dist_m, node))
    for dist_m, node in end_candidates:
        extra_edges.append(make_egress_edge(dist_m, node))

    result = dijkstra_with_extra_edges(BASE_ADJ, extra_edges)
    if result is None:
        return jsonify({"error": "Keine Route gefunden."}), 404

    path_edges = result["edges"]

    line_features = []
    for idx, edge in enumerate(path_edges):
        feat = edge_to_feature(edge, idx, start_lon, start_lat, end_lon, end_lat)
        if feat:
            line_features.append(feat)

    point_features = build_point_features(path_edges)
    summary = build_summary(path_edges, result["total_weight"])

    return jsonify({
        "type": "PtRouteResponse",
        "graph_level": GRAPH.get("graph_level"),
        "config": {
            "graph_path": GRAPH_PATH,
            "use_osrm_walking": USE_OSRM_WALKING,
            "osrm_walk_base_url": OSRM_WALK_BASE_URL,
            "fallback_to_straight_lines": FALLBACK_TO_STRAIGHT_LINES,
        },
        "start_candidates": [
            {
                "node_id": node["id"],
                "name": node.get("name"),
                "distance_m": round(dist_m, 1),
                "route_type": node.get("route_type"),
                "route_ref": node.get("route_ref"),
                "relation_id": node.get("relation_id"),
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
                "route_ref": node.get("route_ref"),
                "relation_id": node.get("relation_id"),
                "member_kind": node.get("member_kind"),
                "stop_kind": node.get("stop_kind"),
            }
            for dist_m, node in end_candidates
        ],
        "edges": path_edges,
        "summary": summary,
        "route_line": {
            "type": "FeatureCollection",
            "features": line_features,
        },
        "route_points": {
            "type": "FeatureCollection",
            "features": point_features,
        }
    })


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "graph_path": GRAPH_PATH,
        "nodes": len(NODES),
        "edges": len(EDGES),
        "use_osrm_walking": USE_OSRM_WALKING,
        "osrm_walk_base_url": OSRM_WALK_BASE_URL,
        "route_geojson_paths": ROUTE_GEOJSON_PATHS,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8010, debug=True)
