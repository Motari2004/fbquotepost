import os
import requests
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException, Header
from dotenv import load_dotenv

# Load .env file for local development
load_dotenv()

app = FastAPI()

# --- CONFIGURATION ---
# Add this to your Vercel Environment Variables. 
# If not set, it defaults to the string below.
CRON_SECRET = os.getenv("CRON_SECRET", "my_super_secret_key_123")

# --- HELPERS ---
def get_env(key):
    return os.getenv(key)

def get_db_connection():
    url = get_env("DATABASE_URL")
    if not url: 
        raise Exception("DATABASE_URL is missing from environment variables")
    
    # FIX for Neon/Vercel: psycopg2 requires 'postgres://', but Neon provides 'postgresql://'
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgres://", 1)
    
    try:
        # sslmode='require' is mandatory for Neon DB connections
        return psycopg2.connect(
            url, 
            connect_timeout=10, 
            sslmode='require' 
        )
    except Exception as e:
        raise Exception(f"Neon Connection Failed: {str(e)}")

# --- DATABASE LOGIC ---
def get_sequential_topic():
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # Create tables if they don't exist (Automatic Setup)
        cur.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value INTEGER)")
        cur.execute("CREATE TABLE IF NOT EXISTS topics (id SERIAL PRIMARY KEY, name TEXT UNIQUE)")
        cur.execute("INSERT INTO bot_state (key, value) VALUES ('pointer', 0) ON CONFLICT DO NOTHING")
        
        # Seed default topics if the table is empty
        cur.execute("SELECT COUNT(*) as total FROM topics")
        total = cur.fetchone()['total']
        
        if total == 0:
            topics_list = [('resilience',), ('discipline',), ('focus',), ('growth',), ('mindset',), ('consistency',)]
            cur.executemany("INSERT INTO topics (name) VALUES (%s)", topics_list)
            conn.commit()
            cur.execute("SELECT COUNT(*) as total FROM topics")
            total = cur.fetchone()['total']
        
        # Get the current pointer (where we left off)
        cur.execute("SELECT value FROM bot_state WHERE key = 'pointer'")
        res = cur.fetchone()
        pointer = res['value'] if res else 0
        
        # Calculate the actual topic ID (modulo allows looping back to start)
        actual_id = (pointer % total) + 1
        
        cur.execute("SELECT name FROM topics WHERE id = %s", (actual_id,))
        topic_row = cur.fetchone()
        
        if not topic_row:
            raise Exception(f"Topic with ID {actual_id} not found in database")
            
        topic = topic_row['name']
        
        # Increment pointer for the next run
        cur.execute("UPDATE bot_state SET value = value + 1 WHERE key = 'pointer'")
        conn.commit()
        
        cur.close()
        return topic
        
    except Exception as e:
        raise Exception(f"DB Error: {str(e)}")
    finally:
        if conn:
            conn.close()

# --- CONTENT LOGIC ---
def get_ai_content(topic):
    api_key = get_env("GEMINI_API_KEY")
    if not api_key: raise Exception("GEMINI_API_KEY missing")
    
    genai.configure(api_key=api_key)
    # Using gemini-1.5-flash: fast, efficient, and reliable
    model = genai.GenerativeModel("gemini-1.5-flash")
    
    # 1. Generate a high-impact quote
    caption_res = model.generate_content(f"Write a powerful, short, one-sentence Facebook quote about {topic}. No hashtags.")
    caption = caption_res.text.strip()
    
    # 2. Generate keywords for Unsplash image search
    kw_prompt = f"Provide 3 simple English keywords for a high-quality Unsplash photo that matches this quote: '{caption}'. Comma separated only."
    kw_res = model.generate_content(kw_prompt)
    keywords = kw_res.text.strip()
    
    # 3. Fetch random image from Unsplash
    unsplash_key = get_env("UNSPLASH_ACCESS_KEY")
    img_url = "https://images.unsplash.com/photo-1497567149193-57567572f26f" # Hardcoded fallback image
    if unsplash_key:
        try:
            res = requests.get(
                f"https://api.unsplash.com/photos/random?query={keywords}&client_id={unsplash_key}&orientation=squarish", 
                timeout=10
            )
            img_url = res.json().get("urls", {}).get("regular", img_url)
        except: 
            pass # Use fallback if Unsplash fails
            
    return caption, img_url

def post_to_buffer(text, image_url):
    token = get_env("BUFFER_ACCESS_TOKEN")
    profile_id = get_env("BUFFER_PROFILE_ID")
    if not token or not profile_id: raise Exception("Buffer credentials missing")

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    # Buffer GraphQL Mutation for immediate posting
    query = "mutation CreateImmediatePost($input: CreatePostInput!) { createPost(input: $input) { ... on PostActionSuccess { post { id } } } }"
    variables = {
        "input": {
            "channelId": profile_id, 
            "text": text, 
            "schedulingType": "automatic", 
            "mode": "shareNow", 
            "assets": [{"externalUrl": image_url}],
            "metadata": {"facebook": {"type": "post"}}
        }
    }
    
    res = requests.post("https://api.buffer.com", json={"query": query, "variables": variables}, headers=headers, timeout=10)
    if res.status_code != 200:
        raise Exception(f"Buffer API Error: {res.text}")

# --- ROUTES ---

@app.get("/")
def home():
    return {
        "status": "Bot Active", 
        "message": "Your quote bot is running. Trigger the post via /cron-post"
    }

@app.get("/cron-post")
def cron(authorization: str = Header(None)):
    # SECURITY: Only allow requests that match your CRON_SECRET
    # Vercel Cron Jobs can send this in the Header
    if authorization != CRON_SECRET:
        # For local testing, if you didn't set a secret, we allow it.
        if CRON_SECRET != "my_super_secret_key_123":
            raise HTTPException(status_code=403, detail="Unauthorized: Secret key mismatch")

    try:
        # 1. Get topic from Neon DB
        topic = get_sequential_topic()
        
        # 2. Get AI Content and Image
        caption, img = get_ai_content(topic)
        
        # 3. Post to Facebook via Buffer
        post_to_buffer(caption, img)
        
        return {
            "status": "Success", 
            "topic": topic, 
            "caption": caption, 
            "image": img
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Local Execution block
if __name__ == "__main__":
    import uvicorn
    print("🚀 Starting local server on http://127.0.0.1:10000")
    uvicorn.run(app, host="0.0.0.0", port=10000)
