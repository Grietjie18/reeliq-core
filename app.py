import os
import uuid
from typing import List, Optional
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from databases import Database
from sqlalchemy import create_engine, MetaData, Table, Column, Float, String, DateTime, Integer

# --- 1. DATA SCHEMA (CF-CONVENTION ALIGNED) ---
class Observation(BaseModel):
    observation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    
    # Core Variables
    sea_surface_temperature: float 
    sea_surface_salinity: Optional[float] = None
    sea_surface_turbidity: Optional[float] = None
    
    # Propulsion Data
    speed_over_ground: float
    speed_through_water: float
    
    # Quality Control
    qc_flag: int = 0

# --- 2. DATABASE CONFIGURATION ---
DATABASE_URL = os.getenv("DATABASE_URL")

# If testing locally without a DB, this handles the error gracefully
if not DATABASE_URL:
    DATABASE_URL = "sqlite:///./test.db"  # Fallback to local file for safety

database = Database(DATABASE_URL)
metadata = MetaData()

observations_table = Table(
    "observations",
    metadata,
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

# --- 3. APP INITIALIZATION ---
app = FastAPI(title="REEL IQ Core API")

@app.on_event("startup")
async def startup():
    await database.connect()
    # This creates the physical table in Postgres if it doesn't exist
    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine)

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

# --- 4. ENDPOINTS ---

@app.get("/")
async def root():
    return {"message": "REEL IQ Core Online", "database": "Connected"}

@app.post("/ingest/{vessel_id}")
async def ingest_data(vessel_id: str, data: Observation):
    try:
        query = observations_table.insert().values(
            observation_id=str(data.observation_id),
            vessel_id=vessel_id,
            timestamp=data.timestamp,
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
        return {"status": "success", "vessel": vessel_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/data")
async def get_all_data(limit: int = 100):
    """Returns the last 100 pings from the database for the map."""
    query = observations_table.select().order_by(observations_table.c.timestamp.desc()).limit(limit)
    return await database.fetch_all(query)

@app.get("/vessel/{vessel_id}")
async def get_vessel_history(vessel_id: str):
    """Returns the history for a specific boat."""
    query = observations_table.select().where(observations_table.c.vessel_id == vessel_id)
    return await database.fetch_all(query)
