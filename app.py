import uvicorn
import json
import numpy as np
import copernicusmarine
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from scipy.interpolate import griddata
from schema import Observation

app = FastAPI()
fleet_status = {}

# --- SATELLITE ENGINE ---
def get_satellite_background():
    try:
        # Pulling Level 4 Gap-Filled SST (Sea Surface Temp)
        DATASET_ID = "METOFFICE-GLO-SST-L4-NRT-OBS-SST-V2"
        
        # J-Bay Bounding Box
        ds = copernicusmarine.open_dataset(
            dataset_id=DATASET_ID,
            minimum_longitude=24.8, maximum_longitude=25.2,
            minimum_latitude=-34.2, maximum_latitude=-33.9
        )
        
        # Latest frame converted from Kelvin to Celsius
        latest_sst = ds.analysed_sst.isel(time=-1) - 273.15
        
        # Convert to a flat list of points for the interpolation engine
        lats = latest_sst.latitude.values
        lons = latest_sst.longitude.values
        vals = latest_sst.values
        
        sat_points = []
        for i, lat in enumerate(lats):
            for j, lon in enumerate(lons):
                if not np.isnan(vals[i,j]):
                    sat_points.append([lat, lon, float(vals[i,j])])
        return sat_points
    except Exception as e:
        print(f"Satellite Fetch Error: {e}")
        return []

@app.post("/ingest/{vessel_id}")
async def ingest_data(vessel_id: str, obs: Observation):
    global fleet_status
    fleet_status[vessel_id] = obs.model_dump()
    return {"status": "success"}

@app.get("/", response_class=HTMLResponse)
async def map_dashboard():
    # 1. Get Satellite Data (The Background)
    sat_data = get_satellite_background()
    
    # 2. Get Boat Data (The Ground Truth)
    boat_lats = [o['latitude'] for o in fleet_status.values()]
    boat_lons = [o['longitude'] for o in fleet_status.values()]
    boat_temps = [o['temp_c'] for o in fleet_status.values()]

    # 3. DATA FUSION: Combine Satellite + Boat Points
    all_lats = [p[0] for p in sat_data] + boat_lats
    all_lons = [p[1] for p in sat_data] + boat_lons
    all_temps = [p[2] for p in sat_data] + boat_temps

    interpolated_points = []
    
    # Only run math if we have data
    if len(all_lats) > 5:
        grid_lat, grid_lon = np.mgrid[-34.15:-33.95:40j, 24.85:25.15:40j]
        grid_z = griddata((all_lats, all_lons), all_temps, (grid_lat, grid_lon), method='linear')

        for i in range(len(grid_lat)):
            for j in range(len(grid_lon)):
                val = grid_z[i,j]
                if not np.isnan(val):
                    interpolated_points.append([grid_lat[i,j], grid_lon[i,j], val])

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>REEL IQ | Satellite Fusion</title>
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
        <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
        <style>
            body {{ background: #0b1622; color: white; font-family: 'Segoe UI', sans-serif; margin: 0; padding: 20px; }}
            #map {{ height: 80vh; border-radius: 12px; border: 1px solid #333; }}
            .stats {{ display: flex; gap: 20px; margin-bottom: 10px; }}
        </style>
        <meta http-equiv="refresh" content="30">
    </head>
    <body>
        <h1>🛰️ REEL IQ <span style="font-weight:100">| Satellite + Vessel Fusion</span></h1>
        <div class="stats">
            <div>Nodes: <strong>{len(fleet_status)}</strong></div>
            <div>Source: <strong>Copernicus L4 NRT SST</strong></div>
        </div>
        <div id="map"></div>

        <script>
            var map = L.map('map').setView([-34.05, 25.02], 11);
            L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png').addTo(map);

            var modelData = {json.dumps(interpolated_points)};
            
            modelData.forEach(p => {{
                // Dynamic Color Logic
                var hue = 240 - ((p[2] - 15) * 20); // Blue to Red shift
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
