@app.get("/", response_class=HTMLResponse)
async def map_dashboard():
    base = get_baseline()
    lats = [p['lat'] for p in base] + [o['latitude'] for o in fleet_status.values()]
    lons = [p['lon'] for p in base] + [o['longitude'] for o in fleet_status.values()]
    temps = [p['temp'] for p in base] + [o['temp_c'] for o in fleet_status.values()]

    interpolated_points = []
    if len(lats) > 3:
        # INCREASE RESOLUTION: 60x60 grid makes squares touch
        grid_lat, grid_lon = np.mgrid[-34.15:-33.95:60j, 24.85:25.15:60j]
        grid_z = griddata((lats, lons), temps, (grid_lat, grid_lon), method='linear')

        for i in range(len(grid_lat)):
            for j in range(len(grid_lon)):
                val = grid_z[i,j]
                lat, lon = grid_lat[i,j], grid_lon[i,j]
                
                # THE LAND FILTER: 
                # Very simple logic to keep data South/East of the J-Bay coastline
                if not np.isnan(val) and lon > 24.92: 
                    interpolated_points.append([float(lat), float(lon), float(val)])

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>REEL IQ | Live Analytics</title>
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
        <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
        <style>
            body {{ background: #0b1622; color: white; margin: 0; font-family: sans-serif; }}
            #map {{ height: 100vh; width: 100vw; }}
            .overlay {{ position: absolute; top: 10px; left: 50px; z-index: 1000; background: rgba(0,0,0,0.7); padding: 15px; border-radius: 8px; border: 1px solid #4dabf7; }}
        </style>
    </head>
    <body>
        <div class="overlay">
            <h2 style="margin:0; color:#4dabf7;">⚓ REEL IQ</h2>
            <small>Live Fusion: Satellite + 50 Fleet Nodes</small>
        </div>
        <div id="map"></div>

        <script>
            // PERSISTENT ZOOM: Store map state in session storage
            var lastCenter = JSON.parse(sessionStorage.getItem('mapCenter')) || [-34.05, 25.02];
            var lastZoom = sessionStorage.getItem('mapZoom') || 11;

            var map = L.map('map').setView(lastCenter, lastZoom);
            
            // Save state before refresh
            map.on('moveend', () => {{
                sessionStorage.setItem('mapCenter', JSON.stringify(map.getCenter()));
                sessionStorage.setItem('mapZoom', map.getZoom());
            }});

            L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png').addTo(map);

            var modelData = {json.dumps(interpolated_points)};
            
            modelData.forEach(p => {{
                var hue = 240 - ((p[2] - 16) * 15);
                // Larger rectangle size (0.004) ensures they overlap and "touch"
                L.rectangle([[p[0]-0.0025, p[1]-0.0025], [p[0]+0.0025, p[1]+0.0025]], {{
                    color: "hsl(" + hue + ", 100%, 50%)", 
                    weight: 0, 
                    fillOpacity: 0.5
                }}).addTo(map);
            }});

            // Refresh only the data, not the whole page zoom
            setTimeout(() => {{ location.reload(); }}, 15000);
        </script>
    </body>
    </html>
    """
