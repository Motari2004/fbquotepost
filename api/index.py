import os
import requests
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv

# Load .env file for local development (ignored on Vercel, which uses its own env vars)
load_dotenv()

app = FastAPI()

GEMINI_MODEL = "gemini-2.5-flash"
FALLBACK_IMAGE = "https://images.unsplash.com/photo-1497567149193-57567572f26f"


# --- HELPERS ---
def get_env(key):
    return os.getenv(key)


def get_db_connection():
    url = get_env("DATABASE_URL")
    if not url:
        raise Exception("DATABASE_URL is missing from environment variables")

    try:
        # sslmode='require' is mandatory for Neon
        return psycopg2.connect(url, connect_timeout=10, sslmode="require")
    except Exception as e:
        raise Exception(f"Neon Connection Failed: {str(e)}")


# --- DATABASE LOGIC ---
def get_sequential_topic():
    """Return the current topic WITHOUT advancing the pointer."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value INTEGER)")
        cur.execute("CREATE TABLE IF NOT EXISTS topics (id SERIAL PRIMARY KEY, name TEXT UNIQUE)")
        cur.execute("INSERT INTO bot_state (key, value) VALUES ('pointer', 0) ON CONFLICT DO NOTHING")

        cur.execute("SELECT COUNT(*) AS total FROM topics")
        total = cur.fetchone()["total"]

        if total == 0:
            topics_list = [
                ("resilience",), ("discipline",), ("focus",),
                ("growth",), ("mindset",), ("consistency",),
            ]
            cur.executemany("INSERT INTO topics (name) VALUES (%s)", topics_list)
            cur.execute("SELECT COUNT(*) AS total FROM topics")
            total = cur.fetchone()["total"]

        cur.execute("SELECT value FROM bot_state WHERE key = 'pointer'")
        res = cur.fetchone()
        pointer = res["value"] if res else 0

        actual_id = (pointer % total) + 1
        cur.execute("SELECT name FROM topics WHERE id = %s", (actual_id,))
        topic_row = cur.fetchone()

        if not topic_row:
            raise Exception(f"Topic with ID {actual_id} not found")

        conn.commit()
        cur.close()
        return topic_row["name"]

    except Exception as e:
        raise Exception(f"DB Error: {str(e)}")
    finally:
        if conn:
            conn.close()


def advance_pointer():
    """Move to the next topic. Called only after a successful post."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE bot_state SET value = value + 1 WHERE key = 'pointer'")
        conn.commit()
        cur.close()
    except Exception as e:
        raise Exception(f"DB Error (advance pointer): {str(e)}")
    finally:
        if conn:
            conn.close()


# --- CONTENT LOGIC ---
def get_ai_content(topic):
    api_key = get_env("GEMINI_API_KEY")
    if not api_key:
        raise Exception("GEMINI_API_KEY missing")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)

    caption_res = model.generate_content(
        f"Write a powerful, short, one-sentence Facebook quote about {topic}. No hashtags."
    )
    caption = caption_res.text.strip()

    kw_prompt = (
        f"Provide 3 simple English keywords for a high-quality Unsplash photo "
        f"that matches this quote: '{caption}'. Comma separated only."
    )
    kw_res = model.generate_content(kw_prompt)
    keywords = kw_res.text.strip()

    unsplash_key = get_env("UNSPLASH_ACCESS_KEY")
    img_url = FALLBACK_IMAGE
    if unsplash_key:
        try:
            res = requests.get(
                "https://api.unsplash.com/photos/random",
                params={
                    "query": keywords,
                    "client_id": unsplash_key,
                    "orientation": "squarish",
                },
                timeout=10,
            )
            img_url = res.json().get("urls", {}).get("regular", FALLBACK_IMAGE)
        except Exception:
            pass

    return caption, img_url


def post_to_buffer(text, image_url):
    token = get_env("BUFFER_ACCESS_TOKEN")
    profile_id = get_env("BUFFER_PROFILE_ID")
    if not token or not profile_id:
        raise Exception("Buffer credentials missing")

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    query = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        __typename
        ... on PostActionSuccess {
          post { id status }
        }
        ... on MutationError {
          message
        }
      }
    }
    """
    variables = {
        "input": {
            "channelId": profile_id,
            "text": text,
            "schedulingType": "automatic",
            "mode": "shareNow",
            "assets": [{"image": {"url": image_url}}],
            "metadata": {"facebook": {"type": "post"}},
        }
    }

    res = requests.post(
        "https://api.buffer.com",
        json={"query": query, "variables": variables},
        headers=headers,
        timeout=15,
    )
    if res.status_code != 200:
        raise Exception(f"Buffer API Error: {res.text}")

    body = res.json()

    # GraphQL returns HTTP 200 even when the request fails, so check the body
    if body.get("errors"):
        raise Exception(f"Buffer GraphQL Error: {body['errors']}")

    result = (body.get("data") or {}).get("createPost") or {}
    if "post" not in result:
        raise Exception(f"Buffer rejected the post: {result.get('message', body)}")

    return result["post"]


# --- ROUTES ---
# Each route is registered under two paths so it works whether Vercel
# passes the original path or the rewritten /api/... path to FastAPI.
@app.get("/")
@app.get("/api")
@app.get("/api/index")
def home():
    return {
        "status": "Bot Active",
        "message": "FastAPI is running on Vercel. Trigger /cron-post to post a quote.",
    }


@app.get("/cron-post")
@app.get("/api/cron-post")
def cron():
    try:
        topic = get_sequential_topic()
        caption, img = get_ai_content(topic)
        post = post_to_buffer(caption, img)
        advance_pointer()  # only after Buffer confirms the post

        return {
            "status": "Success",
            "buffer_post": post,
            "topic": topic,
            "caption": caption,
            "image": img,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# This is for local testing only (ignored on Vercel)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)