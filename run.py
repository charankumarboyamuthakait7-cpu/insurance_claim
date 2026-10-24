"""Entry point for running the Health Insurance Claims Simulator locally."""
from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=False)
