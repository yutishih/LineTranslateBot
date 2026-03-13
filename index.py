# Vercel Entry Point
from dotenv import load_dotenv
from app.app import app
import os

load_dotenv()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
