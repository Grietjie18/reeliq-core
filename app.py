import uvicorn
import json
import os
from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from schema import Observation

app = FastAPI()
fleet_status = {}
recent_logs = []

@app.post("/ingest/{vessel_id}")
async def ingest_data(vessel_id: str, obs: Observation):
    global fleet_status, recent_logs
    data = obs.model_dump()
    fleet_status[vessel_id] = data
    
    # Keep a running log of the last 10 pings for the UI
    log_entry = {
        "id": vessel_id, 
        "time": datetime.now().strftime("%H:%M:%S"),
        "temp": data['temp_c']
    }
    recent_logs.insert(0, log_entry)
    recent_logs = recent_logs[:10]
    return {"status": "success"}

@app.get("/", response_class=HTMLResponse)
async def map_dashboard():
    vessels = [{"id": k, **v} for k, v in fleet_status.items()]
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>REEL IQ | Command Center</title>
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
        <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
        <style>
            body {{ background: #06090f; color: #e6edf3; margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica; overflow: hidden; }}
            #map {{ height: 100vh; width: 100vw; position: absolute; z-index: 1; }}
            
            .sidebar {{ 
                position: absolute; top: 20px; left: 20px; width: 300px; z-index: 1000;
                background: rgba(13, 17, 23, 0.85); backdrop-filter: blur(10px);
                border: 1px solid #30363d; border-radius: 12px; padding: 20px;
                box-shadow: 0 8px 32px rgba(0,0,0,0.5);
            }}

            .status-pulse {{
                display: inline-block; width: 10px; height: 10px; background: #238636;
                border-radius: 50%; margin-right: 8px; box-shadow: 0 0 8px #238636;
                animation: pulse 2s infinite;
            }}

            @keyframes pulse {{ 
                0% {{ opacity: 1; }} 50% {{ opacity: 0.4; }} 100% {{ opacity: 1; }}
            }}

            .log-item {{ font-size: 11px; padding: 8px 0; border-bottom: 1px solid #21262d; color: #8b949e; }}
            .log-item b {{ color: #58a6ff; }}
            h2 {{ margin: 0; font-size: 18px; color: #f0f6fc; display: flex; align-items: center; }}
            .stats-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 15px 0; }}
            .stat-box {{ background: #161b22; padding: 10px; border-radius: 6px; border: 1px solid #30363d; text-align: center; }}
        </style>
    </head>
    <body>
        <div class="sidebar">
            <h2><span class="status-pulse"></span> REEL IQ CORE</h2>
            <div style="font-size: 12px; opacity: 0.6; margin-bottom: 15px;">Operational Oceanography Unit</div>
            
            <div class="stats-grid">
                <div class="stat-box"><small>FLEET</small><br><strong>{len(vessels)}</strong></div>
                <div class="stat-box"><small>AVG TEMP</small><br><strong>{round(sum(v['temp_c'] for v in vessels)/len(vessels),1) if vessels else 0}°C</strong></div>
            </div>

            <div style="font-size: 12px; font-weight: bold; margin-bottom: 10px; color: #f0f6fc;">LIVE INGESTION FEED</div>
            <div id="logs">
                {"".join([f'<div class="log-item">[{l["time"]}] <b>{l["id"]}</b>: {l["temp"]}°C</div>' for l in recent_logs])}
            </div>
        </div>

        <div id="map"></div>

        <script>
            var lastCenter = JSON.parse(sessionStorage.getItem('mapCenter')) || [-34.05, 25.02];
            var lastZoom = sessionStorage.getItem('mapZoom') || 12;

            var map = L.map('map', {{ zoomControl: false }}).setView(lastCenter, lastZoom);
            L.control.zoom({{ position: 'topright' }}).addTo(map);

            map.on('moveend', () => {{
                sessionStorage.setItem('mapCenter', JSON.stringify(map.getCenter()));
                sessionStorage.setItem('mapZoom', map.getZoom());
            }});

            L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png').addTo(map);

            var vesselData = {json.dumps(vessels)};
            vesselData.forEach(v => {{
                var hue = 240 - ((v.temp_c - 16) * 34);
                if (hue < 0) hue = 0; if (hue > 240) hue = 240;

                L.circleMarker([v.latitude, v.longitude], {{
                    radius: 7, fillColor: "hsl(" + hue + ", 100%, 50%)",
                    color: "#fff", weight: 1.5, fillOpacity: 0.8
                }}).addTo(map).bindPopup("<b>" + v.id + "</b><br>Temp: " + v.temp_c + "°C");
            }});

            setTimeout(() => {{ location.reload(); }}, 5000);
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
