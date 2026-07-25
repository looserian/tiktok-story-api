# TikTok Story API

A production-ready REST API for fetching TikTok stories, built with **FastAPI**, **Playwright**, and **Docker**.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12 |
| Framework | FastAPI |
| Server | Uvicorn |
| Browser automation | Playwright + Chromium |
| Containerisation | Docker + Docker Compose |

---

## Project Structure

```
tiktok-story-api/
│
├── app/
│   ├── main.py        # FastAPI app factory & middleware
│   ├── routes.py      # Endpoint definitions
│   ├── scraper.py     # Playwright scraper (stub — ready for implementation)
│   ├── auth.py        # Bearer token authentication dependency
│   ├── config.py      # Environment-based configuration (pydantic-settings)
│   ├── models.py      # Pydantic request/response models
│   └── __init__.py
│
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
└── README.md
```

---

## Quick Start

### 1. Configure environment variables

```bash
cp .env.example .env
# Edit .env and set your API key:
#   API_KEY=your-secret-api-key-here
```

### 2. Build and run with Docker Compose

```bash
docker compose up --build
```

The API will be available at **http://localhost:8000**.

### 3. Explore the interactive docs

Open your browser and navigate to:

```
http://localhost:8000/docs
```

This opens the **Swagger UI** with full request/response documentation and a built-in API tester.

---

## API Endpoints

### `GET /`
Public. Returns API metadata.

```json
{
  "name": "TikTok Story API",
  "version": "1.0"
}
```

### `GET /health`
Public. Liveness probe — used by Docker Compose health checks.

```json
{
  "status": "ok"
}
```

### `GET /stories?username={username}`
🔒 **Requires authentication.**

Returns TikTok stories for the specified username.

**Request headers:**
```
Authorization: Bearer <your-api-key>
```

**Example request:**
```bash
curl -H "Authorization: Bearer your-secret-api-key-here" \
     "http://localhost:8000/stories?username=someuser"
```

**Example response:**
```json
{
  "success": true,
  "username": "someuser",
  "stories": []
}
```

> **Note:** Story scraping is not yet implemented. The endpoint always returns an empty `stories` list until the Playwright scraper in `app/scraper.py` is implemented.

---

## Authentication

All protected endpoints require a Bearer token:

```
Authorization: Bearer <API_KEY>
```

Where `API_KEY` matches the value set in your `.env` file.  
An invalid or missing token returns **HTTP 401 Unauthorized**.

---

## Development (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env             # set API_KEY

uvicorn app.main:app --reload
```

---

## Implementing the Scraper

The scraper stub lives in [`app/scraper.py`](app/scraper.py).  
Playwright and Chromium are already installed in the Docker image.  
Fill in the `fetch_stories()` function to begin scraping:

```python
async def fetch_stories(username: str) -> list[Story]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(f"https://www.tiktok.com/@{username}")
        # ... extract story data ...
        await browser.close()
    return stories
```

---

## License

MIT
