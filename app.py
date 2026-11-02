import os
import requests
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

# --- CONFIGURATION HELPERS ---
def get_env(key):
    val = os.getenv(key)
    if not val:
        # This prevents a crash and allows the home page to tell us what is missing
        return None
    return val

# --- DATABASE LOGIC ---
def get_db_connection():
    db_url = get_env("DATABASE_URL")
    if not db_url:
        raise Exception("DATABASE_URL is missing from environment variables")
    # Use connect_timeout to prevent the function from hanging
    return psycopg2.connect(db_url, connect_timeout=10)

def get_sequential_topic():
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # Create tables if they don't exist
        cur.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value INTEGER)")
        cur.execute("CREATE TABLE IF NOT EXISTS topics (id SERIAL PRIMARY KEY, name TEXT UNIQUE)")
        cur.execute("INSERT INTO bot_state (key, value) VALUES ('pointer', 0) ON CONFLICT DO NOTHING")
        
        cur.execute("SELECT COUNT(*) FROM topics")
        if cur.fetchone()[0] == 0:
            topics = [('resilience',), ('discipline',), ('focus',), ('growth',), ('mindset',), ('consistency',)]
            cur.executemany("INSERT INTO topics (name) VALUES (%s)", topics)
        
        conn.commit()

        cur.execute("SELECT value FROM bot_state WHERE key = 'pointer'")
        pointer = cur.fetchone()['value']
        cur.execute("SELECT COUNT(*) as total FROM topics")
        total = cur.fetchone()['total']
        
        actual_index = (pointer % total) + 1
        cur.execute("SELECT name FROM topics WHERE id = %s", (actual_index,))
        topic = cur.fetchone()['name']
        
        cur.execute("UPDATE bot_state SET value = value + 1 WHERE key = 'pointer'")
        conn.commit()
        cur.close()
        conn.close()
        return topic
    except Exception as e:
        raise Exception(f"DB Error: {str(e)}")

# --- AI & CONTENT LOGIC ---
def get_ai_caption(topic):
    api_key = get_env("GEMINI_API_KEY")
    if not api_key:
        raise Exception("GEMINI_API_KEY is missing")
    
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")
    response = model.generate_content(f"Powerful short Facebook quote about {topic}. 1 sentence.")
    return response.text.strip()

def get_image(caption):
    api_key = get_env("UNSPLASH_ACCESS_KEY")
    if not api_key:
        return "https://images.unsplash.com/photo-1497567149193-57567572f26f" # Fallback image
    
    try:
        # Use Gemini to get keywords for Unsplash
        gemini_key = get_env("GEMINI_API_KEY")
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel("gemini-3.1-flash-lite")
        prompt = f"3 simple English keywords for Unsplash photo for: '{caption}'. Comma separated only."
        keywords = model.generate_content(prompt).text.strip()
        
        res = requests.get(f"https://api.unsplash.com/photos/random?query={keywords}&client_id={api_key}&orientation=squarish", timeout=10)
        return res.json().get("urls", {}).get("regular")
    except:
        return "https://images.unsplash.com/photo-1497567149193-57567572f26f"

def post_to_buffer(text, image_url):
    token = get_env("BUFFER_ACCESS_TOKEN")
    profile_id = get_env("BUFFER_PROFILE_ID")
    
    if not token or not profile_id:
        raise Exception("BUFFER_ACCESS_TOKEN or BUFFER_PROFILE_ID is missing")

    url = "https://api.buffer.com"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    query = "mutation CreateImmediatePost($input: CreatePostInput!) { createPost(input: $input) { ... on PostActionSuccess { post { id } } } }"
    variables = {
        "input": {
            "channelId": profile_id,
            "text": text,
            "schedulingType": "automatic", "mode": "shareNow",
            "assets": [{"externalUrl": image_url}],
            "metadata": {"facebook": {"type": "post"}}
        }
    }
    res = requests.post(url, json={"query": query, "variables": variables}, headers=headers, timeout=10)
    return res.json()

# --- ROUTES ---

@app.get("/")
def home():
    # This acts as a diagnostic page
    missing = []
    for key in ["GEMINI_API_KEY", "DATABASE_URL", "BUFFER_ACCESS_TOKEN", "BUFFER_PROFILE_ID", "UNSPLASH_ACCESS_KEY"]:
        if not get_env(key):
            missing.append(key)
    
    if missing:
        return {
            "status": "Configuration Error", 
            "missing_variables": missing,
            "message": "Please add these keys to Vercel Settings -> Environment Variables"
        }
    
    return {"status": "Bot Active", "message": "Everything is configured! Use /cron-post to trigger a post."}

@app.get("/cron-post")
def cron():
    try:
        topic = get_sequential_topic()
        caption = get_ai_caption(topic)
        image_url = get_image(caption)
        post_to_buffer(caption, image_url)
        return {"status": "Posted!", "topic": topic, "caption": caption, "image": image_url}
    except Exception as e:
        return {"status": "Error", "detail": str(e)}
