from sqlalchemy import text
from database import engine, Base
import models

def init_db():
    print("Connecting to the database to enable pgvector extension...")
    
    with engine.connect() as conn:
        # Enable the vector extension for RAG mathematical searches
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
        
    print("Creating tables if they don't exist...")
    # This reads all classes inheriting from Base and creates them in Neon
    Base.metadata.create_all(bind=engine)
    
    print("Database tables created successfully!")

if __name__ == "__main__":
    init_db()
