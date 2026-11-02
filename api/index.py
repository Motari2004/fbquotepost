import os
import random
import requests
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Setup
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

app = FastAPI()

BUFFER_TOKEN = os.getenv("BUFFER_ACCESS_TOKEN")
BUFFER_PROFILE_ID = os.getenv("BUFFER_PROFILE_ID")
UNSPLASH_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

class PromptRequest(BaseModel):
    topic: str

# --- DATABASE ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def get_sequential_topic():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Create tables if they don't exist (Self-Healing)
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

# --- AI & IMAGE ---
def get_image(caption):
    try:
        prompt = f"3 simple English keywords for Unsplash photo for: '{caption}'. Comma separated only."
        keywords = model.generate_content(prompt).text.strip()
        res = requests.get(f"https://api.unsplash.com/photos/random?query={keywords}&client_id={UNSPLASH_KEY}&orientation=squarish")
        return res.json().get("urls", {}).get("regular")
    except:
        return "https://images.unsplash.com/photo-1497567149193-57567572f26f"

def post_to_buffer(text, image_url):
    url = "https://api.buffer.com"
    headers = {"Authorization": f"Bearer {BUFFER_TOKEN}", "Content-Type": "application/json"}
    query = "mutation CreateImmediatePost($input: CreatePostInput!) { createPost(input: $input) { ... on PostActionSuccess { post { id } } } }"
    variables = {
        "input": {
            "channelId": BUFFER_PROFILE_ID,
            "text": text,
            "schedulingType": "automatic", "mode": "shareNow",
            "assets": [{"externalUrl": image_url}],
            "metadata": {"facebook": {"type": "post"}}
        }
    }
    return requests.post(url, json={"query": query, "variables": variables}, headers=headers).json()

# --- ROUTES ---
@app.get("/")
def home():
    return {"status": "Bot Active", "endpoint": "/cron-post"}

@app.get("/cron-post")
def cron():
    try:
        topic = get_sequential_topic()
        caption = model.generate_content(f"Powerful short Facebook quote about {topic}. 1 sentence.").text.strip()
        image_url = get_image(caption)
        post_to_buffer(caption, image_url)
        return {"status": "Posted!", "topic": topic, "caption": caption}
    except Exception as e:
        return {"error": str(e)}

# This is the magic line for Vercel to find the app
app = app 
