import streamlit as st
import os
import sys
import struct
import fitz  # PyMuPDF
from PIL import Image, ImageEnhance, ImageDraw, ImageFont
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, NavigableString
import pyphen
import base64
import re
import tempfile
import io

# --- CONFIGURATION DEFAULTS ---
DEFAULT_SCREEN_WIDTH = 480
DEFAULT_SCREEN_HEIGHT = 800
DEFAULT_RENDER_SCALE = 3.0
DEFAULT_FONT_SIZE = 22
DEFAULT_MARGIN = 20
DEFAULT_LINE_HEIGHT = 1.4
DEFAULT_FONT_WEIGHT = 400
DEFAULT_BOTTOM_PADDING = 15
DEFAULT_TOP_PADDING = 15

# --- UTILITY FUNCTIONS ---

def fix_css_font_paths(css_text, target_font_family="'CustomFont'"):
    if target_font_family is None:
        return css_text
    # Simple regex to force font family
    css_text = re.sub(r'font-family\s*:\s*[^;!]+', f'font-family: {target_font_family}', css_text)
    return css_text

def get_pil_font(font_path, size):
    try:
        if font_path and os.path.exists(font_path):
            return ImageFont.truetype(font_path, size)
        return ImageFont.load_default()
    except:
        return ImageFont.load_default()

def extract_all_css(book):
    css_rules = []
    for item in book.get_items_of_type(ebooklib.ITEM_STYLE):
        try:
            css_rules.append(item.get_content().decode('utf-8', errors='ignore'))
        except:
            pass
    return "\n".join(css_rules)

def extract_images_to_base64(book):
    image_map = {}
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        try:
            filename = os.path.basename(item.get_name())
            b64_data = base64.b64encode(item.get_content()).decode('utf-8')
            image_map[filename] = f"data:{item.media_type};base64,{b64_data}"
        except:
            pass
    return image_map

def get_official_toc_mapping(book):
    mapping = {}
    def process_toc_item(item):
        if isinstance(item, tuple):
            if len(item) > 1 and isinstance(item[1], list):
                for sub in item[1]: process_toc_item(sub)
        elif isinstance(item, epub.Link):
            clean_href = item.href.split('#')[0]
            mapping[clean_href] = item.title
    for item in book.toc: process_toc_item(item)
    return mapping

def hyphenate_html_text(soup, language_code):
    try:
        dic = pyphen.Pyphen(lang=language_code)
    except:
        try:
            dic = pyphen.Pyphen(lang='en')
        except:
            return soup

    word_pattern = re.compile(r'\w+', re.UNICODE)

    def replace_match(match):
        word = match.group(0)
        if len(word) < 6: return word
        return dic.inserted(word, hyphen='\u00AD')

    for text_node in soup.find_all(string=True):
        if text_node.parent.name in ['script', 'style', 'head', 'title', 'meta']: continue
        if not text_node.strip(): continue
        original_text = str(text_node)
        new_text = word_pattern.sub(replace_match, original_text)
        if new_text != original_text:
            text_node.replace_with(NavigableString(new_text))
    return soup

# --- PROCESSING ENGINE (Modified for Web) ---

class EpubProcessor:
    def __init__(self):
        self.fitz_docs = []
        self.toc_data_final = []
        self.toc_pages_images = []
        self.page_map = []
        self.total_pages = 0
        self.toc_items_per_page = 18
        self.is_ready = False
        self.temp_dir = tempfile.TemporaryDirectory() # Auto-cleanup temp dir

    def load_and_layout(self, epub_bytes, font_path, font_size, margin, line_height, font_weight,
                        bottom_padding, top_padding, text_align="justify", add_toc=True):
        
        self.font_path = font_path
        self.font_size = font_size
        self.margin = margin
        self.line_height = line_height
        self.font_weight = font_weight
        self.bottom_padding = bottom_padding
        self.top_padding = top_padding
        self.text_align = text_align
        self.screen_width = DEFAULT_SCREEN_WIDTH
        self.screen_height = DEFAULT_SCREEN_HEIGHT

        # Close existing docs
        for doc, _ in self.fitz_docs: doc.close()
        self.fitz_docs, self.page_map, self.toc_data = [], [], []

        # Save uploaded bytes to temp file for ebooklib
        epub_temp_path = os.path.join(self.temp_dir.name, "input.epub")
        with open(epub_temp_path, "wb") as f:
            f.write(epub_bytes)

        try:
            book = epub.read_epub(epub_temp_path)
        except Exception as e:
            st.error(f"Error reading EPUB: {e}")
            return False

        # Font handling for CSS
        if self.font_path and os.path.exists(self.font_path):
            # We need to copy the font to a temp path accessible by the renderer
            # But for simplicity, we assume font_path is a valid system path or uploaded path
            # In a web context, we might embed base64 font, but here we link normally
            css_font_path = self.font_path.replace("\\", "/")
            font_face_rule = f'@font-face {{ font-family: "CustomFont"; src: url("{css_font_path}"); }}'
            font_family_val = '"CustomFont"'
        else:
            font_face_rule = ""
            font_family_val = "serif"

        custom_css = f"""
        <style>
            {font_face_rule}
            @page {{ margin: 0; }}
            body, p, div, span, li, blockquote, dd, dt {{
                font-family: {font_family_val} !important;
                font-size: {self.font_size}pt !important;
                font-weight: {self.font_weight} !important;
                line-height: {self.line_height} !important;
                text-align: {self.text_align} !important;
                color: black !important;
                overflow-wrap: break-word;
            }}
            body {{
                margin: 0 !important;
                padding: {self.margin}px !important;
                background-color: white !important;
            }}
            img {{ max-width: 95% !important; height: auto !important; display: block; margin: 50px auto !important; }}
            h1, h2, h3 {{ 
                text-align: center !important; 
                margin-top: 1em; 
                font-weight: {min(900, self.font_weight + 200)} !important; 
            }}
        </style>
        """

        try:
            book_lang = book.get_metadata('DC', 'language')[0][0]
        except:
            book_lang = 'en'

        image_map = extract_images_to_base64(book)
        original_css = fix_css_font_paths(extract_all_css(book), font_family_val)
        toc_mapping = get_official_toc_mapping(book)

        items = [book.get_item_with_id(item_ref[0]) for item_ref in book.spine
                 if isinstance(book.get_item_with_id(item_ref[0]), epub.EpubHtml)]

        temp_chapter_starts = []
        running_page_count = 0

        # Create a progress bar in Streamlit
        progress_bar = st.progress(0)
        
        self.toc_data = []
        for idx, item in enumerate(items):
            progress_bar.progress(int((idx / len(items)) * 90))

            item_name = item.get_name()
            raw_html = item.get_content().decode('utf-8', errors='replace')
            soup = BeautifulSoup(raw_html, 'html.parser')

            has_image = bool(soup.find('img'))
            text_content = soup.get_text().strip()

            if item_name not in toc_mapping and len(text_content) < 50 and not has_image: continue

            temp_chapter_starts.append(running_page_count)
            chapter_title = toc_mapping.get(item_name) or (soup.find(['h1', 'h2']).get_text().strip() if soup.find(
                ['h1', 'h2']) else f"Section {len(self.toc_data) + 1}")
            self.toc_data.append(chapter_title)

            for img_tag in soup.find_all('img'):
                src = os.path.basename(img_tag.get('src', ''))
                if src in image_map: img_tag['src'] = image_map[src]

            soup = hyphenate_html_text(soup, book_lang)

            body_content = "".join([str(x) for x in soup.body.contents]) if soup.body else str(soup)
            final_html = f"<html lang='{book_lang}'><head><style>{original_css}</style>{custom_css}</head><body>{body_content}</body></html>"

            # Use unique temp filename
            temp_html_path = os.path.join(self.temp_dir.name, f"render_{idx}.html")
            with open(temp_html_path, "w", encoding="utf-8") as f:
                f.write(final_html)

            doc = fitz.open(temp_html_path)
            self.fitz_docs.append((doc, has_image))
            for i in range(len(doc)): self.page_map.append((len(self.fitz_docs) - 1, i))
            running_page_count += len(doc)

        if add_toc:
            toc_header_space = 100 + self.top_padding
            toc_row_height = 35
            available_h = self.screen_height - self.bottom_padding - toc_header_space

            self.toc_items_per_page = max(1, int(available_h // toc_row_height))
            num_toc_pages = (len(self.toc_data) + self.toc_items_per_page - 1) // self.toc_items_per_page

            self.toc_data_final = [(t, temp_chapter_starts[i] + num_toc_pages + 1) for i, t in enumerate(self.toc_data)]
            self.toc_pages_images = self._render_toc_pages(self.toc_data_final)
        else:
            self.toc_data_final = [(t, temp_chapter_starts[i] + 1) for i, t in enumerate(self.toc_data)]
            self.toc_pages_images = []

        self.total_pages = len(self.toc_pages_images) + len(self.page_map)
        progress_bar.progress(100)
        self.is_ready = True
        return True

    def _get_ui_font(self, size):
        # Fallback for web
        if self.font_path and os.path.exists(self.font_path):
             return get_pil_font(self.font_path, size)
        return ImageFont.load_default()

    def _render_toc_pages(self, toc_entries):
        pages = []
        font_main = self._get_ui_font(20)
        font_header = self._get_ui_font(24)
        left_margin, right_margin, column_gap = 40, 40, 20
        limit = self.toc_items_per_page

        for i in range(0, len(toc_entries), limit):
            chunk = toc_entries[i: i + limit]
            img = Image.new('1', (self.screen_width, self.screen_height), 1)
            draw = ImageDraw.Draw(img)

            header_text = "TABLE OF CONTENTS"
            header_w = font_header.getlength(header_text)
            header_y = 40 + self.top_padding
            draw.text(((self.screen_width - header_w) // 2, header_y), header_text, font=font_header, fill=0)

            line_y = header_y + 35
            draw.line((left_margin, line_y, self.screen_width - right_margin, line_y), fill=0)

            y = line_y + 25
            for title, pg_num in chunk:
                pg_str = str(pg_num)
                pg_w = font_main.getlength(pg_str)
                max_title_w = self.screen_width - left_margin - right_margin - pg_w - column_gap
                display_title = title
                
                # Check text length safely
                try:
                    if font_main.getlength(display_title) > max_title_w:
                        while font_main.getlength(display_title + "...") > max_title_w and len(display_title) > 0:
                            display_title = display_title[:-1]
                        display_title += "..."
                except: pass

                draw.text((left_margin, y), display_title, font=font_main, fill=0)
                title_end_x = left_margin + font_main.getlength(display_title) + 5
                dots_end_x = self.screen_width - right_margin - pg_w - 10
                if dots_end_x > title_end_x:
                    try:
                        dots_text = "." * int((dots_end_x - title_end_x) / font_main.getlength("."))
                        draw.text((title_end_x, y), dots_text, font=font_main, fill=0)
                    except: pass
                draw.text((self.screen_width - right_margin - pg_w, y), pg_str, font=font_main, fill=0)
                y += 35
            pages.append(img)
        return pages

    def render_page(self, global_page_index):
        if not self.is_ready: return None
        num_toc = len(self.toc_pages_images)

        footer_height = max(0, self.bottom_padding)
        header_height = max(0, self.top_padding)
        content_height = self.screen_height - footer_height - header_height

        if global_page_index < num_toc:
            img = self.toc_pages_images[global_page_index].copy().convert("RGB")
        else:
            doc_idx, page_idx = self.page_map[global_page_index - num_toc]
            doc, has_image = self.fitz_docs[doc_idx]
            page = doc[page_idx]
            mat = fitz.Matrix(DEFAULT_RENDER_SCALE, DEFAULT_RENDER_SCALE)
            pix = page.get_pixmap(matrix=mat, alpha=False)

            img_content = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            img_content = img_content.resize((self.screen_width, content_height), Image.Resampling.LANCZOS).convert("L")

            img = Image.new("RGB", (self.screen_width, self.screen_height), (255, 255, 255))
            img.paste(img_content, (0, header_height))

            if has_image:
                img = img.convert("L")
                img = ImageEnhance.Contrast(ImageEnhance.Brightness(img).enhance(1.15)).enhance(1.4)
                img = img.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
            else:
                img = img.convert("L")
                img = ImageEnhance.Contrast(img).enhance(2.0).point(lambda p: 255 if p > 140 else 0, mode='1')
            img = img.convert("RGB")

        draw = ImageDraw.Draw(img)
        font_ui = self._get_ui_font(16)

        page_num_disp = global_page_index + 1
        current_title = ""
        chapter_pages = [item[1] for item in self.toc_data_final]
        for title, start_pg in reversed(self.toc_data_final):
            if page_num_disp >= start_pg:
                current_title = title
                break

        bar_height = 4
        bar_y_top = self.screen_height - 20
        footer_y = self.screen_height - 45

        draw.rectangle([10, bar_y_top, self.screen_width - 10, bar_y_top + bar_height], fill=(255, 255, 255),
                       outline=(0, 0, 0))

        for cp in chapter_pages:
            if self.total_pages > 0:
                mx = int(((cp - 1) / self.total_pages) * (self.screen_width - 20)) + 10
                draw.line([mx, bar_y_top - 4, mx, bar_y_top], fill=(0, 0, 0), width=1)

        if self.total_pages > 0:
            bw = int((page_num_disp / self.total_pages) * (self.screen_width - 20))
            draw.rectangle([10, bar_y_top, 10 + bw, bar_y_top + bar_height], fill=(0, 0, 0))

        draw.text((15, footer_y), f"{page_num_disp}/{self.total_pages}", font=font_ui, fill=(0, 0, 0))
        if current_title:
            try:
                draw.text((100, footer_y), f"| {current_title}"[:35], font=font_ui, fill=(0, 0, 0))
            except: pass

        return img

    def get_xtc_bytes(self):
        # Returns bytes buffer instead of writing to file
        if not self.is_ready: return None
        blob, idx = bytearray(), bytearray()
        data_off = 56 + (16 * self.total_pages)
        
        # Simple progress tracking for export
        prog_text = st.empty()
        
        for i in range(self.total_pages):
            if i % 10 == 0: prog_text.text(f"Generating page {i+1}/{self.total_pages}...")
            img = self.render_page(i).convert("L").point(lambda p: 255 if p > 128 else 0, mode='1')
            w, h = img.size
            xtg = struct.pack("<IHHBBIQ", 0x00475458, w, h, 0, 0, ((w + 7) // 8) * h, 0) + img.tobytes()
            idx.extend(struct.pack("<QIHH", data_off + len(blob), len(xtg), w, h))
            blob.extend(xtg)
            
        header = struct.pack("<IHHBBBBIQQQQQ", 0x00435458, 0x0100, self.total_pages, 0, 0, 0, 0, 0, 0, 56, data_off, 0, 0)
        prog_text.empty()
        
        return io.BytesIO(header + idx + blob)

# --- STREAMLIT APP ---

def main():
    st.set_page_config(page_title="EPUB to XTC Web Converter", layout="wide")
    
    st.title("EPUB to XTC Converter (Web)")

    # Sidebar for inputs
    with st.sidebar:
        st.header("Settings")
        uploaded_file = st.file_uploader("Upload EPUB", type=["epub"])
        
        uploaded_font = st.file_uploader("Upload Custom Font (TTF)", type=["ttf"])
        
        # Checkbox for TOC
        use_toc = st.checkbox("Generate TOC Pages", value=True)
        text_align = st.selectbox("Text Alignment", ["justify", "left"])
        
        # Sliders
        font_size = st.slider("Font Size", 12, 36, DEFAULT_FONT_SIZE)
        font_weight = st.slider("Font Weight", 100, 900, DEFAULT_FONT_WEIGHT, step=100)
        line_height = st.slider("Line Height", 1.0, 2.5, DEFAULT_LINE_HEIGHT)
        margin = st.slider("Margin", 0, 100, DEFAULT_MARGIN)
        top_pad = st.slider("Top Padding", 0, 100, DEFAULT_TOP_PADDING)
        bot_pad = st.slider("Bottom Padding", 0, 100, DEFAULT_BOTTOM_PADDING)
        
        btn_process = st.button("Process Book", type="primary")

    # Initialize Session State
    if 'processor' not in st.session_state:
        st.session_state.processor = None
    if 'current_page' not in st.session_state:
        st.session_state.current_page = 0

    # Logic
    if btn_process and uploaded_file:
        proc = EpubProcessor()
        
        # Handle Font Upload
        font_path = ""
        if uploaded_font:
            # Save font to temp file so PIL/CSS can read it
            tfile = tempfile.NamedTemporaryFile(delete=False, suffix=".ttf")
            tfile.write(uploaded_font.read())
            font_path = tfile.name
        
        with st.spinner("Processing EPUB..."):
            success = proc.load_and_layout(
                uploaded_file.getvalue(),
                font_path,
                font_size, margin, line_height, font_weight,
                bot_pad, top_pad, text_align, use_toc
            )
            
            if success:
                st.session_state.processor = proc
                st.session_state.current_page = 0
                st.success("Processing Complete!")
            else:
                st.error("Failed to process EPUB.")

    # Preview and Export Area
    if st.session_state.processor and st.session_state.processor.is_ready:
        col1, col2 = st.columns([1, 1])
        
        with col1:
            st.subheader("Preview")
            
            # Navigation
            c_prev, c_page, c_next = st.columns([1, 2, 1])
            with c_prev:
                if st.button("Previous"):
                    st.session_state.current_page = max(0, st.session_state.current_page - 1)
            with c_next:
                if st.button("Next"):
                    st.session_state.current_page = min(st.session_state.processor.total_pages - 1, st.session_state.current_page + 1)
            with c_page:
                st.write(f"Page {st.session_state.current_page + 1} / {st.session_state.processor.total_pages}")
            
            # Render Image
            img = st.session_state.processor.render_page(st.session_state.current_page)
            st.image(img, caption=f"Page {st.session_state.current_page + 1}", use_column_width=False, width=400)

        with col2:
            st.subheader("Export")
            st.write("Click below to generate and download the .xtc file.")
            
            if st.button("Generate XTC File"):
                with st.spinner("Compiling binary XTC file..."):
                    xtc_data = st.session_state.processor.get_xtc_bytes()
                    
                    st.download_button(
                        label="Download .xtc file",
                        data=xtc_data,
                        file_name="converted_book.xtc",
                        mime="application/octet-stream"
                    )

    elif not uploaded_file:
        st.info("Upload an EPUB file in the sidebar to begin.")

if __name__ == "__main__":
    main()
