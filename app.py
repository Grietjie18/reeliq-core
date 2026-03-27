import uvicorn
import json
import os
import numpy as np
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from scipy.interpolate import griddata
from schema import Observation

app = FastAPI()
fleet_status = {}

# Load the "Baseline" (Fake Satellite)
def get_baseline():
    # Looks for the JSON in the same folder as this script
    path = os.path.join(os.path.dirname(__file__), "satellite_data.json")
    with open(path, "r") as f:
        return json.load(f)

@app.post("/ingest/{vessel_id}")
async def ingest_data(vessel_id: str, obs: Observation):
    global fleet_status
    fleet_status[vessel_id] = obs.model_dump()
    return {"status": "success"}

@app.get("/", response_class=HTMLResponse)
async def map_dashboard():
    base = get_baseline()
    
    # Extract coordinates and values for interpolation
    lats = [p['lat'] for p in base] + [o['latitude'] for o in fleet_status.values()]
    lons = [p['lon'] for p in base] + [o['longitude'] for o in fleet_status.values()]
    temps = [p['temp'] for p in base] + [o['temp_c'] for o in fleet_status.values()]

    interpolated_points = []
    
    if len(lats) > 3:
        # 60x60 grid creates 3,600 data points for a smooth surface
        grid_lat, grid_lon = np.mgrid[-34.15:-33.95:60j, 24.85:25.15:60j]
        
        # Linear interpolation blends the boat data into the satellite background
        grid_z = griddata((lats, lons), temps, (grid_lat, grid_lon), method='linear')

        for i in range(len(grid_lat)):
            for j in range(len(grid_lon)):
                val = grid_z[i,j]
                lat, lon = grid_lat[i,j], grid_lon[i,j]
                
                # THE LAND FILTER: Only keep points East of the J-Bay shore (approx 24.92)
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
            body {{ background: #0b1622; color: white; margin: 0; font-family: sans-serif; overflow: hidden; }}
            #map {{ height: 100vh; width: 100vw; }}
            .overlay {{ 
                position: absolute; top: 10px; left: 50px; z-index: 1000; 
                background: rgba(0,0,0,0.8); padding: 15px; border-radius: 8px; 
                border: 1px solid #4dabf7; pointer-events: none;
            }}
            .legend {{
                position: absolute; bottom: 30px; right: 20px; z-index: 1000;
                background: rgba(0,0,0,0.8); padding: 10px; border-radius: 5px;
                font-size: 12px; border: 1px solid #333;
            }}
        </style>
    </head>
    <body>
        <div class="overlay">
            <h2 style="margin:0; color:#4dabf7;">⚓ REEL IQ</h2>
            <small>Live Fusion: Satellite + {len(fleet_status)} Fleet Nodes</small>
        </div>

        <div class="legend">
            <strong>Temp (°C)</strong><br>
            <div style="background: linear-gradient(to right, blue, cyan, yellow, red); height: 10px; width: 120px; margin: 5px 0;"></div>
            <div style="display:flex; justify-content: space-between;">
                <span>16°C</span><span>23°C</span>
            </div>
        </div>

        <div id="map"></div>

        <script>
            // PERSISTENT ZOOM: Checks session storage so refresh doesn't reset your view
            var lastCenter = JSON.parse(sessionStorage.getItem('mapCenter')) || [-34.05, 25.02];
            var lastZoom = sessionStorage.getItem('mapZoom') || 11;

            var map = L.map('map', {{ zoomControl: false }}).setView(lastCenter, lastZoom);
            L.control.zoom({{ position: 'topright' }}).addTo(map);
            
            // Save map state whenever user moves or zooms
            map.on('moveend', () => {{
                sessionStorage.setItem('mapCenter', JSON.stringify(map.getCenter()));
                sessionStorage.setItem('mapZoom', map.getZoom());
            }});

            L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png').addTo(map);

            var modelData = {json.dumps(interpolated_points)};
            
            modelData.forEach(p => {{
                // HSL Color logic: 240 (Blue) at 16°C down to 0 (Red) at 23°C
                var hue = 240 - ((p[2] - 16) * 30); 
                if (hue < 0) hue = 0;
                if (hue > 240) hue = 240;

                // Rectangle size (0.003) allows overlap for a seamless look
                L.rectangle([[p[0]-0.002, p[1]-0.002], [p[0]+0.002, p[1]+0.002]], {{
                    color: "hsl(" + hue + ", 100%, 50%)", 
                    weight: 0, 
                    fillOpacity: 0.45
                }}).addTo(map);
            }});

            // Auto-refresh data every 15 seconds without losing zoom
            setTimeout(() => {{ location.reload(); }}, 15000);
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    # Use the port Render provides or default to 8000
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
