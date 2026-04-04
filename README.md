# ptr

For the map, put your pmtiles in /assets/pmtiles.

# Extract OSM data
<pre>
osmium tags-filter wien.osm.pbf r/route=bus -o bus-routes-only.osm.pbf -O
osmium cat bus-routes-only.osm.pbf -f opl | awk -F' ' '$1 ~ /^r/ {print $1}' > bus-route-ids.txt
osmium getid -r wien.osm.pbf $(cat bus-route-ids.txt) -o bus-complete.osm.pbf -O

osmium tags-filter wien.osm.pbf r/route=tram -o tram-routes-only.osm.pbf -O
osmium cat tram-routes-only.osm.pbf -f opl | awk -F' ' '$1 ~ /^r/ {print $1}' > tram-route-ids.txt
osmium getid -r wien.osm.pbf $(cat tram-route-ids.txt) -o tram-complete.osm.pbf -O

osmium tags-filter wien.osm.pbf r/route=subway -o subway-routes-only.osm.pbf -O
osmium cat subway-routes-only.osm.pbf -f opl | awk -F' ' '$1 ~ /^r/ {print $1}' > subway-route-ids.txt
osmium getid -r wien.osm.pbf $(cat subway-route-ids.txt) -o subway-complete.osm.pbf -O

osmium tags-filter wien.osm.pbf r/route=train -o train-routes-only.osm.pbf -O
osmium cat train-routes-only.osm.pbf -f opl | awk -F' ' '$1 ~ /^r/ {print $1}' > train-route-ids.txt
osmium getid -r wien.osm.pbf $(cat train-route-ids.txt) -o train-complete.osm.pbf -O
</pre>

# Build route geometry
<pre>
python build_geojson.py bus-complete.osm.pbf bus-routes.geojson bus
python build_geojson.py tram-complete.osm.pbf tram-routes.geojson tram
python build_geojson.py subway-complete.osm.pbf subway-routes.geojson subway
python build_geojson.py train-complete.osm.pbf train-routes.geojson train
</pre>

# Build route graph
<pre>
python build_graph.py bus-complete.osm.pbf bus-stop-graph.json bus
python build_graph.py tram-complete.osm.pbf tram-stop-graph.json tram
python build_graph.py subway-complete.osm.pbf subway-stop-graph.json subway
python build_graph.py train-complete.osm.pbf train-stop-graph.json train
</pre>

# Merge route graphs
<pre>
python merge_graphs.py merged-stop-graph.json bus-stop-graph.json tram-stop-graph.json subway-stop-graph.json train-stop-graph.json --same-name-transfer=120 --near-transfer=35
</pre>

# Build a route and test it
<pre>
python build-route.py merged-stop-graph.json 16.3725,48.2089 16.3400,48.1850 route-result.json
python test-route.py
</pre>
