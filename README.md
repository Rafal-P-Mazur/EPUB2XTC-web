# EPUB to XTC Converter (Web Version)
**For Xteink X4 E-readers**

A web-based tool designed to convert standard `.epub` files into the `.xtc` binary format required by the **Xteink X4** e-reader. It renders HTML content into paginated, bitmapped images optimized for e-ink displays directly in your browser.

[![Streamlit App](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://epub2xtc.streamlit.app/)

## Main Features

* **Smart Hyphenation:** Uses `pyphen` to inject soft hyphens into text nodes, ensuring proper line breaks and justified text flow.
* **Unlisted Chapters:** Ability to hide specific sections (like Acknowledgments) from the TOC and Progress Bar without deleting them from the book.
* **Table of Contents Generation:** Automatically creates visual TOC pages at the start of the file.
* **Visual Progress Bar:** Generates a reading progress bar at the bottom of every page.
* **Custom Typography:** Supports system fonts and allows uploading custom `.ttf` fonts.
* **Image Optimization:** Automatically extracts, scales, contrast-enhances, and dithers (Floyd-Steinberg) images embedded in the EPUB.
* **Smart Preview Scaling:** Preview window automatically adapts to Portrait (width-locked) or Landscape (height-locked) orientation.

## 📖 User Manual

1.  **Load an EPUB:** Drag and drop your file into the **"Upload EPUB"** area in the sidebar. The app will instantly parse the book structure.
2.  **Select Chapters:** Once parsed, a **"Chapter Visibility"** expander will appear in the sidebar.
    * **Uncheck** any chapters you wish to hide from the **Table of Contents** and **Progress Bar**.
    * *Note:* These chapters are **not deleted**; they remain in the book for reading but will not clutter your navigation.
3.  **Configure Layout:**
    * **Font:** Use the default or upload a custom `.ttf` file.
    * **Settings:** Adjust Font Size, Weight, Line Height, Margins, and Padding in the sidebar.
    * **Orientation:** Switch between Portrait and Landscape modes.
    * **Preview Zoom:** Use the slider to resize the preview image (Smart Scaling automatically optimizes the zoom).
4.  **Render:** The layout **updates automatically** a moment after you change any setting. (A manual "Apply Changes" button is available if you need to force a refresh).
5.  **Navigate & Preview:**
    * Use the **⬅ Previous** and **Next ➡** buttons at the top to flip pages.
    * Enter a specific number in the **"Jump to page"** input box below the preview to skip directly to that page.
6.  **Download:** Once satisfied, click the **Download XTC** button in the sidebar to save the final binary file.

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
