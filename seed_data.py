import os
import re
import google.generativeai as genai
from database import SessionLocal
from models import GovernmentScheme
from system_prompt import SYSTEM_PROMPT
from dotenv import load_dotenv

load_dotenv()

genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

def get_embedding(text: str):
    result = genai.embed_content(
        model="models/gemini-embedding-001",
        content=text,
        task_type="retrieval_document",
    )
    return result['embedding']

def seed_database():
    db = SessionLocal()
    existing_count = db.query(GovernmentScheme).count()
    if existing_count > 0:
        print(f"Database already has {existing_count} records. To re-seed, clear the table first.")
        db.close()
        return

    print("Parsing SYSTEM_PROMPT into RAG chunks...")
    
    # The prompt is consistently formatted with sections like "\n--- TITLE ---\n"
    sections = re.split(r'\n---\s+(.+?)\s+---\n', SYSTEM_PROMPT)
    
    # sections[0] will be the preamble/rules, which we skip for RAG documents
    schemes = []
    
    for i in range(1, len(sections), 2):
        title = sections[i].strip()
        content = sections[i+1].strip()
        
        # Remove any decorative borders that might have gotten captured at the end of a section
        content = content.split('═══════════════════════════════════════')[0].strip()
        
        schemes.append((title, content))

    print(f"Found {len(schemes)} unique government schemes to embed.")
    
    for title, content in schemes:
        print(f"Generating embedding vector for: {title} ...")
        full_text = f"Title: {title}\nDetails: {content}"
        
        try:
            embedding = get_embedding(full_text)
            
            scheme_record = GovernmentScheme(
                title=title,
                content=full_text,
                embedding=embedding
            )
            db.add(scheme_record)
        except Exception as e:
            print(f"Error embedding '{title}': {e}")
            
    print("Committing to Neon Database...")
    db.commit()
    db.close()
    print("Database seeded successfully with pgvector embeddings!")

if __name__ == "__main__":
    seed_database()
