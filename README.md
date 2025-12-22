# 🎈 Blank app template

A simple Streamlit app template for you to modify!

[![Open in Streamlit](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://blank-app-template.streamlit.app/)

### How to run it on your own machine

1. Install the requirements

   ```
   $ pip install -r requirements.txt
   ```

2. Run the app

   ```
   $ streamlit run streamlit_app.py
   ```

## Running with Docker (Streamlit Web App)

This repository can be run locally using Docker and Docker Compose, providing the same Streamlit web interface as the public demo.

### Build and run

Clone the repository and run:

```bash
docker compose build --no-cache
docker compose up -d && docker-compose logs -f
```
