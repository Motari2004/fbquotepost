import os
import requests
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv
import uvicorn # Import uvicorn to run from within python

# Load .env file for local development (ignored on Vercel)
load_dotenv()

app = FastAPI()

# --- HELPERS ---
def get_env(key):
    return os.getenv(key)

def get_db_connection():
    url = get_env("DATABASE_URL")
    if not url: 
        raise Exception("DATABASE_URL missing from environment")
    
    # Fix for Neon DB / Vercel compatibility
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgres://", 1)
    
    try:
        # sslmode='require' is mandatory for Neon/Postgres on Vercel
        return psycopg2.connect(
            url, 
            connect_timeout=10, 
            sslmode='require'
        )
    except Exception as e:
        raise Exception(f"Database Connection Error: {str(e)}")

# --- DATABASE LOGIC ---
def get_sequential_topic():
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # Setup Tables
        cur.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value INTEGER)")
        cur.execute("CREATE TABLE IF NOT EXISTS topics (id SERIAL PRIMARY KEY, name TEXT UNIQUE)")
        cur.execute("INSERT INTO bot_state (key, value) VALUES ('pointer', 0) ON CONFLICT DO NOTHING")
        
        # Seed Topics if empty
        cur.execute("SELECT COUNT(*) FROM topics")
        if cur.fetchone()[0] == 0:
            topics = [('resilience',), ('discipline',), ('focus',), ('growth',), ('mindset',), ('consistency',)]
            cur.executemany("INSERT INTO topics (name) VALUES (%s)", topics)
        
        conn.commit()

        # Get current topic based on pointer
        cur.execute("SELECT value FROM bot_state WHERE key = 'pointer'")
        pointer = cur.fetchone()['value']
        cur.execute("SELECT COUNT(*) as total FROM topics")
        total = cur.fetchone()['total']
        
        actual_index = (pointer % total) + 1
        cur.execute("SELECT name FROM topics WHERE id = %s", (actual_index,))
        topic = cur.fetchone()['name']
        
        # Increment pointer for next time
        cur.execute("UPDATE bot_state SET value = value + 1 WHERE key = 'pointer'")
        conn.commit()
        cur.close()
        conn.close()
        return topic
    except Exception as e:
        raise Exception(f"DB Sequence Error: {str(e)}")

# --- CONTENT LOGIC ---
def get_ai_content(topic):
    api_key = get_env("GEMINI_API_KEY")
    if not api_key: 
        raise Exception("GEMINI_API_KEY missing")
    
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")
    
    # 1. Generate Caption
    caption = model.generate_content(f"Powerful short Facebook quote about {topic}. 1 sentence.").text.strip()
    
    # 2. Generate Image Keywords
    kw_prompt = f"3 simple English keywords for Unsplash photo for: '{caption}'. Comma separated only."
    keywords = model.generate_content(kw_prompt).text.strip()
    
    # 3. Fetch Image from Unsplash
    unsplash_key = get_env("UNSPLASH_ACCESS_KEY")
    img_url = "https://images.unsplash.com/photo-1497567149193-57567572f26f" # Fallback
    if unsplash_key:
        try:
            res = requests.get(f"https://api.unsplash.com/photos/random?query={keywords}&client_id={unsplash_key}&orientation=squarish", timeout=10)
            img_url = res.json().get("urls", {}).get("regular", img_url)
        except: 
            pass
            
    return caption, img_url

def post_to_buffer(text, image_url):
    token = get_env("BUFFER_ACCESS_TOKEN")
    profile_id = get_env("BUFFER_PROFILE_ID")
    if not token or not profile_id: 
        raise Exception("Buffer credentials (token or profile_id) missing")

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
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
    required = ["GEMINI_API_KEY", "DATABASE_URL", "BUFFER_ACCESS_TOKEN", "BUFFER_PROFILE_ID", "UNSPLASH_ACCESS_KEY"]
    missing = [k for k in required if not get_env(k)]
    if missing:
        return {"status": "Configuration Error", "missing_variables": missing}
    return {"status": "Bot Active", "endpoint": "/cron-post"}

@app.get("/cron-post")
def cron():
    try:
        topic = get_sequential_topic()
        caption, img = get_ai_content(topic)
        post_to_buffer(caption, img)
        return {
            "status": "Success", 
            "topic": topic, 
            "caption": caption, 
            "image": img
        }
    except Exception as e:
        return {"status": "Error", "detail": str(e)}

# --- RUNNER BLOCK ---
# This part only runs when you do 'python app.py'
if __name__ == "__main__":
    print("Starting server on port 10000...")
    uvicorn.run(app, host="0.0.0.0", port=10000)
