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
    # 1. Combine Baseline + Live Boats
    base = get_baseline()
    lats = [p['lat'] for p in base] + [o['latitude'] for o in fleet_status.values()]
    lons = [p['lon'] for p in base] + [o['longitude'] for o in fleet_status.values()]
    temps = [p['temp'] for p in base] + [o['temp_c'] for o in fleet_status.values()]

    interpolated_points = []
    
    # 2. RUN THE MATH (The "Real" Interpolation)
    if len(lats) > 3:
        # We create a 40x40 grid over the bay
        grid_lat, grid_lon = np.mgrid[-34.15:-33.95:40j, 24.85:25.15:40j]
        
        # This fills every pixel based on the nearest data points
        grid_z = griddata((lats, lons), temps, (grid_lat, grid_lon), method='linear')

        for i in range(len(grid_lat)):
            for j in range(len(grid_lon)):
                val = grid_z[i,j]
                if not np.isnan(val):
                    interpolated_points.append([grid_lat[i,j], grid_lon[i,j], float(val)])

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>REEL IQ | Ocean Intel</title>
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
        <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
        <style>
            body {{ background: #0b1622; color: white; font-family: sans-serif; margin: 0; padding: 10px; }}
            #map {{ height: 90vh; border-radius: 15px; border: 2px solid #1a2a3a; }}
            .legend {{ position: absolute; bottom: 40px; right: 20px; z-index: 1000; background: rgba(0,0,0,0.8); padding: 10px; border-radius: 5px; }}
        </style>
        <meta http-equiv="refresh" content="10">
    </head>
    <body>
        <h2 style="margin:5px">⚓ REEL IQ <span style="font-weight:100">| Live Spatial Model</span></h2>
        <div id="map"></div>
        <div class="legend">
            <strong>Surface Temp (°C)</strong><br>
            <div style="background: linear-gradient(to right, blue, yellow, red); height: 10px; width: 100%;"></div>
            <small>16°C &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; 23°C</small>
        </div>
        <script>
            var map = L.map('map').setView([-34.05, 25.02], 11);
            L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png').addTo(map);

            var modelData = {json.dumps(interpolated_points)};
            
            modelData.forEach(p => {{
                // This creates the professional "pixelated" look
                var hue = 240 - ((p[2] - 16) * 15); 
                L.rectangle([[p[0]-0.002, p[1]-0.002], [p[0]+0.002, p[1]+0.002]], {{
                    color: "hsl(" + hue + ", 100%, 50%)", weight: 0, fillOpacity: 0.5
                }}).addTo(map);
            }});
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)