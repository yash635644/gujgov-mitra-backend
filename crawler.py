import os
import time
import requests
import urllib3
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Suppress insecure request warnings for government sites with bad SSL certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from google import genai
from groq import Groq
from sqlalchemy.orm import Session

# Import our DB setup and models
from database import SessionLocal
from models import GovernmentScheme, CrawlerSource, PendingScheme

load_dotenv()

# Setup AI Clients
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_API_KEY)

def scrape_website(url: str):
    """
    Scrapes the target URL provided by the database.
    """
    print(f"🌐 Crawling {url}...")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    try:
        # verify=False prevents SSL crashes on portals with misconfigured certificates 
        response = requests.get(url, headers=headers, timeout=15, verify=False)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        paragraphs = soup.find_all(['p', 'div', 'span', 'li'])
        
        raw_text = ""
        for p in paragraphs:
            text = p.get_text(strip=True)
            if len(text) > 40:
                raw_text += text + "\n"
        
        return raw_text[:3000] if len(raw_text) > 3000 else raw_text
        
    except Exception as e:
        print(f"❌ Scraping failed for {url}: {e}")
        return None

def extract_scheme_with_groq(raw_text: str):
    """
    Sends messy HTML text to Groq (Llama 3) to pull out a beautifully formatted Scheme string.
    """
    print("🧠 Utilizing Groq to extract clean scheme patterns...")
    
    prompt = f"""
    You are an AI data extractor. Read the following messy scraped text from a government website.
    If you find ANY mentions of a government service, scheme, certificate, or process, extract it and format it EXACTLY like this:
    
    Title: [Name of Scheme]
    Details: 
    ✅ What You Need to Do: [Summary]
    📋 Required Documents: [List]
    🌐 Official Portal: [URL if found]
    
    If the text is just random website UI (login buttons, footer menus) and contains no actual scheme, ONLY reply with the exact word "SKIP".
    
    Scraped Text:
    {raw_text}
    """
    
    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            temperature=0.1,
            max_tokens=1000,
        )
        content = chat_completion.choices[0].message.content.strip()
        return content
    except Exception as e:
        print(f"❌ Groq Extraction Error: {e}")
        return "SKIP"

def get_embedding_with_gemini_safely(text: str):
    """
    Turns the clean text into a 3072D array math vector using Gemini.
    """
    print("📐 Generating 3072D Vector with Gemini...")
    try:
        result = genai_client.models.embed_content(
            model="gemini-embedding-001",
            contents=text,
        )
        return result.embeddings[0].values
    except Exception as e:
        print(f"❌ Gemini Embedding Error: {e}")
        return None

def run_crawler():
    print("🚀 Starting Daily Government Website Crawler...")
    db = SessionLocal()
    
    # Fetch all active target URLs from the database
    targets = db.query(CrawlerSource).filter(CrawlerSource.is_active == True).all()
    
    if not targets:
        print("⚠️ No active crawler sources found in database. Exiting.")
        db.close()
        return
        
    print(f"📋 Found {len(targets)} active sources to crawl.")
    
    for target in targets:
        print(f"\n--- Processing: {target.description or target.url} ---")
        
        # 1. Scrape the website
        raw_html_text = scrape_website(target.url)
        
        if not raw_html_text:
             continue
    
        # 2. Use Groq to parse it
        clean_scheme = extract_scheme_with_groq(raw_html_text)
        
        if clean_scheme == "SKIP" or "SKIP" in clean_scheme.upper():
            print("⏭️ Groq determined this page contained no useful scheme data. Skipping.")
            continue
            
        print(f"✨ Groq Extracted Data:\n{clean_scheme}\n")
        
        # 3. Save to PendingQueue instead of embedding directly
        title = clean_scheme.split("Title:")[1].split("Details:")[0].strip() if "Title:" in clean_scheme else f"Auto-Scraped Scheme: {target.url}"
        
        # 4. Check for duplicates in both live and pending
        existing_live = db.query(GovernmentScheme).filter(GovernmentScheme.title == title).first()
        existing_pending = db.query(PendingScheme).filter(PendingScheme.title == title).first()
        
        if existing_live or existing_pending:
            print(f"🛑 Scheme '{title}' already exists or is pending. Skipping.")
            continue
    
        # 5. Save to Pending DB
        print(f"💾 Saving '{title}' to Data Approval Queue...")
        new_pending = PendingScheme(
            title=title,
            content=clean_scheme,
            source_url=target.url
        )
        
        try:
            db.add(new_pending)
            db.commit()
            print("✅ Successfully added new scheme to the Approval Queue!")
        except Exception as e:
            print(f"❌ Failed to save to pending queue: {e}")
            db.rollback()
            
    db.close()
    print("\n🏁 Daily Crawl Finished!")

if __name__ == "__main__":
    run_crawler()
