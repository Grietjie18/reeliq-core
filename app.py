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
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    latitude: float
    longitude: float
    sea_surface_temperature: float 
    sea_surface_salinity: Optional[float] = None
    sea_surface_turbidity: Optional[float] = None
    speed_over_ground: float
    speed_through_water: float
    qc_flag: int = 0

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

MASTER_API_KEY = os.getenv("MASTER_API_KEY", "jbay-science-2026")
database = Database(DATABASE_URL)
metadata = MetaData()

observations_table = Table(
    "observations", metadata,
    Column("observation_id", String, primary_key=True),
    Column("vessel_id", String, index=True),
    Column("timestamp", DateTime),
    Column("latitude", Float), 
    Column("longitude", Float),
    Column("sea_surface_temperature", Float), 
    Column("sea_surface_salinity", Float, nullable=True),
    Column("sea_surface_turbidity", Float, nullable=True),
    Column("speed_over_ground", Float), 
    Column("speed_through_water", Float),
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
    if api_key != MASTER_API_KEY: 
        raise HTTPException(status_code=401)
    
    try:
        query = observations_table.insert().values(
            observation_id=str(data.observation_id), 
            vessel_id=vessel_id,
            timestamp=data.timestamp.replace(tzinfo=None), 
            latitude=data.latitude, 
            longitude=data.longitude,
            sea_surface_temperature=data.sea_surface_temperature, 
            sea_surface_salinity=data.sea_surface_salinity,
            sea_surface_turbidity=data.sea_surface_turbidity, 
            speed_over_ground=data.speed_over_ground,
            speed_through_water=data.speed_through_water, 
            qc_flag=data.qc_flag
        )
        await database.execute(query)
        return {"status": "success"}
    except Exception as e:
        print(f"DATABASE ERROR ON INGEST: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/live")
async def get_live_json():
    try:
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        # Using a wider 24-hour window just to guarantee we see data
        time_limit = now_utc - timedelta(hours=24)
        
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
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/", response_class=HTMLResponse)
async def get_map():
    html_content = """
    <html>
        <head>
            <title>REEL IQ | Live Thermal Sea</title>
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <script src="https://leaflet.github.io/Leaflet.heat/dist/leaflet-heat.js"></script>
            <style>
                body { margin: 0; background: #06090f; color: #00f2ff; font-family: sans-serif; }
                #map { height: 100vh; width: 100%; }
                #overlay { 
                    position: absolute; top: 20px; left: 20px; z-index: 1000; 
                    background: rgba(0,18,25,0.9); padding: 15px; border-radius: 8px; 
                    border: 1px solid #00f2ff; box-shadow: 0 0 15px rgba(0,242,255,0.2);
                }
                /* Use 'screen' or 'lighter' to make overlapping heat points much brighter */
                .leaflet-heatmap-layer { opacity: 0.95; mix-blend-mode: screen; }
            </style>
        </head>
        <body>
            <div id="overlay">
                <b>REEL IQ | OCEAN THERMAL</b><br>
                <small id="status">Syncing Sensors...</small>
            </div>
            <div id="map"></div>
            <script>
                var map = L.map('map', { zoomControl: false, attributionControl: false }).setView([-34.14, 25.02], 11);
                L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png').addTo(map);

                // 🔥 ULTRA-BRIGHT HEATMAP CONFIG
                var heatLayer = L.heatLayer([], {
                    radius: 110,      // Massive radius to force ocean coverage
                    blur: 50,         // Crisp but smooth edges
                    max: 1.0,         
                    minOpacity: 0.6,  // High floor so we see EVERYTHING
                    gradient: {
                        0.0: 'blue', 
                        0.2: 'cyan', 
                        0.5: 'lime', 
                        0.8: 'yellow', 
                        1.0: 'red'
                    }
                }).addTo(map);

                async function updateThermalGrid() {
                    try {
                        const response = await fetch('/api/live');
                        const data = await response.json();
                        let points = [];
                        let sensorCount = Object.keys(data).length;
                        
                        for (const [v_id, info] of Object.entries(data)) {
                            // Calculate Intensity: Any SST from 10 to 30 degC
                            // This ensures that even cold winter water (15C) is visible
                            let intensity = (info.last.sst - 10) / 20; 
                            intensity = Math.min(Math.max(intensity, 0.4), 1.0);

                            info.path.forEach(coord => {
                                // 10x Jitter Multiplier to turn a single line into a wide thermal break
                                for (let i = 0; i < 10; i++) {
                                    let latJ = (Math.random() - 0.5) * 0.02;
                                    let lonJ = (Math.random() - 0.5) * 0.02;
                                    points.push([coord[0] + latJ, coord[1] + lonJ, intensity]);
                                }
                            });
                        }
                        
                        if (points.length > 0) {
                            heatLayer.setLatLngs(points);
                        }
                        document.getElementById('status').innerText = "Live Sensors: " + sensorCount;
                        
                    } catch (e) { 
                        console.error("Refresh Error"); 
                    }
                }

                setInterval(updateThermalGrid, 10000);
                updateThermalGrid();
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)
