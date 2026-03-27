# ... (Keep existing imports and Observation schema) ...
import numpy as np # You might need to run: pip install numpy

@app.get("/", response_class=HTMLResponse)
async def map_dashboard():
    # 1. Extract the "Real" Data Points
    points = []
    for obs in fleet_status.values():
        points.append({
            "lat": obs['latitude'], 
            "lon": obs['longitude'], 
            "temp": obs['temp_c']
        })
    
    points_json = json.dumps(points)

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>REEL IQ | Ocean Intelligence</title>
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
        <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
        <script src="https://leaflet.github.io/Leaflet.heat/dist/leaflet-heat.js"></script>
        <style>
            #map {{ height: 90vh; width: 100%; border-radius: 10px; }}
            body {{ font-family: sans-serif; background: #0b1622; color: white; padding: 20px; }}
            .stats {{ background: rgba(0,0,0,0.8); padding: 15px; border-radius: 8px; margin-bottom: 10px; }}
        </style>
        <meta http-equiv="refresh" content="10">
    </head>
    <body>
        <div class="stats">
            <h2 style="margin:0; color: #4dabf7;">⚓ REEL IQ <span style="font-weight: 300; color: #fff;">| Spatial Model v1.0</span></h2>
            <small>Analyzing 50 nodes in Jeffreys Bay Sector</small>
        </div>
        
        <div id="map"></div>
        
        <script>
            var map = L.map('map').setView([-34.05, 25.02], 11);
            L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png').addTo(map);

            var points = {points_json};

            // THE MODEL: We create a high-density heatmap that acts as a surface
            var heatData = points.map(p => [
                p.lat, 
                p.lon, 
                (p.temp - 15) / 10 // Normalizing intensity based on temp
            ]);

            var heat = L.heatLayer(heatData, {{
                radius: 80, // High radius 'fills' the gaps between boats
                blur: 50, 
                maxZoom: 10,
                gradient: {{0.2: 'blue', 0.4: 'cyan', 0.6: 'lime', 0.8: 'yellow', 1.0: 'red'}}
            }}).addTo(map);

            // Add Boat Markers
            points.forEach(p => {{
                L.circleMarker([p.lat, p.lon], {{
                    radius: 3, color: '#fff', weight: 1, fillOpacity: 0.5
                }}).addTo(map);
            }});
        </script>
    </body>
    </html>
    """