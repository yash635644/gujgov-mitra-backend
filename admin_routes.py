from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional
from pydantic import BaseModel
import os
import io
from fastapi.responses import StreamingResponse
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter

from database import get_db
from models import GovernmentScheme, ChatLog, CrawlerSource, PendingScheme
from datetime import datetime, timedelta, timezone
import jwt
from google import genai
from crawler import scrape_website, extract_scheme_with_groq, get_embedding_with_gemini_safely

ADMIN_USER = os.getenv("ADMIN_USER")
ADMIN_PASS = os.getenv("ADMIN_PASS")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not ADMIN_USER or not ADMIN_PASS or not ADMIN_TOKEN:
    print("⚠️ WARNING: Admin credentials not fully set in .env! Admin routes may fail.")
    # For a stricter setup in production, you would do: raise RuntimeError(...)

if GEMINI_API_KEY:
    genai_client = genai.Client(api_key=GEMINI_API_KEY)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
)

def verify_admin(x_admin_token: str = Header(...)):
    try:
        payload = jwt.decode(x_admin_token, ADMIN_TOKEN, algorithms=["HS256"])
        if payload.get("sub") != ADMIN_USER:
            raise HTTPException(status_code=401, detail="Invalid Token Subject")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token Expired. Please log in again.")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid Token")

# --- Authentication ---
class LoginRequest(BaseModel):
    username: str
    password: str

@router.post("/login")
def login(creds: LoginRequest):
    if creds.username == ADMIN_USER and creds.password == ADMIN_PASS:
        # Generate JWT token valid for 24 hours
        payload = {
            "sub": ADMIN_USER,
            "exp": datetime.now(timezone.utc) + timedelta(hours=24)
        }
        token = jwt.encode(payload, ADMIN_TOKEN, algorithm="HS256")
        return {"token": token, "user": ADMIN_USER}
    raise HTTPException(status_code=401, detail="Invalid credentials")

# --- Dashboard Stats ---
@router.get("/stats", dependencies=[Depends(verify_admin)])
def get_dashboard_stats(db: Session = Depends(get_db)):
    total_schemes = db.query(func.count(GovernmentScheme.id)).scalar()
    total_chats = db.query(func.count(ChatLog.id)).scalar()
    total_crawlers = db.query(func.count(CrawlerSource.id)).scalar()
    
    # Generate 7-day chart data based on ChatLog timestamps
    chart_data = []
    today = datetime.now()
    seven_days_ago = today - timedelta(days=6)
    start_of_seven_days_ago = seven_days_ago.replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Execute a SINGLE query to fetch timestamps instead of 7 network trips
    recent_chats = db.query(ChatLog.timestamp).filter(
        ChatLog.timestamp >= start_of_seven_days_ago
    ).all()
    
    # Perform grouping in lightning-fast Python memory
    day_counts = {}
    for chat in recent_chats:
        day_str = chat.timestamp.strftime("%a")
        day_counts[day_str] = day_counts.get(day_str, 0) + 1
        
    for i in range(6, -1, -1):
        target_date = today - timedelta(days=i)
        day_str = target_date.strftime("%a")
        chart_data.append({
            "name": day_str,
            "queries": day_counts.get(day_str, 0)
        })
    
    return {
        "total_schemes": total_schemes,
        "total_chats": total_chats,
        "total_crawlers": total_crawlers,
        "chart_data": chart_data
    }

@router.get("/activity", dependencies=[Depends(verify_admin)])
def get_recent_activity(db: Session = Depends(get_db)):
    # Fetch recent LIVE schemes
    recent_schemes = db.query(GovernmentScheme).order_by(GovernmentScheme.id.desc()).limit(5).all()
    # Fetch recent PENDING scraped items
    recent_pending = db.query(PendingScheme).order_by(PendingScheme.id.desc()).limit(5).all()
    
    activity_timeline = []
    
    # In SQLite, the lack of explicit DateTime columns sometimes means we fall back to IDs assuming auto-increment
    # However, for a generic activity feed we'll mock the timestamp for demonstration if the schema doesn't have one,
    # or use a default past timestamp if unavailable. 
    # In our `models.py`, `GovernmentScheme` does not actually have a created_at column. 
    # For now, we will assign a mock timestamp based on ID (higher ID = newer) to simulate a timeline
    # since we cannot alter the SQLite schema dynamically without migrations.
    
    now = datetime.now()
    
    for s in recent_schemes:
        # Simulate timestamp based on ID: id 1 = 1 hour ago, id 2 = 50 mins ago, etc.
        simulated_time = now - timedelta(minutes=(100 - (s.id * 5)))
        activity_timeline.append({
            "id": f"scheme_{s.id}",
            "type": "scheme_live",
            "title": s.title,
            "message": "Published a new active scheme.",
            "timestamp": simulated_time.isoformat()
        })
        
    for p in recent_pending:
        simulated_time = now - timedelta(minutes=(110 - (p.id * 5)))
        activity_timeline.append({
            "id": f"pending_{p.id}",
            "type": "scheme_pending",
            "title": p.title,
            "message": "Crawler pushed data to Approval Queue.",
            "timestamp": simulated_time.isoformat()
        })
        
    # Sort chronologically (newest first)
    activity_timeline.sort(key=lambda x: x["timestamp"], reverse=True)
    
    return activity_timeline[:5]

# --- Government Schemes ---
class SchemeCreate(BaseModel):
    title: str
    content: str
    
class SchemeUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None

@router.get("/schemes", dependencies=[Depends(verify_admin)])
def get_schemes(db: Session = Depends(get_db)):
    schemes = db.query(GovernmentScheme).order_by(GovernmentScheme.id.desc()).all()
    # Exclude embeddings from payload to save bandwidth
    return [
        {
            "id": s.id, 
            "title": s.title, 
            "content": s.content[:100] + "..." if len(s.content) > 100 else s.content
        } for s in schemes
    ]

@router.post("/schemes", dependencies=[Depends(verify_admin)])
def create_scheme(scheme: SchemeCreate, db: Session = Depends(get_db)):
    # Automatically generate embeddings for the new scheme
    embedding = None
    if GEMINI_API_KEY:
        try:
            result = genai_client.models.embed_content(
                model="gemini-embedding-001",
                contents=scheme.content,
            )
            embedding = result.embeddings[0].values
        except Exception as e:
            print(f"Error generating embedding: {e}")
            raise HTTPException(status_code=500, detail="Failed to generate AI embeddings for the scheme")
            
    if not embedding:
        raise HTTPException(status_code=500, detail="Embedding generation returned empty")

    new_scheme = GovernmentScheme(
        title=scheme.title,
        content=scheme.content,
        embedding=embedding
    )
    db.add(new_scheme)
    db.commit()
    db.refresh(new_scheme)
    
    return {
        "id": new_scheme.id, 
        "title": new_scheme.title, 
        "message": "Scheme created and embedded successfully"
    }

@router.delete("/schemes/{scheme_id}", dependencies=[Depends(verify_admin)])
def delete_scheme(scheme_id: int, db: Session = Depends(get_db)):
    scheme = db.query(GovernmentScheme).filter(GovernmentScheme.id == scheme_id).first()
    if not scheme:
        raise HTTPException(status_code=404, detail="Scheme not found")
    
    db.delete(scheme)
    db.commit()
    return {"message": "Scheme deleted successfully"}

# --- Pending Schemes (Approval Queue) ---
class PendingSchemeApprove(BaseModel):
    title: str
    content: str

@router.get("/pending-schemes", dependencies=[Depends(verify_admin)])
def get_pending_schemes(db: Session = Depends(get_db)):
    pending = db.query(PendingScheme).order_by(PendingScheme.id.desc()).all()
    return pending

@router.put("/pending-schemes/{pending_id}/approve", dependencies=[Depends(verify_admin)])
def approve_pending_scheme(pending_id: int, scheme_data: PendingSchemeApprove, db: Session = Depends(get_db)):
    pending = db.query(PendingScheme).filter(PendingScheme.id == pending_id).first()
    if not pending:
        raise HTTPException(status_code=404, detail="Pending scheme not found")
        
    # Generate Gemini embedding only on approval
    embedding = None
    if GEMINI_API_KEY:
        try:
            result = genai_client.models.embed_content(
                model="gemini-embedding-001",
                contents=scheme_data.content,
            )
            embedding = result.embeddings[0].values
        except Exception as e:
            print(f"Error generating embedding: {e}")
            raise HTTPException(status_code=500, detail="Failed to generate AI embeddings for the scheme")
            
    if not embedding:
        raise HTTPException(status_code=500, detail="Embedding generation returned empty")

    new_scheme = GovernmentScheme(
        title=scheme_data.title,
        content=scheme_data.content,
        embedding=embedding
    )
    db.add(new_scheme)
    db.delete(pending) # Remove from pending queue
    db.commit()
    db.refresh(new_scheme)
    
    return {
        "id": new_scheme.id, 
        "title": new_scheme.title, 
        "message": "Scheme approved, embedded, and published successfully"
    }

@router.delete("/pending-schemes/{pending_id}", dependencies=[Depends(verify_admin)])
def reject_pending_scheme(pending_id: int, db: Session = Depends(get_db)):
    pending = db.query(PendingScheme).filter(PendingScheme.id == pending_id).first()
    if not pending:
        raise HTTPException(status_code=404, detail="Pending scheme not found")
    
    db.delete(pending)
    db.commit()
    return {"message": "Scheme rejected and deleted locally"}

# --- Crawler Sources ---
class CrawlerCreate(BaseModel):
    url: str
    description: Optional[str] = None

class CrawlerUpdate(BaseModel):
    is_active: bool

@router.get("/crawlers", dependencies=[Depends(verify_admin)])
def get_crawlers(db: Session = Depends(get_db)):
    crawlers = db.query(CrawlerSource).order_by(CrawlerSource.id.desc()).all()
    return crawlers

@router.post("/crawlers", dependencies=[Depends(verify_admin)])
def create_crawler(crawler: CrawlerCreate, db: Session = Depends(get_db)):
    existing = db.query(CrawlerSource).filter(CrawlerSource.url == crawler.url).first()
    if existing:
        raise HTTPException(status_code=400, detail="URL already exists")
    
    new_crawler = CrawlerSource(
        url=crawler.url,
        description=crawler.description,
        is_active=True
    )
    db.add(new_crawler)
    db.commit()
    db.refresh(new_crawler)
    return new_crawler

@router.put("/crawlers/{crawler_id}", dependencies=[Depends(verify_admin)])
def update_crawler(crawler_id: int, update_data: CrawlerUpdate, db: Session = Depends(get_db)):
    crawler = db.query(CrawlerSource).filter(CrawlerSource.id == crawler_id).first()
    if not crawler:
        raise HTTPException(status_code=404, detail="Crawler not found")
    
    crawler.is_active = update_data.is_active
    db.commit()
    db.refresh(crawler)
    return crawler

@router.delete("/crawlers/{crawler_id}", dependencies=[Depends(verify_admin)])
def delete_crawler(crawler_id: int, db: Session = Depends(get_db)):
    crawler = db.query(CrawlerSource).filter(CrawlerSource.id == crawler_id).first()
    if not crawler:
        raise HTTPException(status_code=404, detail="Crawler not found")
    
    db.delete(crawler)
    db.commit()
    return {"message": "Crawler deleted successfully"}

@router.post("/crawlers/{crawler_id}/run", dependencies=[Depends(verify_admin)])
def run_crawler_manual(crawler_id: int, db: Session = Depends(get_db)):
    target = db.query(CrawlerSource).filter(CrawlerSource.id == crawler_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Crawler source not found")
        
    try:
        raw_html_text = scrape_website(target.url)
        if not raw_html_text:
            raise HTTPException(status_code=500, detail="Failed to scrape website content.")
            
        clean_scheme = extract_scheme_with_groq(raw_html_text)
        if clean_scheme == "SKIP" or "SKIP" in clean_scheme.upper():
            return {"message": "Groq determined this page contained no useful scheme data.", "status": "skipped"}
            
        title = clean_scheme.split("Title:")[1].split("Details:")[0].strip() if "Title:" in clean_scheme else f"Auto-Scraped: {target.url}"
        
        # Check for duplicates in both locations
        existing_live = db.query(GovernmentScheme).filter(GovernmentScheme.title == title).first()
        existing_pending = db.query(PendingScheme).filter(PendingScheme.title == title).first()
        
        if existing_live or existing_pending:
            return {"message": f"Scheme '{title}' already exists or is pending.", "status": "duplicate"}
            
        new_pending = PendingScheme(
            title=title,
            content=clean_scheme,
            source_url=target.url
        )
        db.add(new_pending)
        db.commit()
        return {"message": f"Successfully scraped '{title}' and moved it to the Approval Queue!", "status": "success"}
        
    except Exception as e:
        print(f"Manual Crawler Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# --- Chat Logs ---
@router.delete("/chats/cleanup", dependencies=[Depends(verify_admin)])
def force_chats_cleanup(days: int = 23, db: Session = Depends(get_db)):
    cutoff_date = datetime.now() - timedelta(days=days)
    deleted_count = db.query(ChatLog).filter(ChatLog.timestamp < cutoff_date).delete()
    db.commit()
    return {"message": f"Successfully deleted {deleted_count} logs older than {days} days."}

@router.get("/chats", dependencies=[Depends(verify_admin)])
def get_chats(limit: int = 50, db: Session = Depends(get_db)):
    chats = db.query(ChatLog).order_by(ChatLog.timestamp.desc()).limit(limit).all()
    
    # Process chats to flag unanswered queries
    # These are common phrases Gemini outputs when context is missing
    failure_keywords = [
        "i don't have information",
        "i do not have information",
        "i don't know",
        "i cannot find",
        "i couldn't find",
        "not mentioned in the provided context",
        "does not contain information"
    ]
    
    results = []
    for chat in chats:
        is_unanswered = any(keyword in chat.ai_response.lower() for keyword in failure_keywords)
        results.append({
            "id": chat.id,
            "user_query": chat.user_message,
            "bot_response": chat.ai_response,
            "timestamp": chat.timestamp,
            "is_unanswered": is_unanswered
        })
        
    return results

# --- Reporting ---
@router.get("/reports/generate", dependencies=[Depends(verify_admin)])
def generate_report(db: Session = Depends(get_db)):
    # 1. Gather Data
    total_schemes = db.query(func.count(GovernmentScheme.id)).scalar()
    total_pending = db.query(func.count(PendingScheme.id)).scalar()
    total_crawlers = db.query(func.count(CrawlerSource.id)).scalar()
    
    chats = db.query(ChatLog).all()
    total_queries = len(chats)
    
    failure_keywords = [
        "i don't have information", "i do not have information", "i don't know",
        "i cannot find", "i couldn't find", "not mentioned", "does not contain information"
    ]
    unanswered_count = sum(1 for c in chats if any(k in c.ai_response.lower() for k in failure_keywords))
    
    # 2. Draw PDF in Memory (RAM)
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    
    # Color Palette
    PRIMARY = (0.1, 0.2, 0.5)      # Deep Blue
    SECONDARY = (0.2, 0.4, 0.8)    # Lighter Blue
    GRAY = (0.3, 0.3, 0.3)
    LIGHT_GRAY = (0.9, 0.9, 0.9)
    RED = (0.8, 0.1, 0.1)
    GREEN = (0.1, 0.6, 0.2)
    
    # --- HEADER SECTION ---
    # Top Action Bar
    p.setFillColorRGB(*PRIMARY)
    p.rect(0, height - 60, width, 60, fill=1, stroke=0)
    
    p.setFillColorRGB(1, 1, 1) # White Text
    p.setFont("Helvetica-Bold", 24)
    p.drawString(40, height - 40, "GujGov Mitra")
    
    p.setFont("Helvetica", 12)
    p.drawString(40, height - 55, "Corporate Analytics & AI Performance Report")
    
    # Generated Date Right Aligned
    report_date = f"Generated On: {datetime.now().strftime('%d %B, %Y %I:%M %p')}"
    p.drawRightString(width - 40, height - 35, report_date)
    
    # --- EXECUTIVE SUMMARY ---
    y_pos = height - 100
    p.setFillColorRGB(*PRIMARY)
    p.setFont("Helvetica-Bold", 16)
    p.drawString(40, y_pos, "1. Executive Summary")
    
    # Draw a divider
    p.setStrokeColorRGB(*SECONDARY)
    p.setLineWidth(1)
    p.line(40, y_pos - 10, width - 40, y_pos - 10)
    
    # Draw Metric Boxes
    box_width = 160
    box_height = 70
    spacing = 20
    
    # Helper to draw metric cards
    def draw_metric_card(x, y, title, value, val_color=PRIMARY):
        p.setFillColorRGB(*LIGHT_GRAY)
        p.setStrokeColorRGB(0.8, 0.8, 0.8)
        p.roundRect(x, y - box_height, box_width, box_height, 6, fill=1, stroke=1)
        
        p.setFillColorRGB(*GRAY)
        p.setFont("Helvetica", 10)
        p.drawString(x + 10, y - 20, title)
        
        p.setFillColorRGB(*val_color)
        p.setFont("Helvetica-Bold", 28)
        p.drawString(x + 10, y - 55, str(value))

    y_pos -= 30
    draw_metric_card(40, y_pos, "Total Scheme DB Size", total_schemes, PRIMARY)
    draw_metric_card(40 + box_width + spacing, y_pos, "Citizen Queries", total_queries, SECONDARY)
    
    failure_rate_str = f"{(unanswered_count / total_queries * 100):.1f}%" if total_queries > 0 else "0%"
    failure_color = RED if total_queries > 0 and (unanswered_count / total_queries * 100) > 10 else GREEN
    draw_metric_card(40 + (box_width + spacing) * 2, y_pos, "AI Error Rate", failure_rate_str, failure_color)
    
    # --- SYSTEM HEALTH ---
    y_pos -= (box_height + 40)
    p.setFillColorRGB(*PRIMARY)
    p.setFont("Helvetica-Bold", 16)
    p.drawString(40, y_pos, "2. System Health & Infrastructure")
    
    p.setStrokeColorRGB(*SECONDARY)
    p.setLineWidth(1)
    p.line(40, y_pos - 10, width - 40, y_pos - 10)
    
    y_pos -= 40
    p.setFillColorRGB(*GRAY)
    p.setFont("Helvetica", 12)
    
    p.drawString(40, y_pos, "• Live Web Crawlers Running:")
    p.setFont("Helvetica-Bold", 12)
    p.drawString(250, y_pos, f"{total_crawlers} Active Services")
    
    y_pos -= 25
    p.setFont("Helvetica", 12)
    p.drawString(40, y_pos, "• Documents in QA Approval Queue:")
    p.setFont("Helvetica-Bold", 12)
    p.drawString(250, y_pos, f"{total_pending} Warnings")
    
    # --- CHATBOT PERFORMANCE ANALYSIS ---
    y_pos -= 50
    p.setFillColorRGB(*PRIMARY)
    p.setFont("Helvetica-Bold", 16)
    p.drawString(40, y_pos, "3. AI Chatbot Performance Breakdown")
    
    p.setStrokeColorRGB(*SECONDARY)
    p.setLineWidth(1)
    p.line(40, y_pos - 10, width - 40, y_pos - 10)
    
    y_pos -= 40
    p.setFillColorRGB(*GRAY)
    p.setFont("Helvetica", 12)
    
    successful_queries = total_queries - unanswered_count
    
    p.drawString(40, y_pos, "• Successfully Answered Queries:")
    p.setFillColorRGB(*GREEN)
    p.setFont("Helvetica-Bold", 12)
    p.drawString(250, y_pos, f"{successful_queries}")
    
    y_pos -= 25
    p.setFillColorRGB(*GRAY)
    p.setFont("Helvetica", 12)
    p.drawString(40, y_pos, "• Failed/Missing Context Queries:")
    p.setFillColorRGB(*RED)
    p.setFont("Helvetica-Bold", 12)
    p.drawString(250, y_pos, f"{unanswered_count}")
    
    y_pos -= 40
    p.setFillColorRGB(*GRAY)
    p.setFont("Helvetica-Oblique", 10)
    warning_text = "* Note: If 'Failed Queries' is high, admins should review the 'Chat Logs' tab and run Crawlers on missing topics."
    p.drawString(40, y_pos, warning_text)
    
    # Footer
    p.setFillColorRGB(*GRAY)
    p.setFont("Helvetica", 9)
    p.drawCentredString(width / 2, 30, "Generated automatically by GujGov Mitra Admin System • internal strict confidentiality")
    
    # Finalize PDF
    p.showPage()
    p.save()
    
    # 3. Stream File without Saving
    buffer.seek(0)
    return StreamingResponse(
        buffer, 
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=GujGov_Report_{datetime.now().strftime('%Y%m%d')}.pdf"}
    )
