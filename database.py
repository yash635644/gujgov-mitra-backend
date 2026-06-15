import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

# Load .env
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL is not set in the .env file")

# Ensure sslmode=require for Neon DB if not already present
if "?" not in DATABASE_URL:
    DATABASE_URL += "?sslmode=require"

# Create the SQLAlchemy Engine with connection pool recycling and pre-ping
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,      # Tests connection before using to prevent "SSL closed unexpectedly" errors
    pool_recycle=3600,       # Recycles connections after an hour to prevent stale connections
    pool_size=10,
    max_overflow=20
)

# Create a sessionmaker
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for the models
Base = declarative_base()

# Dependency for FastAPI
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
