import os
import random
import requests
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

# Gemini AI Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Buffer & Unsplash Config
BUFFER_TOKEN = os.getenv("BUFFER_ACCESS_TOKEN")
BUFFER_PROFILE_ID = os.getenv("BUFFER_PROFILE_ID")
UNSPLASH_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

class PromptRequest(BaseModel):
    topic: str

class PostRequest(BaseModel):
    caption: str
    image_url: str

# --- NEON DATABASE LOGIC ---

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    """Creates the state table and seeds topics if they don't exist."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Create table for the pointer
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value INTEGER
        );
    """)
    
    # Create table for topics
    cur.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE
        );
    """)
    
    # Seed default topics if table is empty
    cur.execute("SELECT COUNT(*) FROM topics")
    if cur.fetchone()[0] == 0:
        default_topics = [
            ('resilience',), ('discipline',), ('focus',), ('growth',), 
            ('mindset',), ('consistency',), ('ambition',), ('hard work',), 
            ('gratitude',), ('self-belief',), ('courage',), ('persistence',)
        ]
        cur.executemany("INSERT INTO topics (name) VALUES (%s)", default_topics)
    
    # Initialize pointer to 0
    cur.execute("INSERT INTO bot_state (key, value) VALUES ('pointer', 0) ON CONFLICT DO NOTHING")
    
    conn.commit()
    cur.close()
    conn.close()

def get_sequential_topic():
    """Fetches topic from Neon DB and increments the pointer."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # 1. Get current pointer
    cur.execute("SELECT value FROM bot_state WHERE key = 'pointer'")
    pointer = cur.fetchone()['value']
    
    # 2. Get total topics count
    cur.execute("SELECT COUNT(*) as total FROM topics")
    total = cur.fetchone()['total']
    
    # 3. Calculate index and fetch topic
    actual_index = (pointer % total) + 1 # PostgreSQL IDs start at 1
    cur.execute("SELECT name FROM topics WHERE id = %s", (actual_index,))
    topic = cur.fetchone()['name']
    
    # 4. Increment pointer for next time
    cur.execute("UPDATE bot_state SET value = value + 1 WHERE key = 'pointer'")
    
    conn.commit()
    cur.close()
    conn.close()
    return topic

# --- AI EYE LOGIC ---

def get_image_keywords(caption):
    try:
        prompt = f"Based on this quote: '{caption}', provide 3 simple English keywords for a high-quality Unsplash photo search. Return ONLY the keywords separated by commas."
        response = model.generate_content(prompt)
        return response.text.strip()
    except:
        return "motivational, nature"

def fetch_unsplash_image(keywords):
    unsplash_url = "https://api.unsplash.com/photos/random"
    params = {"query": keywords, "client_id": UNSPLASH_KEY, "orientation": "squarish"}
    try:
        img_res = requests.get(unsplash_url, params=params, timeout=10)
        if img_res.status_code == 200:
            return img_res.json().get("urls", {}).get("regular")
    except:
        pass
    return "https://images.unsplash.com/photo-1497567149193-57567572f26f"

# --- BUFFER GRAPHQL POST ---

def post_to_buffer_graphql(text, image_url):
    url = "https://api.buffer.com"
    headers = {
        "Authorization": f"Bearer {BUFFER_TOKEN}",
        "Content-Type": "application/json"
    }
    
    query = """
    mutation CreateImmediatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess { post { id } }
        ... on MutationError { message }
      }
    }
    """
    
    variables = {
        "input": {
            "channelId": BUFFER_PROFILE_ID,
            "text": text,
            "schedulingType": "automatic", 
            "mode": "shareNow",              
            "assets": [{"externalUrl": image_url}],
            "metadata": {"facebook": {"type": "post"}}
        }
    }
    
    payload = {"query": query, "variables": variables}
    response = requests.post(url, json=payload, headers=headers)
    return response.json()

# --- ENDPOINTS ---

@app.on_event("startup")
async def startup_event():
    init_db()

@app.get("/cron-post")
async def cron_post():
    try:
        topic = get_sequential_topic()
        prompt = f"Write a unique, powerful motivational Facebook quote about {topic}. No hashtags, just 1-2 sentences."
        caption = model.generate_content(prompt).text.strip()

        keywords = get_image_keywords(caption)
        image_url = fetch_unsplash_image(keywords)

        result = post_to_buffer_graphql(caption, image_url)
        data = result.get("data", {}).get("createPost", {})
        
        if "message" in data:
            raise HTTPException(status_code=500, detail=f"Buffer Error: {data['message']}")
        
        if not data.get("post"):
            raise HTTPException(status_code=500, detail=f"API Error: {result.get('errors')}")

        return {"status": "success", "topic": topic, "caption": caption, "image": image_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/post")
async def manual_post(req: PostRequest):
    try:
        result = post_to_buffer_graphql(req.caption, req.image_url)
        data = result.get("data", {}).get("createPost", {})
        if "message" in data:
            raise HTTPException(status_code=500, detail=f"Buffer Error: {data['message']}")
        return {"status": "success", "post_id": data["post"]["id"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/generate")
async def generate_content(req: PromptRequest):
    try:
        prompt = f"Write a short, powerful motivational Facebook quote about {req.topic}. No hashtags, just 1-2 sentences."
        caption = model.generate_content(prompt).text.strip()
        keywords = get_image_keywords(caption)
        image_url = fetch_unsplash_image(keywords)
        return {"caption": caption, "image_url": image_url}
    except:
        return {"caption": "Success is a journey.", "image_url": "https://images.unsplash.com/photo-1497567149193-57567572f26f"}

@app.get("/")
async def root():
    return {"message": "Bot is running on Vercel with Neon DB. Use /cron-post to trigger."}
