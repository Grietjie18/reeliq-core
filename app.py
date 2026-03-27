import uvicorn
import json
import os
from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from schema import Observation

app = FastAPI()
# We keep this global so it persists as long as the server is awake
fleet_status = {}
recent_logs = []

@app.post("/ingest/{vessel_id}")
async def ingest_data(vessel_id: str, obs: Observation):
    global fleet_status, recent_logs
    data = obs.model_dump()
    fleet_status[vessel_id] = data
    
    log_entry = {
        "id": vessel_id, 
        "time": datetime.now().strftime("%H:%M:%S"),
        "temp": data['temp_c']
    }
    recent_logs.insert(0, log_entry)
    if len(recent_logs) > 8: recent_logs.pop()
    return {"status": "success"}

# NEW: This "Endpoint" only sends the raw data, not the whole HTML page
@app.get("/data")
async def get_data():
    vessels = [{"id": k, **v} for k, v in fleet_status.items()]
    return {"vessels": vessels, "logs": recent_logs}

@app.get("/", response_class=HTMLResponse)
async def map_dashboard():
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>REEL IQ | Live Analytics</title>
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
        <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
        <style>
            body {{ background: #06090f; color: #e6edf3; margin: 0; font-family: sans-serif; overflow: hidden; }}
            #map {{ height: 100vh; width: 100vw; position: absolute; z-index: 1; }}
            .sidebar {{ 
                position: absolute; top: 20px; left: 20px; width: 280px; z-index: 1000;
                background: rgba(13, 17, 23, 0.85); backdrop-filter: blur(10px);
                border: 1px solid #30363d; border-radius: 12px; padding: 15px;
            }}
            .log-item {{ font-size: 11px; padding: 5px 0; border-bottom: 1px solid #21262d; color: #8b949e; }}
            .status-pulse {{
                display: inline-block; width: 8px; height: 8px; background: #238636;
                border-radius: 50%; margin-right: 8px; animation: pulse 2s infinite;
            }}
            @keyframes pulse {{ 0% {{ opacity: 1; }} 50% {{ opacity: 0.3; }} 100% {{ opacity: 1; }} }}
        </style>
    </head>
    <body>
        <div class="sidebar">
            <h3 style="margin:0;"><span class="status-pulse"></span> REEL IQ LIVE</h3>
            <div id="stats" style="margin: 10px 0; font-size: 13px;">Detecting Fleet...</div>
            <div style="font-size: 11px; font-weight: bold; color: #58a6ff;">RECENT PINGS</div>
            <div id="log-container"></div>
        </div>
        <div id="map"></div>

        <script>
            var map = L.map('map', {{ zoomControl: false }}).setView([-34.05, 25.02], 12);
            L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png').addTo(map);
            
            var vesselLayer = L.layerGroup().addTo(map);

            async function updateDashboard() {{
                try {{
                    const response = await fetch('/data');
                    const data = await response.json();
                    
                    // 1. Update Stats
                    document.getElementById('stats').innerHTML = `Fleet Size: <b>${{data.vessels.length}}</b>`;
                    
                    // 2. Update Logs
                    let logHtml = "";
                    data.logs.forEach(l => {{
                        logHtml += `<div class="log-item">[${{l.time}}] <b>${{l.id}}</b>: ${{l.temp}}°C</div>`;
                    }});
                    document.getElementById('log-container').innerHTML = logHtml;

                    // 3. Update Map Markers WITHOUT refreshing the whole page
                    vesselLayer.clearLayers();
                    data.vessels.forEach(v => {{
                        var hue = 240 - ((v.temp_c - 16) * 34);
                        if (hue < 0) hue = 0; if (hue > 240) hue = 240;

                        L.circleMarker([v.latitude, v.longitude], {{
                            radius: 7, fillColor: "hsl(" + hue + ", 100%, 50%)",
                            color: "#fff", weight: 1, fillOpacity: 0.8
                        }}).addTo(vesselLayer).bindTooltip(v.id + ": " + v.temp_c + "°C");
                    }});
                }} catch (e) {{ console.log("Waiting for data..."); }}
            }}

            // Run update every 3 seconds - NO WHITE FLASH!
            setInterval(updateDashboard, 3000);
            updateDashboard();
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
