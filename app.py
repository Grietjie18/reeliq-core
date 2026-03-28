@app.get("/map", response_class=HTMLResponse)
async def get_map():
    # 🌊 Increased to 3 minutes so boats don't disappear between pings
    time_limit = datetime.utcnow() - timedelta(minutes=3)
    
    query = observations_table.select().where(
        observations_table.c.timestamp >= time_limit
    ).order_by(observations_table.c.vessel_id, observations_table.c.timestamp.asc())
    
    rows = await database.fetch_all(query)
    
    vessel_paths = {}
    for row in rows:
        v_id = row['vessel_id']
        if v_id not in vessel_paths:
            vessel_paths[v_id] = []
        vessel_paths[v_id].append([row['latitude'], row['longitude']])

    track_scripts = ""
    for v_id, coords in vessel_paths.items():
        if len(coords) > 1:
            track_scripts += f"L.polyline({coords}, {{color: '#00f2ff', weight: 2, opacity: 0.5}}).addTo(map);\n"
        
        last_coord = coords[-1]
        track_scripts += f"""
            L.circleMarker({last_coord}, {{
                radius: 7, fillColor: "#00f2ff", color: "#fff", weight: 2, fillOpacity: 1
            }}).addTo(map).bindPopup('<b>Vessel:</b> {v_id}');
        """

    html_content = f"""
    <html>
        <head>
            <title>REEL IQ | Live Monitor</title>
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <style>
                body {{ margin: 0; background: #06090f; }}
                #map {{ height: 100vh; width: 100%; }}
            </style>
        </head>
        <body>
            <div id="map"></div>
            <script>
                // Map setup
                var map = L.map('map', {{ zoomControl: false }}).setView([-34.14, 25.02], 11);
                L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png').addTo(map);

                // Add the boats
                {track_scripts}

                // ⚡️ REFRESH LOGIC (15 seconds)
                // We reload the page, but the browser usually caches the "view" 
                // if we don't force a reset.
                setTimeout(function(){{ 
                    location.reload(); 
                }}, 15000);
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)
