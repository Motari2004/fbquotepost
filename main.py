import os
import requests
import google.generativeai as genai
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# Load environment variables
load_dotenv()

# Gemini AI Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-3.1-flash-lite")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Buffer Config
BUFFER_TOKEN = os.getenv("BUFFER_ACCESS_TOKEN")
BUFFER_PROFILE_ID = os.getenv("BUFFER_PROFILE_ID")
UNSPLASH_KEY = os.getenv("UNSPLASH_ACCESS_KEY")

class PromptRequest(BaseModel):
    topic: str

class PostRequest(BaseModel):
    caption: str
    image_url: str

app.mount("/static", StaticFiles(directory="static"), name="static")

# --- SEQUENTIAL TOPIC LOGIC ---

def get_sequential_topic():
    """Cycles through topics.txt using pointer.txt."""
    if not os.path.exists("topics.txt"):
        with open("topics.txt", "w") as f:
            f.write("resilience\ndiscipline\nfocus\ngrowth\n")

    with open("topics.txt", "r") as f:
        all_topics = [line.strip() for line in f if line.strip()]

    if os.path.exists("pointer.txt"):
        with open("pointer.txt", "r") as f:
            try:
                current_index = int(f.read().strip())
            except:
                current_index = 0
    else:
        current_index = 0

    actual_index = current_index % len(all_topics)
    selected_topic = all_topics[actual_index]

    with open("pointer.txt", "w") as f:
        f.write(str(actual_index + 1))

    return selected_topic

# --- AI EYE LOGIC ---

def get_image_keywords(caption):
    """Analyzes the quote to find the best matching image keywords."""
    try:
        prompt = f"Based on this quote: '{caption}', provide 3 simple English keywords for a high-quality Unsplash photo search. Return ONLY the keywords separated by commas."
        response = model.generate_content(prompt)
        return response.text.strip()
    except:
        return "motivational, nature"

def fetch_unsplash_image(keywords):
    """Fetches a random image from Unsplash based on AI keywords."""
    unsplash_url = "https://api.unsplash.com/photos/random"
    params = {"query": keywords, "client_id": UNSPLASH_KEY, "orientation": "squarish"}
    try:
        img_res = requests.get(unsplash_url, params=params, timeout=10)
        if img_res.status_code == 200:
            return img_res.json().get("urls", {}).get("regular")
    except:
        pass
    return "https://images.unsplash.com/photo-1497567149193-57567572f26f"

# --- MODERN GRAPHQL BUFFER POST (ASSETS VERSION) ---

def post_to_buffer_graphql(text, image_url):
    """
    Uses the modern Buffer GraphQL API configured to publish 
    immediately via 'shareNow' and 'assets' array.
    """
    url = "https://api.buffer.com"
    headers = {
        "Authorization": f"Bearer {BUFFER_TOKEN}",
        "Content-Type": "application/json"
    }
    
    query = """
    mutation CreateImmediatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess {
          post {
            id
          }
        }
        ... on MutationError {
          message
        }
      }
    }
    """
    
    # 'assets' entries must be OneOf objects, each wrapping the URL
    # in an explicit asset-type key (image/video/document/link).
    # Facebook also requires an explicit post type via metadata.
    variables = {
        "input": {
            "channelId": BUFFER_PROFILE_ID,
            "text": text,
            "schedulingType": "automatic", 
            "mode": "shareNow",              
            "assets": [
                {
                    "image": {
                        "url": image_url
                    }
                }
            ],
            "metadata": {
                "facebook": {
                    "type": "post"
                }
            }
        }
    }
    
    payload = {
        "query": query,
        "variables": variables
    }
    
    response = requests.post(url, json=payload, headers=headers)
    return response.json()

# --- CRON ENDPOINT ---

@app.get("/cron-post")
async def cron_post():
    """
    TRIGGERED BY EXTERNAL CRON.
    Generates a fresh quote + image and publishes immediately.
    """
    try:
        topic = get_sequential_topic()
        print(f"Cron triggered! Processing topic: {topic}")

        prompt = f"Write a unique, powerful motivational Facebook quote about {topic}. No hashtags, just 1-2 sentences."
        caption = model.generate_content(prompt).text.strip()

        keywords = get_image_keywords(caption)
        image_url = fetch_unsplash_image(keywords)

        result = post_to_buffer_graphql(caption, image_url)

        data = result.get("data", {}).get("createPost", {})
        
        if "message" in data:
            error_msg = data["message"]
            raise HTTPException(status_code=500, detail=f"Buffer GraphQL Error: {error_msg}")
        
        if not data.get("post"):
            top_level_errors = result.get("errors")
            raise HTTPException(status_code=500, detail=f"API Error: {top_level_errors}")

        print(f"Successfully posted {topic} immediately to Facebook!")
        return {"status": "success", "topic": topic, "caption": caption, "image": image_url}

    except Exception as e:
        print(f"Cron Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- MANUAL POST ENDPOINT ---

@app.post("/post")
async def manual_post(req: PostRequest):
    """
    MANUAL POST ENDPOINT — called from the web UI.
    Takes the caption + image already generated by /generate
    and publishes them to Facebook via Buffer shareNow.
    """
    try:
        result = post_to_buffer_graphql(req.caption, req.image_url)

        data = result.get("data", {}).get("createPost", {})

        if "message" in data:
            raise HTTPException(
                status_code=500,
                detail=f"Buffer GraphQL Error: {data['message']}"
            )

        if not data.get("post"):
            raise HTTPException(
                status_code=500,
                detail=f"API Error: {result.get('errors')}"
            )

        print("Manually posted to Facebook!")
        return {"status": "success", "post_id": data["post"]["id"]}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Manual Post Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- OTHER ROUTES ---

@app.get("/")
async def read_index():
    return FileResponse('static/index.html')

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

if __name__ == "__main__":
    import uvicorn
    print("Cron-Ready AI Bot (Assets + shareNow Version) Active.")
    uvicorn.run(app, host="0.0.0.0", port=10000)