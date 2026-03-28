import os, uuid
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from databases import Database
from sqlalchemy import create_engine, MetaData, Table, Column, Float, String, DateTime, Integer

# --- 1. DATA SCHEMA & DB ---
class Observation(BaseModel):
    observation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    timestamp: datetime = Field(default_factory=datetime.now)
    latitude: float; longitude: float; sea_surface_temperature: float 
    sea_surface_salinity: Optional[float] = None; sea_surface_turbidity: Optional[float] = None
    speed_over_ground: float; speed_through_water: float; qc_flag: int = 0

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

MASTER_API_KEY = os.getenv("MASTER_API_KEY", "jbay-science-2026")
database = Database(DATABASE_URL); metadata = MetaData()

observations_table = Table(
    "observations", metadata,
    Column("observation_id", String, primary_key=True),
    Column("vessel_id", String, index=True),
    Column("timestamp", DateTime),
    Column("latitude", Float), Column("longitude", Float),
    Column("sea_surface_temperature", Float), Column("sea_surface_salinity", Float, nullable=True),
    Column("sea_surface_turbidity", Float, nullable=True),
    Column("speed_over_ground", Float), Column("speed_through_water", Float),
    Column("qc_flag", Integer, default=0)
)

app = FastAPI()

@app.on_event("startup")
async def startup():
    await database.connect()
    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine)

# --- 2. ENDPOINTS ---

@app.post("/ingest/{vessel_id}")
async def ingest_data(vessel_id: str, data: Observation, api_key: str = Query(...)):
    if api_key != MASTER_API_KEY: raise HTTPException(status_code=401)
    query = observations_table.insert().values(
        observation_id=str(data.observation_id), vessel_id=vessel_id,
        timestamp=data.timestamp, latitude=data.latitude, longitude=data.longitude,
        sea_surface_temperature=data.sea_surface_temperature, sea_surface_salinity=data.sea_surface_salinity,
        sea_surface_turbidity=data.sea_surface_turbidity, speed_over_ground=data.speed_over_ground,
        speed_through_water=data.speed_through_water, qc_flag=data.qc_flag
    )
    await database.execute(query); return {"status": "success"}

# LIVE DATA API (Used by the Map to update without flashing)
@app.get("/api/live")
async def get_live_json():
    try:
        # 1. Create a "Naive" time (stripping timezone info for the DB)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        time_limit = now - timedelta(hours=24)
        
        # 2. Query the database
        query = observations_table.select().where(
            observations_table.c.timestamp >= time_limit
        ).order_by(observations_table.c.timestamp.asc())
        
        rows = await database.fetch_all(query)
        
        vessels = {}
        for r in rows:
            v_id = r['vessel_id']
            if v_id not in vessels: 
                vessels[v_id] = {"path": [], "last": {}}
            vessels[v_id]["path"].append([r['latitude'], r['longitude']])
            vessels[v_id]["last"] = {
                "lat": r['latitude'], 
                "lon": r['longitude'], 
                "sst": r['sea_surface_temperature']
            }
        return vessels
    except Exception as e:
        # This will show you the REAL error in your Render logs
        print(f"DATABASE ERROR: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

# HOME PAGE (The Map Dashboard)
@app.get("/", response_class=HTMLResponse)
async def get_map():
    html_content = """
    <html>
        <head>
            <title>REEL IQ | Live Monitor</title>
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <style>
                body { margin: 0; background: #06090f; color: #00f2ff; font-family: sans-serif; overflow: hidden; }
                #map { height: 100vh; width: 100%; z-index: 1; }
                #overlay { position: absolute; top: 20px; left: 20px; z-index: 1000; background: rgba(0,18,25,0.85); padding: 15px; border-radius: 8px; border: 1px solid #00f2ff; min-width: 150px; }
            </style>
        </head>
        <body>
            <div id="overlay">
                <b style="font-size: 1.2em;">REEL IQ CORE</b><br>
                <small style="opacity: 0.7;">Offshore Monitoring</small>
                <hr style="border: 0.5px solid #00f2ff; opacity: 0.3; margin: 10px 0;">
                <div id="stats">Checking Database...</div>
            </div>
            <div id="map"></div>
            <script>
                var map = L.map('map', { zoomControl: false, attributionControl: false }).setView([-34.14, 25.02], 11);
                L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png').addTo(map);
                
                var layers = L.layerGroup().addTo(map);

                async function updateData() {
                    try {
                        const response = await fetch('/api/live');
                        const data = await response.json();
                        layers.clearLayers(); 
                        
                        let count = 0;
                        for (const [v_id, info] of Object.entries(data)) {
                            count++;
                            // Draw Tracks (Polylines)
                            if (info.path.length > 1) {
                                L.polyline(info.path, {color: '#00f2ff', weight: 1.5, opacity: 0.4}).addTo(layers);
                            }
                            // Draw Vessel Dot (CircleMarker)
                            L.circleMarker([info.last.lat, info.last.lon], {
                                radius: 7, fillColor: "#00f2ff", color: "#fff", weight: 2, fillOpacity: 1
                            }).addTo(layers).bindPopup('<b>' + v_id + '</b><br>SST: ' + info.last.sst + '°C');
                        }
                        document.getElementById('stats').innerText = "Vessels Active: " + count;
                    } catch (e) {
                        console.log("Update failed, retrying...");
                    }
                }

                // Smoothly update every 10 seconds (No screen flash)
                setInterval(updateData, 10000);
                updateData(); // Run once on page load
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)
