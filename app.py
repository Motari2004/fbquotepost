import os
import requests
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI
from dotenv import load_dotenv

# Load .env file for local development
load_dotenv()

app = FastAPI()

# --- HELPERS ---
def get_env(key):
    return os.getenv(key)

def get_db_connection():
    url = get_env("DATABASE_URL")
    if not url: 
        raise Exception("DATABASE_URL is missing from environment variables")
    
    # Fix Neon protocol for psycopg2
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgres://", 1)
    
    try:
        # MANDATORY for Neon: sslmode='require'
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
        
        # Create tables if they don't exist
        cur.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value INTEGER)")
        cur.execute("CREATE TABLE IF NOT EXISTS topics (id SERIAL PRIMARY KEY, name TEXT UNIQUE)")
        cur.execute("INSERT INTO bot_state (key, value) VALUES ('pointer', 0) ON CONFLICT DO NOTHING")
        
        # Seed Topics if empty
        cur.execute("SELECT COUNT(*) as total FROM topics")
        total = cur.fetchone()['total']
        
        if total == 0:
            topics_list = [('resilience',), ('discipline',), ('focus',), ('growth',), ('mindset',), ('consistency',)]
            cur.executemany("INSERT INTO topics (name) VALUES (%s)", topics_list)
            conn.commit()
            cur.execute("SELECT COUNT(*) as total FROM topics")
            total = cur.fetchone()['total']
        
        # Get the current pointer
        cur.execute("SELECT value FROM bot_state WHERE key = 'pointer'")
        res = cur.fetchone()
        pointer = res['value'] if res else 0
        
        # Calculate actual topic index
        actual_id = (pointer % total) + 1
        
        cur.execute("SELECT name FROM topics WHERE id = %s", (actual_id,))
        topic_row = cur.fetchone()
        
        if not topic_row:
            raise Exception(f"Topic with ID {actual_id} not found in database")
            
        topic = topic_row['name']
        
        # Increment pointer
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
    # Updated to a valid stable model name
    model = genai.GenerativeModel("gemini-3.1-flash-lite")
    
    caption = model.generate_content(f"Powerful short Facebook quote about {topic}. 1 sentence.").text.strip()
    kw_prompt = f"3 simple English keywords for Unsplash photo for: '{caption}'. Comma separated only."
    keywords = model.generate_content(kw_prompt).text.strip()
    
    unsplash_key = get_env("UNSPLASH_ACCESS_KEY")
    img_url = "https://images.unsplash.com/photo-1497567149193-57567572f26f"
    if unsplash_key:
        try:
            res = requests.get(f"https://api.unsplash.com/photos/random?query={keywords}&client_id={unsplash_key}&orientation=squarish", timeout=10)
            img_url = res.json().get("urls", {}).get("regular", img_url)
        except: pass
            
    return caption, img_url

def post_to_buffer(text, image_url):
    token = get_env("BUFFER_ACCESS_TOKEN")
    profile_id = get_env("BUFFER_PROFILE_ID")
    if not token or not profile_id: raise Exception("Buffer credentials missing")

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
    return {"status": "Bot Active", "endpoint": "/cron-post"}

@app.get("/cron-post")
def cron():
    try:
        topic = get_sequential_topic()
        caption, img = get_ai_content(topic)
        post_to_buffer(caption, img)
        return {"status": "Success", "topic": topic, "caption": caption, "image": img}
    except Exception as e:
        return {"status": "Error", "detail": str(e)}

# Local execution only
if __name__ == "__main__":
    import uvicorn
    print("🚀 Starting local server on port 10000...")
    uvicorn.run(app, host="0.0.0.0", port=10000)
