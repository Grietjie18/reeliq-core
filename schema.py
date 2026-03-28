from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

class Observation(BaseModel):
    # Meta Data
    observation_id: UUID = Field(default_factory=uuid4)
    vessel_id: str
    api_key: str  # We'll use this for Task 4
    
    # Temporal (UTC is non-negotiable for marine science)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    
    # Spatial
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    
    # Core Variables (Using CF Standard Names)
    sea_surface_temperature: float = Field(..., alias="temp_c")
    sea_surface_salinity: Optional[float] = None
    sea_surface_turbidity: Optional[float] = None # NTU
    
    # Propulsion Data (For Fuel Efficiency Task)
    speed_over_ground: float  # GPS speed
    speed_through_water: float # Sensor speed (impeller)
    
    # QC Flags (0=No QC, 1=Pass, 2=Suspect, 3=Fail)
    qc_flag: int = 0
