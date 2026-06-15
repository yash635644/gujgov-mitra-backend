import os
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from pydantic import BaseModel
from typing import List, Optional
from dotenv import load_dotenv

from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

from google import genai
from google.genai import types
from groq import Groq
from sqlalchemy.orm import Session

# Import our DB setup and models
from database import get_db, SessionLocal
from models import GovernmentScheme, ChatLog

# Import Admin Routes
import admin_routes

load_dotenv()

app = FastAPI(title="GujGov Mitra API")

# Background Scheduler for DB Bloat Cleanup
scheduler = BackgroundScheduler()

def automatic_log_cleanup(days: int = 23):
    print(f"🧹 Running Automatic Log Cleanup (older than {days} days)...")
    db = SessionLocal()
    try:
        cutoff_date = datetime.now() - timedelta(days=days)
        deleted_count = db.query(ChatLog).filter(ChatLog.timestamp < cutoff_date).delete()
        db.commit()
        print(f"✅ Deleted {deleted_count} old chat logs.")
    except Exception as e:
        print(f"❌ Cleanup failed: {e}")
        db.rollback()
    finally:
        db.close()

@app.on_event("startup")
def start_scheduler():
    # Run once a day (interval=24 hours)
    scheduler.add_job(automatic_log_cleanup, 'interval', hours=24, kwargs={'days': 23})
    scheduler.start()
    print("⏰ APScheduler started: Daily DB cleanup scheduled.")

@app.on_event("shutdown")
def shutdown_scheduler():
    scheduler.shutdown()
    print("⏰ APScheduler shutdown.")

# Setup Rate Limiter
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include the Admin API Routes
app.include_router(admin_routes.router)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai_client = genai.Client(api_key=GEMINI_API_KEY)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

class Message(BaseModel):
    role: str
    content: str
                 
class ChatRequest(BaseModel):
    message: str
    history: Optional[List[Message]] = []

def get_embedding(text: str):
    if not GEMINI_API_KEY:
        return None
    result = genai_client.models.embed_content(
        model="gemini-embedding-001",
        contents=text,
    )
    return result.embeddings[0].values

def build_system_prompt(context_chunks: List[str]) -> str:
    base_rules = """You are "GujGov Mitra" (ગુજગોવ મિત્ર), an expert AI government assistant for citizens of Gujarat, India.

CRITICAL INSTRUCTIONS ON INFORMATION SOURCES & SCOPE:
1. You have access to official Internal Government Schemes (provided below). Use this for explaining step-by-step processes, documents, and rules.
2. You also have LIVE internet access via Google Search. If the user asks for breaking news, latest alerts, recent announcements by the Chief Minister, or anything not covered by the internal schemes, SEARCH GOOGLE implicitly to provide an accurate, up-to-date answer.
3. STRICT OUT-OF-SCOPE RULE: You are ONLY allowed to answer questions related to Gujarat Government, Central Indian Government, public schemes, civic issues, official news, or public services. If a user asks about general knowledge, technology (e.g., iPhone prices), sports, entertainment, coding, or anything outside the realm of government/public services, YOU MUST POLITELY REFUSE. 
   - Example Refusal: "I am GujGov Mitra, an AI dedicated to Gujarat Government services and schemes. I cannot answer general topics like that, but I would be happy to help you with any government-related inquiries!"
    
LANGUAGE RULES:
1. If user writes in Gujarati script → reply 100% in Gujarati
2. If user writes in English → reply in English
3. If user mixes Gujarati + English → reply in Gujarati with English terms in brackets
4. ALWAYS use simple language and be warm and friendly.

FORMAT RULES FOR SCHEMES:
👋 [1-2 line friendly answer]
✅ What You Need to Do: [Clear direct answer if applicable]
📋 Required Documents: [Bullet points if applicable]
🔢 Step-by-Step Process: [Numbered list if applicable]
🌐 Official Portal / Helpline: [Details and links]

FORMAT RULES FOR NEWS/ANNOUNCEMENTS:
🗞️ [Brief, accurate summary of the news]
📅 [Date of announcement]
🔗 [Source Link]

Here is the exact specific Government Information retrieved from our internal database for this user's query:
"""
    context_text = "\n\n---\n\n".join(context_chunks)
    return base_rules + "\n" + context_text

async def chat_with_gemini(message: str, history: List[Message], system_prompt: str) -> str:
    # 1. Format history into genai.types.Content 
    formatted_contents = []
    
    # Optional: Find first user message if needed
    first_user_idx = next((i for i, m in enumerate(history) if m.role == 'user'), -1)
    if first_user_idx != -1:
        history_to_use = history[first_user_idx:]
    else:
        history_to_use = []

    for msg in history_to_use:
        formatted_contents.append(
            types.Content(
                role="user" if msg.role == 'user' else "model",
                parts=[types.Part.from_text(text=msg.content if msg.content.strip() else " ")]
            )
        )
        
    # Append the new user message
    formatted_contents.append(
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=message if message.strip() else " ")]
        )
    )

    # 2. Call the chat generation API with Search Grounding
    response = genai_client.models.generate_content(
        model='gemini-2.5-flash',
        contents=formatted_contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[{"google_search": {}}],  # Valid syntax for the new SDK
        ),
    )
    return response.text

async def chat_with_groq(message: str, history: List[Message], system_prompt: str) -> str:
    messages = [{"role": "system", "content": system_prompt}]

    first_user_idx = next((i for i, m in enumerate(history) if m.role == "user"), -1)
    clean_history = history[first_user_idx:] if first_user_idx != -1 else []

    for msg in clean_history:
        messages.append({
            "role": "user" if msg.role == "user" else "assistant",
            "content": msg.content if msg.content.strip() else " "
        })

    messages.append({"role": "user", "content": message if message.strip() else " "})

    chat_completion = groq_client.chat.completions.create(
        messages=messages,
        model="llama-3.3-70b-versatile",
        temperature=0.1,
        max_tokens=2048,
    )

    content = chat_completion.choices[0].message.content
    return content if content else "Sorry, I could not generate a response."

@app.post("/api/chat")
@limiter.limit("15/minute")
async def chat_endpoint(request: Request, chat_request: ChatRequest, db: Session = Depends(get_db)):
    message = chat_request.message
    history = chat_request.history

    # 1. Generate embedding for user query inside threadpool to prevent blocking event loop
    user_embedding = await run_in_threadpool(get_embedding, message)
    context_chunks = []
    
    # 2. Retrieve relevant context from Neon DB inside a threadpool
    if user_embedding:
        print("🔍 Searching Neon RAG Database for relevant schemes...")
        
        def fetch_schemes():
            return db.query(GovernmentScheme).order_by(
                GovernmentScheme.embedding.cosine_distance(user_embedding)
            ).limit(3).all()
            
        closest_schemes = await run_in_threadpool(fetch_schemes)
        
        for scheme in closest_schemes:
            print(f"📄 Found Match: {scheme.title}")
            context_chunks.append(scheme.content)

    # 3. Build the highly targeted RAG system prompt
    dynamic_system_prompt = build_system_prompt(context_chunks)

    response_text = None
    used_provider = None

    if GEMINI_API_KEY:
        try:
            response_text = await chat_with_gemini(message, history, dynamic_system_prompt)
            used_provider = "Gemini"
        except Exception as e:
            print(f"⚠️ Gemini failed, switching to Groq fallback: {e}")

    if not response_text and groq_client:
        try:
            response_text = await chat_with_groq(message, history, dynamic_system_prompt)
            used_provider = "Groq"
        except Exception as e:
            print(f"❌ Groq also failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    if not response_text:
        raise HTTPException(
            status_code=500, 
            detail="No AI configured."
        )

    # 4. Save to ChatLog in DB
    try:
        new_log = ChatLog(user_message=message, ai_response=response_text)
        db.add(new_log)
        db.commit()
    except Exception as e:
        print(f"Failed to save chat log: {e}")

    print(f"✅ Response sent via {used_provider} using RAG!")
    return {"response": response_text}

@app.post("/api/transcribe")
@limiter.limit("10/minute")
async def transcribe_audio(request: Request, file: UploadFile = File(...)):
    if not groq_client:
        raise HTTPException(status_code=500, detail="Groq API not configured.")
        
    # Check MIME type strictly
    if not file.content_type or not file.content_type.startswith("audio/"):
        raise HTTPException(status_code=400, detail="Invalid file type. Only audio files are allowed.")
        
    try:
        content = await file.read()
        
        # Limit to 5MB (5 * 1024 * 1024 bytes)
        MAX_FILE_SIZE = 5 * 1024 * 1024
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=413, detail="File too large. Maximum size is 5MB.")
            
        audio_tuple = ("recording.webm", content, file.content_type)
        
        def run_transcription():
            return groq_client.audio.transcriptions.create(
                file=audio_tuple,
                model="whisper-large-v3",
            )
            
        transcription = await run_in_threadpool(run_transcription)
        return {"text": transcription.text}
    except Exception as e:
        print(f"Transcription error: {e}")
        raise HTTPException(status_code=500, detail="Audio transcription failed")

@app.get("/health")
async def health_check():
    return {"status": "ok", "gemini_rag_active": bool(GEMINI_API_KEY)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
