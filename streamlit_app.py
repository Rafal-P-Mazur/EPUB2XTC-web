import streamlit as st
import os
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

    for text_node in soup.find_all(string=True):
        if text_node.parent.name in ['script', 'style', 'head', 'title', 'meta']:
            continue
        if not text_node.strip():
            continue

        original_text = str(text_node)
        clean_text = original_text.replace('\u00A0', ' ')

        def replace_match(match):
            word = match.group(0)
            if len(word) < 6: return word
            return dic.inserted(word, hyphen='\u00AD')

        new_text = word_pattern.sub(replace_match, clean_text)

        if new_text != original_text:
            text_node.replace_with(NavigableString(new_text))

    return soup


# --- PROCESSING ENGINE ---

class EpubProcessor:
    def __init__(self):
        self.fitz_docs = []
        self.toc_data_final = []
        self.toc_pages_images = []
        self.page_map = []
        self.total_pages = 0
        self.toc_items_per_page = 18
        self.is_ready = False
        self.temp_dir = tempfile.TemporaryDirectory()

    def load_and_layout(self, epub_bytes, font_path, font_size, margin, line_height, font_weight,
                        bottom_padding, top_padding, text_align="justify", orientation="Portrait", add_toc=True):

        self.font_path = font_path
        self.font_size = int(font_size)
        self.margin = margin
        self.line_height = line_height
        self.font_weight = font_weight
        self.bottom_padding = bottom_padding
        self.top_padding = top_padding
        self.text_align = text_align

        if orientation == "Landscape":
            self.screen_width = DEFAULT_SCREEN_HEIGHT  # 800
            self.screen_height = DEFAULT_SCREEN_WIDTH  # 480
        else:
            self.screen_width = DEFAULT_SCREEN_WIDTH  # 480
            self.screen_height = DEFAULT_SCREEN_HEIGHT  # 800

        # Close existing docs
        for doc, _ in self.fitz_docs: doc.close()
        self.fitz_docs, self.page_map, self.toc_data = [], [], []

        # Save uploaded bytes to temp file
        epub_temp_path = os.path.join(self.temp_dir.name, "input.epub")
        with open(epub_temp_path, "wb") as f:
            f.write(epub_bytes)

        try:
            book = epub.read_epub(epub_temp_path)
        except Exception as e:
            st.error(f"Error reading EPUB: {e}")
            return False

        if self.font_path and os.path.exists(self.font_path):
            css_font_path = self.font_path.replace("\\", "/")
            font_face_rule = f'@font-face {{ font-family: "CustomFont"; src: url("{css_font_path}"); }}'
            font_family_val = '"CustomFont"'
        else:
            font_face_rule = ""
            font_family_val = "serif"

        custom_css = f"""
        <style>
            {font_face_rule}

            @page {{ 
                size: {self.screen_width}pt {self.screen_height}pt; 
                margin: 0; 
            }}

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
                width: 100% !important;
                height: 100% !important;
            }}
            img {{ max-width: 95% !important; height: auto !important; display: block; margin: 20px auto !important; }}
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

        # Progress reporting for Streamlit
        status_text = st.empty()
        progress_bar = st.progress(0)

        self.toc_data = []
        for idx, item in enumerate(items):
            status_text.text(f"Processing chapter {idx + 1}/{len(items)}...")
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

            temp_html_path = os.path.join(self.temp_dir.name, f"render_{idx}.html")
            with open(temp_html_path, "w", encoding="utf-8") as f:
                f.write(final_html)

            doc = fitz.open(temp_html_path)

            rect = fitz.Rect(0, 0, self.screen_width, self.screen_height)
            doc.layout(rect=rect)

            self.fitz_docs.append((doc, has_image))
            for i in range(len(doc)): self.page_map.append((len(self.fitz_docs) - 1, i))
            running_page_count += len(doc)

        if add_toc:
            # Dynamic calculation for TOC items per page based on font size
            toc_header_space = 100 + self.top_padding
            # Row height is font_size * line_height + padding
            self.toc_row_height = int(self.font_size * self.line_height * 1.2)
            available_h = self.screen_height - self.bottom_padding - toc_header_space

            self.toc_items_per_page = max(1, int(available_h // self.toc_row_height))
            num_toc_pages = (len(self.toc_data) + self.toc_items_per_page - 1) // self.toc_items_per_page

            self.toc_data_final = [(t, temp_chapter_starts[i] + num_toc_pages + 1) for i, t in enumerate(self.toc_data)]
            self.toc_pages_images = self._render_toc_pages(self.toc_data_final)
        else:
            self.toc_data_final = [(t, temp_chapter_starts[i] + 1) for i, t in enumerate(self.toc_data)]
            self.toc_pages_images = []

        self.total_pages = len(self.toc_pages_images) + len(self.page_map)

        status_text.empty()
        progress_bar.empty()

        self.is_ready = True
        return True

    def _get_ui_font(self, size):
        if self.font_path and os.path.exists(self.font_path):
            return get_pil_font(self.font_path, int(size))
        # Fallback to default if no custom font
        return ImageFont.load_default()

    def _render_toc_pages(self, toc_entries):
        pages = []

        # Use dynamic sizes based on config
        main_size = self.font_size
        header_size = int(self.font_size * 1.2)

        font_main = self._get_ui_font(main_size)
        font_header = self._get_ui_font(header_size)

        # Faux bold logic: if weight > 500, use stroke_width=1
        base_stroke = 1 if self.font_weight > 500 else 0
        header_stroke = 1 if self.font_weight > 400 else 0

        left_margin, right_margin, column_gap = 40, 40, 20
        limit = self.toc_items_per_page

        for i in range(0, len(toc_entries), limit):
            chunk = toc_entries[i: i + limit]
            img = Image.new('1', (self.screen_width, self.screen_height), 1)
            draw = ImageDraw.Draw(img)

            header_text = "TABLE OF CONTENTS"
            header_w = font_header.getlength(header_text)
            header_y = 40 + self.top_padding
            draw.text(
                ((self.screen_width - header_w) // 2, header_y),
                header_text,
                font=font_header,
                fill=0,
                stroke_width=header_stroke
            )

            line_y = header_y + int(header_size * 1.5)
            draw.line((left_margin, line_y, self.screen_width - right_margin, line_y), fill=0)

            y = line_y + int(main_size * 1.2)

            for title, pg_num in chunk:
                pg_str = str(pg_num)
                pg_w = font_main.getlength(pg_str)
                max_title_w = self.screen_width - left_margin - right_margin - pg_w - column_gap
                display_title = title

                # Truncate title if too long
                try:
                    if font_main.getlength(display_title) > max_title_w:
                        while font_main.getlength(display_title + "...") > max_title_w and len(display_title) > 0:
                            display_title = display_title[:-1]
                        display_title += "..."
                except:
                    pass

                draw.text(
                    (left_margin, y),
                    display_title,
                    font=font_main,
                    fill=0,
                    stroke_width=base_stroke
                )

                title_end_x = left_margin + font_main.getlength(display_title) + 5
                dots_end_x = self.screen_width - right_margin - pg_w - 10

                # Draw dots
                if dots_end_x > title_end_x:
                    try:
                        dot_char = "."
                        dot_w = font_main.getlength(dot_char)
                        if dot_w > 0:
                            dots_count = int((dots_end_x - title_end_x) / dot_w)
                            dots_text = dot_char * dots_count
                            draw.text((title_end_x, y), dots_text, font=font_main, fill=0)
                    except:
                        pass

                # Draw page number
                draw.text(
                    (self.screen_width - right_margin - pg_w, y),
                    pg_str,
                    font=font_main,
                    fill=0,
                    stroke_width=base_stroke
                )

                y += self.toc_row_height

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
            except:
                pass

        return img

    def get_xtc_bytes(self):
        if not self.is_ready: return None
        blob, idx = bytearray(), bytearray()
        data_off = 56 + (16 * self.total_pages)

        prog_text = st.empty()

        for i in range(self.total_pages):
            if i % 10 == 0: prog_text.text(f"Exporting page {i + 1}/{self.total_pages}...")
            img = self.render_page(i).convert("L").point(lambda p: 255 if p > 128 else 0, mode='1')
            w, h = img.size
            xtg = struct.pack("<IHHBBIQ", 0x00475458, w, h, 0, 0, ((w + 7) // 8) * h, 0) + img.tobytes()
            idx.extend(struct.pack("<QIHH", data_off + len(blob), len(xtg), w, h))
            blob.extend(xtg)

        header = struct.pack("<IHHBBBBIQQQQQ", 0x00435458, 0x0100, self.total_pages, 0, 0, 0, 0, 0, 0, 56, data_off, 0,
                             0)
        prog_text.empty()

        return io.BytesIO(header + idx + blob)


# --- STREAMLIT APP ---

def main():
    st.set_page_config(page_title="EPUB to XTC Live", layout="wide", initial_sidebar_state="expanded")

    # CSS to increase Sidebar Width and hide default headers
    st.markdown("""
    <style>
        section[data-testid="stSidebar"] { width: 400px !important; }
        .block-container { padding-top: 1rem; padding-bottom: 1rem; }
        
        /* REMOVED: header[data-testid="stHeader"] { height: 0; visibility: hidden; } 
           The line above was hiding the button to re-open the sidebar.
           
           Use the lines below instead if you want a cleaner look but 
           still want the sidebar menu button to be visible:
        */
        
        header[data-testid="stHeader"] {
            background-color: rgba(0,0,0,0); /* Transparent background */
        }
        
        /* Optional: Hide the colored decoration line at the top */
        header[data-testid="stHeader"] > div:first-child {
            background: transparent;
        }
    </style>
    """, unsafe_allow_html=True)

    # Initialize Session State
    if 'processor' not in st.session_state:
        st.session_state.processor = EpubProcessor()
    if 'current_page' not in st.session_state:
        st.session_state.current_page = 0
    if 'last_config' not in st.session_state:
        st.session_state.last_config = {}

    st.markdown(
        "<h3 style='margin-bottom: 0.5rem; text-align: center;'>📘 EPUB → XTC Converter</h3>",
        unsafe_allow_html=True
    )

    # --- SIDEBAR: CONTROLS ---
    with st.sidebar:
        # 1. EXPORT SECTION
        if st.session_state.processor.is_ready:
            st.success("✅ Book Ready for Export")
            if st.button("Download XTC File", type="primary", use_container_width=True):
                with st.spinner("Generating binary..."):
                    xtc_data = st.session_state.processor.get_xtc_bytes()
                    st.download_button(
                        label="Click here to Save",
                        data=xtc_data,
                        file_name="book.xtc",
                        mime="application/octet-stream"
                    )
            st.divider()

        # 2. INPUT SECTION
        st.header("1. Input Files")
        uploaded_file = st.file_uploader("Upload EPUB", type=["epub"])
        uploaded_font = st.file_uploader("Custom Font (TTF)", type=["ttf"])

        st.divider()

        # 3. SETTINGS GRID
        st.header("2. Layout & Typography")

        current_config = {}

        r1_c1, r1_c2 = st.columns(2)
        with r1_c1:
            current_config['orientation'] = st.selectbox("Orientation", ["Portrait", "Landscape"])
        with r1_c2:
            current_config['align'] = st.selectbox("Alignment", ["justify", "left"])

        current_config['use_toc'] = st.checkbox("Generate Table of Contents", value=True)

        st.subheader("Text Settings")
        r2_c1, r2_c2 = st.columns(2)
        with r2_c1:
            current_config['font_size'] = st.number_input("Font Size", 10, 50, DEFAULT_FONT_SIZE)
        with r2_c2:
            current_config['font_weight'] = st.number_input("Weight (100-900)", 100, 900, DEFAULT_FONT_WEIGHT, step=100)

        r3_c1, r3_c2 = st.columns(2)
        with r3_c1:
            current_config['line_height'] = st.number_input("Line Height", 1.0, 3.0, DEFAULT_LINE_HEIGHT, step=0.1)
        with r3_c2:
            current_config['margin'] = st.number_input("Side Margin", 0, 100, DEFAULT_MARGIN)

        st.subheader("Vertical Spacing")
        r4_c1, r4_c2 = st.columns(2)
        with r4_c1:
            current_config['top_pad'] = st.number_input("Top Pad", 0, 100, DEFAULT_TOP_PADDING)
        with r4_c2:
            current_config['bot_pad'] = st.number_input("Bottom Pad", 0, 100, DEFAULT_BOTTOM_PADDING)

        st.divider()
        st.header("3. View")
        preview_width = st.slider("Preview Zoom", 200, 800, 350)

    # --- LOGIC CORE ---

    if uploaded_file:
        file_signature = uploaded_file.name + str(uploaded_file.size)
        current_config['file_sig'] = file_signature

        # Handle Font Path
        font_path = ""
        if uploaded_font:
            # We use a context manager or explicitly close to ensure the lock is released
            # so PIL can read it immediately after.
            try:
                tfile = tempfile.NamedTemporaryFile(delete=False, suffix=".ttf")
                tfile.write(uploaded_font.getvalue())
                font_path = tfile.name
                tfile.close()  # Explicitly close to release lock
                current_config['font_sig'] = uploaded_font.name
            except Exception as e:
                st.error(f"Error handling font file: {e}")

        # Check if we need to run the heavy processing
        if current_config != st.session_state.last_config or not st.session_state.processor.is_ready:

            relative_pos = 0.0
            if st.session_state.processor.is_ready and st.session_state.processor.total_pages > 0:
                relative_pos = st.session_state.current_page / st.session_state.processor.total_pages

            with st.spinner("Rendering layout..."):
                success = st.session_state.processor.load_and_layout(
                    uploaded_file.getvalue(),
                    font_path,
                    current_config['font_size'],
                    current_config['margin'],
                    current_config['line_height'],
                    current_config['font_weight'],
                    current_config['bot_pad'],
                    current_config['top_pad'],
                    current_config['align'],
                    current_config['orientation'],
                    current_config['use_toc']
                )

                if success:
                    st.session_state.last_config = current_config
                    new_total = st.session_state.processor.total_pages
                    st.session_state.current_page = int(relative_pos * new_total)
                    st.session_state.current_page = min(max(0, st.session_state.current_page), new_total - 1)
                    st.rerun()

    # --- DISPLAY AREA ---

    if st.session_state.processor.is_ready:
        nav_cols = st.columns([1, 3, 1])

        with nav_cols[0]:
            if st.button("⬅ Previous", use_container_width=True):
                st.session_state.current_page = max(0, st.session_state.current_page - 1)

        with nav_cols[1]:
            st.markdown(
                f"""
                <div style="text-align:center; font-size:1.1rem; margin-bottom: 0.5rem; color: #444;">
                    Page <b>{st.session_state.current_page + 1}</b> / {st.session_state.processor.total_pages}
                </div>
                """,
                unsafe_allow_html=True
            )

        with nav_cols[2]:
            if st.button("Next ➡", use_container_width=True):
                st.session_state.current_page = min(
                    st.session_state.processor.total_pages - 1,
                    st.session_state.current_page + 1
                )

        img = st.session_state.processor.render_page(st.session_state.current_page)

        preview_img = img.copy()
        draw = ImageDraw.Draw(preview_img)
        w, h = preview_img.size
        draw.rectangle([(0, 0), (w - 1, h - 1)], outline="black", width=2)

        with io.BytesIO() as buffer:
            preview_img.save(buffer, format="PNG")
            img_b64 = base64.b64encode(buffer.getvalue()).decode()

        st.markdown(
            f"""
            <div style="display: flex; justify-content: center; margin-top: 15px; margin-bottom: 50px;">
                <img src="data:image/png;base64,{img_b64}" width="{preview_width}" style="max-width: 100%; box-shadow: 0px 4px 15px rgba(0,0,0,0.15);">
            </div>
            """,
            unsafe_allow_html=True
        )

    else:
        st.info("👈 Please upload an EPUB file in the sidebar to begin.")


if __name__ == "__main__":
    main()
