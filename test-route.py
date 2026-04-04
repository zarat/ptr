import json

with open('route-result.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

print("SUMMARY")
print(json.dumps(data.get("summary", {}), ensure_ascii=False, indent=2))

print("\nEDGES")
for i, e in enumerate(data.get("edges", []), 1):
    print(f"\n{i}. {e.get('type')}")
    print("  from:", e.get("from"), "|", e.get("from_stop_name"))
    print("  to:  ", e.get("to"), "|", e.get("to_stop_name"))
    print("  route_type:", e.get("route_type"))
    print("  ref:", e.get("ref"))
    print("  route_name:", e.get("route_name"))
    print("  route_from:", e.get("route_from"))
    print("  route_to:", e.get("route_to"))
    print("  distance_m:", e.get("distance_m"))
