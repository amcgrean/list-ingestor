"""Application entry point for both local development and gunicorn."""

from dotenv import load_dotenv

load_dotenv()  # Load .env file if present (local dev only)

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
