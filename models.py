from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean
from sqlalchemy.sql import func
from pgvector.sqlalchemy import Vector
from database import Base

class GovernmentScheme(Base):
    __tablename__ = "government_schemes"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, index=True)
    content = Column(Text)
    
    # Gemini gemini-embedding-001 uses 3072 dimensions
    embedding = Column(Vector(3072))

class PendingScheme(Base):
    """Temporary staging table for Groq-scraped data pending Admin approval."""
    __tablename__ = "pending_schemes"

    id = Column(Integer, primary_key=True, index=True)
    source_url = Column(String, index=True, nullable=True)
    title = Column(String, index=True)
    content = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class ChatLog(Base):
    __tablename__ = "chat_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_message = Column(Text)
    ai_response = Column(Text)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

class CrawlerSource(Base):
    __tablename__ = "crawler_sources"
    
    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, unique=True, index=True)
    description = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
