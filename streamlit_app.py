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
import json

# --- CONFIGURATION DEFAULTS ---
DEFAULT_SCREEN_WIDTH = 480
DEFAULT_SCREEN_HEIGHT = 800
DEFAULT_RENDER_SCALE = 3.0
DEFAULT_FONT_SIZE = 22
DEFAULT_MARGIN = 20
DEFAULT_LINE_HEIGHT = 1.4
DEFAULT_FONT_WEIGHT = 400
DEFAULT_BOTTOM_PADDING = 45
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
        if text_node.parent.name in ['script', 'style', 'head', 'title', 'meta']: continue
        if not text_node.strip(): continue
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
        # Step 1 Data
        self.raw_chapters = []
        self.book_css = ""
        self.book_images = {}
        self.book_lang = 'en'
        self.is_parsed = False

        # Step 2 Data
        self.fitz_docs = []
        self.toc_data_final = []
        self.toc_pages_images = []
        self.page_map = []
        self.total_pages = 0
        self.toc_items_per_page = 18
        self.is_ready = False
        self.temp_dir = tempfile.TemporaryDirectory()

        # Footer Defaults
        self.footer_visible = True
        self.footer_font_size = 16
        self.footer_show_progress = True
        self.footer_show_pagenum = True
        self.footer_show_title = True
        self.footer_text_pos = "Text Above Bar"
        self.footer_bar_height = 4
        self.footer_bottom_margin = 10

    # --- STEP 1: PARSE STRUCTURE (FAST) ---
    def parse_structure(self, epub_bytes):
        self.raw_chapters = []
        epub_temp_path = os.path.join(self.temp_dir.name, "input.epub")
        with open(epub_temp_path, "wb") as f:
            f.write(epub_bytes)

        try:
            book = epub.read_epub(epub_temp_path)
        except Exception as e:
            return False, f"Error reading EPUB: {e}"

        try:
            self.book_lang = book.get_metadata('DC', 'language')[0][0]
        except:
            self.book_lang = 'en'

        self.book_images = extract_images_to_base64(book)
        self.book_css = extract_all_css(book)
        toc_mapping = get_official_toc_mapping(book)

        items = [book.get_item_with_id(item_ref[0]) for item_ref in book.spine
                 if isinstance(book.get_item_with_id(item_ref[0]), epub.EpubHtml)]

        for idx, item in enumerate(items):
            item_name = item.get_name()
            raw_html = item.get_content().decode('utf-8', errors='replace')
            soup = BeautifulSoup(raw_html, 'html.parser')
            text_content = soup.get_text().strip()
            has_image = bool(soup.find('img'))

            if item_name not in toc_mapping and len(text_content) < 50 and not has_image:
                continue

            chapter_title = toc_mapping.get(item_name) or (soup.find(['h1', 'h2']).get_text().strip() if soup.find(
                ['h1', 'h2']) else f"Section {len(self.raw_chapters) + 1}")

            self.raw_chapters.append({
                'title': chapter_title,
                'soup': soup,
                'has_image': has_image
            })

        self.is_parsed = True
        return True, "Success"

    # --- STEP 2: RENDER (SLOW) ---
    def render_chapters(self, selected_indices_set, font_path, font_size, margin, line_height, font_weight,
                        bottom_padding, top_padding, text_align, orientation, add_toc, footer_settings=None):

        self.font_path = font_path
        self.font_size = int(font_size)
        self.margin = margin
        self.line_height = line_height
        self.font_weight = font_weight
        self.bottom_padding = bottom_padding
        self.top_padding = top_padding
        self.text_align = text_align

        # Apply Footer Settings
        if footer_settings:
            self.footer_visible = footer_settings.get("footer_visible", True)
            self.footer_font_size = footer_settings.get("footer_font_size", 16)
            self.footer_show_progress = footer_settings.get("footer_show_progress", True)
            self.footer_show_pagenum = footer_settings.get("footer_show_pagenum", True)
            self.footer_show_title = footer_settings.get("footer_show_title", True)
            self.footer_text_pos = footer_settings.get("footer_text_pos", "Text Above Bar")
            self.footer_bar_height = footer_settings.get("footer_bar_height", 4)
            self.footer_bottom_margin = footer_settings.get("footer_bottom_margin", 10)

        if orientation == "Landscape":
            self.screen_width = DEFAULT_SCREEN_HEIGHT
            self.screen_height = DEFAULT_SCREEN_WIDTH
        else:
            self.screen_width = DEFAULT_SCREEN_WIDTH
            self.screen_height = DEFAULT_SCREEN_HEIGHT

        for doc, _ in self.fitz_docs: doc.close()
        self.fitz_docs, self.page_map = [], []

        if self.font_path and os.path.exists(self.font_path):
            css_font_path = self.font_path.replace("\\", "/")
            font_face_rule = f'@font-face {{ font-family: "CustomFont"; src: url("{css_font_path}"); }}'
            font_family_val = '"CustomFont"'
        else:
            font_face_rule = ""
            font_family_val = "serif"

        patched_css = fix_css_font_paths(self.book_css, font_family_val)
        custom_css = f"""
        <style>
            {font_face_rule}
            @page {{ size: {self.screen_width}pt {self.screen_height}pt; margin: 0; }}
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
            h1, h2, h3 {{ text-align: center !important; margin-top: 1em; font-weight: {min(900, self.font_weight + 200)} !important; }}
        </style>
        """

        temp_chapter_starts = []
        running_page_count = 0
        final_toc_titles = []

        progress_bar = st.progress(0)
        status_text = st.empty()
        total_chapters = len(self.raw_chapters)

        for idx, chapter in enumerate(self.raw_chapters):
            status_text.text(f"Rendering chapter {idx + 1}/{total_chapters}...")
            progress_bar.progress(int((idx / total_chapters) * 90))

            soup = chapter['soup']
            for img_tag in soup.find_all('img'):
                src = os.path.basename(img_tag.get('src', ''))
                if src in self.book_images: img_tag['src'] = self.book_images[src]

            soup = hyphenate_html_text(soup, self.book_lang)

            if idx in selected_indices_set:
                temp_chapter_starts.append(running_page_count)
                final_toc_titles.append(chapter['title'])

            body_content = "".join([str(x) for x in soup.body.contents]) if soup.body else str(soup)
            final_html = f"<html lang='{self.book_lang}'><head><style>{patched_css}</style>{custom_css}</head><body>{body_content}</body></html>"

            temp_html_path = os.path.join(self.temp_dir.name, f"render_{idx}.html")
            with open(temp_html_path, "w", encoding="utf-8") as f:
                f.write(final_html)

            doc = fitz.open(temp_html_path)
            rect = fitz.Rect(0, 0, self.screen_width, self.screen_height)
            doc.layout(rect=rect)

            self.fitz_docs.append((doc, chapter['has_image']))
            for i in range(len(doc)): self.page_map.append((len(self.fitz_docs) - 1, i))
            running_page_count += len(doc)

        if add_toc and final_toc_titles:
            toc_header_space = 100 + self.top_padding
            self.toc_row_height = int(self.font_size * self.line_height * 1.2)
            available_h = self.screen_height - self.bottom_padding - toc_header_space

            self.toc_items_per_page = max(1, int(available_h // self.toc_row_height))
            num_toc_pages = (len(final_toc_titles) + self.toc_items_per_page - 1) // self.toc_items_per_page

            self.toc_data_final = [(t, temp_chapter_starts[i] + num_toc_pages + 1) for i, t in
                                   enumerate(final_toc_titles)]
            self.toc_pages_images = self._render_toc_pages(self.toc_data_final)
        else:
            self.toc_data_final = [(t, temp_chapter_starts[i] + 1) for i, t in enumerate(final_toc_titles)]
            self.toc_pages_images = []

        self.total_pages = len(self.toc_pages_images) + len(self.page_map)

        status_text.empty()
        progress_bar.empty()
        self.is_ready = True
        return True

    def _get_ui_font(self, size):
        if self.font_path and os.path.exists(self.font_path):
            return get_pil_font(self.font_path, int(size))
        return ImageFont.load_default()

    def _render_toc_pages(self, toc_entries):
        pages = []
        main_size = self.font_size
        header_size = int(self.font_size * 1.2)
        font_main = self._get_ui_font(main_size)
        font_header = self._get_ui_font(header_size)
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
            draw.text(((self.screen_width - header_w) // 2, header_y), header_text, font=font_header, fill=0,
                      stroke_width=header_stroke)
            line_y = header_y + int(header_size * 1.5)
            draw.line((left_margin, line_y, self.screen_width - right_margin, line_y), fill=0)
            y = line_y + int(main_size * 1.2)

            for title, pg_num in chunk:
                pg_str = str(pg_num)
                pg_w = font_main.getlength(pg_str)
                max_title_w = self.screen_width - left_margin - right_margin - pg_w - column_gap
                display_title = title
                try:
                    if font_main.getlength(display_title) > max_title_w:
                        while font_main.getlength(display_title + "...") > max_title_w and len(display_title) > 0:
                            display_title = display_title[:-1]
                        display_title += "..."
                except:
                    pass

                draw.text((left_margin, y), display_title, font=font_main, fill=0, stroke_width=base_stroke)
                title_end_x = left_margin + font_main.getlength(display_title) + 5
                dots_end_x = self.screen_width - right_margin - pg_w - 10
                if dots_end_x > title_end_x:
                    try:
                        dot_char = "."
                        dot_w = font_main.getlength(dot_char)
                        if dot_w > 0:
                            dots_count = int((dots_end_x - title_end_x) / dot_w)
                            draw.text((title_end_x, y), "." * dots_count, font=font_main, fill=0)
                    except:
                        pass

                draw.text((self.screen_width - right_margin - pg_w, y), pg_str, font=font_main, fill=0,
                          stroke_width=base_stroke)
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

        # --- DYNAMIC FOOTER RENDERING ---
        if self.footer_visible:
            draw = ImageDraw.Draw(img)

            # 1. Clean background for footer area based on user margin + buffer
            clean_start_y = self.screen_height - self.bottom_padding
            draw.rectangle([0, clean_start_y, self.screen_width, self.screen_height], fill=(255, 255, 255))

            # 2. Calculate Layout
            font_ui = self._get_ui_font(self.footer_font_size)
            text_height_px = int(self.footer_font_size * 1.3)
            bar_thickness = self.footer_bar_height if self.footer_show_progress else 0

            element_gap = 5

            # Position Reference: footer_bottom_margin is distance from BOTTOM of screen
            base_y = self.screen_height - self.footer_bottom_margin

            bar_y = 0
            text_y = 0

            if self.footer_text_pos == "Text Above Bar":
                # Layout from bottom up: Margin -> Bar -> Gap -> Text
                if self.footer_show_progress:
                    bar_y = base_y - bar_thickness
                    text_ref = bar_y - element_gap
                else:
                    text_ref = base_y

                text_y = text_ref - text_height_px + 3  # +3 adjustment for font baseline

            else:  # "Text Below Bar"
                # Layout from bottom up: Margin -> Text -> Gap -> Bar
                has_text = self.footer_show_pagenum or self.footer_show_title
                if has_text:
                    text_y = base_y - text_height_px
                    bar_ref = text_y - element_gap
                else:
                    bar_ref = base_y

                bar_y = bar_ref - bar_thickness

            # 3. Draw Progress Bar
            if self.footer_show_progress:
                draw.rectangle([10, bar_y, self.screen_width - 10, bar_y + bar_thickness], fill=(255, 255, 255),
                               outline=(0, 0, 0))

                # Chapters ticks
                chapter_pages = [item[1] for item in self.toc_data_final]
                for cp in chapter_pages:
                    if self.total_pages > 0:
                        mx = int(((cp - 1) / self.total_pages) * (self.screen_width - 20)) + 10
                        draw.line([mx, bar_y - 1, mx, bar_y + bar_thickness + 1], fill=(0, 0, 0), width=1)

                # Fill progress
                page_num_disp = global_page_index + 1
                if self.total_pages > 0:
                    bw = int((page_num_disp / self.total_pages) * (self.screen_width - 20))
                    draw.rectangle([10, bar_y, 10 + bw, bar_y + bar_thickness], fill=(0, 0, 0))

            # 4. Draw Text Elements
            has_text = self.footer_show_pagenum or self.footer_show_title
            if has_text:
                page_num_disp = global_page_index + 1
                current_title = ""
                for title, start_pg in reversed(self.toc_data_final):
                    if page_num_disp >= start_pg:
                        current_title = title
                        break

                cursor_x = 15

                # Draw Page Num
                if self.footer_show_pagenum:
                    pg_text = f"{page_num_disp}/{self.total_pages}"
                    draw.text((cursor_x, text_y), pg_text, font=font_ui, fill=(0, 0, 0))
                    cursor_x += font_ui.getlength(pg_text) + 15

                    if self.footer_show_title and current_title:
                        draw.text((cursor_x - 10, text_y), "|", font=font_ui, fill=(0, 0, 0))

                # Draw Title
                if self.footer_show_title and current_title:
                    available_width = self.screen_width - cursor_x - 10
                    display_title = current_title
                    if font_ui.getlength(display_title) > available_width:
                        while font_ui.getlength(display_title + "...") > available_width and len(display_title) > 0:
                            display_title = display_title[:-1]
                        display_title += "..."
                    draw.text((cursor_x, text_y), display_title, font=font_ui, fill=(0, 0, 0))

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

    st.markdown("""
    <style>
        section[data-testid="stSidebar"] { width: 400px !important; }
        .block-container { padding-top: 1rem; padding-bottom: 1rem; }
        header[data-testid="stHeader"] { background-color: rgba(0,0,0,0); }
        header[data-testid="stHeader"] > div:first-child { background: transparent; }
    </style>
    """, unsafe_allow_html=True)

    if 'processor' not in st.session_state: st.session_state.processor = EpubProcessor()
    if 'current_page' not in st.session_state: st.session_state.current_page = 0
    if 'last_config' not in st.session_state: st.session_state.last_config = {}
    if 'selected_chapter_indices' not in st.session_state: st.session_state.selected_chapter_indices = []

    st.markdown("<h3 style='margin-bottom: 0.5rem; text-align: center;'>📘 EPUB → XTC Converter</h3>",
                unsafe_allow_html=True)

    # --- SIDEBAR ---
    with st.sidebar:
        if st.session_state.processor.is_ready:
            st.success("✅ Book Ready")
            if st.button("Download XTC", type="primary", use_container_width=True):
                with st.spinner("Generating..."):
                    xtc_data = st.session_state.processor.get_xtc_bytes()
                    st.download_button("Save File", data=xtc_data, file_name="book.xtc",
                                       mime="application/octet-stream")
            st.divider()

        st.header("1. Input")
        uploaded_file = st.file_uploader("Upload EPUB", type=["epub"])
        uploaded_font = st.file_uploader("Custom Font (TTF)", type=["ttf"])

        if uploaded_file:
            file_key = f"{uploaded_file.name}_{uploaded_file.size}"
            if 'file_key' not in st.session_state or st.session_state.file_key != file_key:
                st.session_state.file_key = file_key
                st.session_state.processor = EpubProcessor()
                with st.spinner("Parsing book structure..."):
                    success, msg = st.session_state.processor.parse_structure(uploaded_file.getvalue())
                    if success:
                        st.session_state.current_page = 0
                        st.session_state.selected_chapter_indices = list(
                            range(len(st.session_state.processor.raw_chapters)))
                    else:
                        st.error(msg)

        # --- PRESET MANAGEMENT (NEW) ---
        st.divider()
        with st.expander("Presets (Save/Load)", expanded=False):
            # Uploader
            uploaded_preset = st.file_uploader("Load Preset (JSON)", type=["json"])
            if uploaded_preset:
                try:
                    loaded_data = json.load(uploaded_preset)
                    # Update session state keys with loaded data
                    # Map config keys to widget keys
                    key_map = {
                        'orientation': 'orientation', 'align': 'align', 'use_toc': 'use_toc',
                        'font_size': 'font_size', 'font_weight': 'font_weight',
                        'line_height': 'line_height', 'margin': 'margin',
                        'top_pad': 'top_pad', 'bot_pad': 'bot_pad',
                        'footer_visible': 'footer_visible',
                        'footer_show_progress': 'footer_show_progress',
                        'footer_show_pagenum': 'footer_show_pagenum',
                        'footer_show_title': 'footer_show_title',
                        'footer_text_pos': 'footer_text_pos',
                        'footer_font_size': 'footer_font_size',
                        'footer_bar_height': 'footer_bar_height',
                        'footer_bottom_margin': 'footer_bottom_margin'
                    }

                    for json_key, ss_key in key_map.items():
                        if json_key in loaded_data:
                            st.session_state[ss_key] = loaded_data[json_key]

                    st.success("Preset loaded! (UI updated)")
                except Exception as e:
                    st.error(f"Failed to load preset: {e}")

            # Downloader (Needs current config from session state)
            # We construct the dict from session_state values if they exist, else defaults
            current_ss_config = {
                'orientation': st.session_state.get('orientation', "Portrait"),
                'align': st.session_state.get('align', "justify"),
                'use_toc': st.session_state.get('use_toc', True),
                'font_size': st.session_state.get('font_size', DEFAULT_FONT_SIZE),
                'font_weight': st.session_state.get('font_weight', DEFAULT_FONT_WEIGHT),
                'line_height': st.session_state.get('line_height', DEFAULT_LINE_HEIGHT),
                'margin': st.session_state.get('margin', DEFAULT_MARGIN),
                'top_pad': st.session_state.get('top_pad', DEFAULT_TOP_PADDING),
                'bot_pad': st.session_state.get('bot_pad', DEFAULT_BOTTOM_PADDING),
                'footer_visible': st.session_state.get('footer_visible', True),
                'footer_show_progress': st.session_state.get('footer_show_progress', True),
                'footer_show_pagenum': st.session_state.get('footer_show_pagenum', True),
                'footer_show_title': st.session_state.get('footer_show_title', True),
                'footer_text_pos': st.session_state.get('footer_text_pos', "Text Above Bar"),
                'footer_font_size': st.session_state.get('footer_font_size', 16),
                'footer_bar_height': st.session_state.get('footer_bar_height', 4),
                'footer_bottom_margin': st.session_state.get('footer_bottom_margin', 10)
            }

            json_str = json.dumps(current_ss_config, indent=4)
            st.download_button(
                label="Download Current Preset",
                data=json_str,
                file_name="my_preset.json",
                mime="application/json"
            )

        st.divider()

        if st.session_state.processor.is_parsed:
            with st.expander("Chapter Visibility (TOC)", expanded=False):
                st.info("Unchecked chapters are hidden from TOC but remain in book.")
                all_titles = [f"{i + 1}. {c['title']}" for i, c in enumerate(st.session_state.processor.raw_chapters)]
                selected_titles = st.multiselect("Include in Navigation:", all_titles, default=all_titles)
                st.session_state.selected_chapter_indices = [all_titles.index(t) for t in selected_titles]
            st.divider()

        st.header("2. Settings")
        current_config = {}
        r1_c1, r1_c2 = st.columns(2)
        with r1_c1:
            current_config['orientation'] = st.selectbox("Orientation", ["Portrait", "Landscape"], key="orientation")
        with r1_c2:
            current_config['align'] = st.selectbox("Alignment", ["justify", "left"], key="align")

        current_config['use_toc'] = st.checkbox("Generate TOC Pages", value=True, key="use_toc")

        st.subheader("Typography")
        r2_c1, r2_c2 = st.columns(2)
        with r2_c1:
            current_config['font_size'] = st.number_input("Font Size", 10, 50, DEFAULT_FONT_SIZE, key="font_size")
        with r2_c2:
            current_config['font_weight'] = st.number_input("Weight", 100, 900, DEFAULT_FONT_WEIGHT, step=100,
                                                            key="font_weight")

        r3_c1, r3_c2 = st.columns(2)
        with r3_c1:
            current_config['line_height'] = st.number_input("Line Height", 1.0, 3.0, DEFAULT_LINE_HEIGHT, step=0.1,
                                                            key="line_height")
        with r3_c2:
            current_config['margin'] = st.number_input("Margin", 0, 100, DEFAULT_MARGIN, key="margin")

        st.subheader("Spacing")
        r4_c1, r4_c2 = st.columns(2)
        with r4_c1:
            current_config['top_pad'] = st.number_input("Top Padding", 0, 100, DEFAULT_TOP_PADDING, key="top_pad")
        with r4_c2:
            current_config['bot_pad'] = st.number_input("Bottom Padding", 0, 100, DEFAULT_BOTTOM_PADDING, key="bot_pad")

        # --- FOOTER SETTINGS (NEW) ---
        st.divider()
        st.subheader("Footer Settings")
        current_config['footer_visible'] = st.checkbox("Show Footer Area", value=True, key="footer_visible")

        if current_config['footer_visible']:
            f_col1, f_col2 = st.columns(2)

            with f_col1:
                st.caption("Content")
                current_config['footer_show_progress'] = st.checkbox("Progress Bar", value=True,
                                                                     key="footer_show_progress")
                current_config['footer_show_pagenum'] = st.checkbox("Page Numbers", value=True,
                                                                    key="footer_show_pagenum")
                current_config['footer_show_title'] = st.checkbox("Chapter Title", value=True, key="footer_show_title")
                current_config['footer_text_pos'] = st.selectbox("Text Position", ["Text Above Bar", "Text Below Bar"],
                                                                 key="footer_text_pos")

            with f_col2:
                st.caption("Style")
                current_config['footer_font_size'] = st.number_input("Text Size", 10, 24, 16, key="footer_font_size")
                current_config['footer_bar_height'] = st.number_input("Bar Thickness", 1, 10, 4,
                                                                      key="footer_bar_height")
                # LABEL RENAMED HERE
                current_config['footer_bottom_margin'] = st.number_input("Footer Position", 0, 100, 10,
                                                                         key="footer_bottom_margin")
        else:
            # Default hidden values if footer is off (to keep consistent state if toggled back)
            current_config['footer_show_progress'] = st.session_state.get('footer_show_progress', True)
            current_config['footer_show_pagenum'] = st.session_state.get('footer_show_pagenum', True)
            current_config['footer_show_title'] = st.session_state.get('footer_show_title', True)
            current_config['footer_text_pos'] = st.session_state.get('footer_text_pos', "Text Above Bar")
            current_config['footer_font_size'] = st.session_state.get('footer_font_size', 16)
            current_config['footer_bar_height'] = st.session_state.get('footer_bar_height', 4)
            current_config['footer_bottom_margin'] = st.session_state.get('footer_bottom_margin', 10)

        st.divider()
        st.header("3. View")
        preview_width = st.slider("Preview Zoom", 200, 800, 350)

        if st.session_state.processor.is_parsed:
            if st.button("Apply Changes / Render", type="primary"):
                st.session_state.force_render = True

    # --- MAIN RENDER LOGIC ---

    font_path = ""
    if uploaded_font:
        try:
            tfile = tempfile.NamedTemporaryFile(delete=False, suffix=".ttf")
            tfile.write(uploaded_font.getvalue())
            font_path = tfile.name
            tfile.close()
            current_config['font_sig'] = uploaded_font.name
        except:
            pass

    current_config['selected_indices_tuple'] = tuple(sorted(st.session_state.selected_chapter_indices))

    should_render = (
            st.session_state.processor.is_parsed
            and (
                    current_config != st.session_state.last_config
                    or not st.session_state.processor.is_ready
                    or st.session_state.get('force_render', False)
            )
    )

    if should_render:
        st.session_state.force_render = False
        relative_pos = 0.0
        if st.session_state.processor.is_ready and st.session_state.processor.total_pages > 0:
            relative_pos = st.session_state.current_page / st.session_state.processor.total_pages

        # Build Footer Settings Dict
        footer_settings = {
            "footer_visible": current_config['footer_visible'],
            "footer_font_size": current_config['footer_font_size'],
            "footer_show_progress": current_config['footer_show_progress'],
            "footer_show_pagenum": current_config['footer_show_pagenum'],
            "footer_show_title": current_config['footer_show_title'],
            "footer_text_pos": current_config['footer_text_pos'],
            "footer_bar_height": current_config['footer_bar_height'],
            "footer_bottom_margin": current_config['footer_bottom_margin']
        }

        with st.spinner("Rendering layout... (Step 2/2)"):
            success = st.session_state.processor.render_chapters(
                set(st.session_state.selected_chapter_indices),
                font_path,
                current_config['font_size'],
                current_config['margin'],
                current_config['line_height'],
                current_config['font_weight'],
                current_config['bot_pad'],
                current_config['top_pad'],
                current_config['align'],
                current_config['orientation'],
                current_config['use_toc'],
                footer_settings=footer_settings
            )

            if success:
                st.session_state.last_config = current_config
                new_total = st.session_state.processor.total_pages
                st.session_state.current_page = int(relative_pos * new_total)
                st.session_state.current_page = min(max(0, st.session_state.current_page), new_total - 1)
                st.rerun()

    # --- DISPLAY AREA ---

    if st.session_state.processor.is_ready:

        # 1. Top Nav
        c1, c2, c3 = st.columns([1, 2, 1])
        with c1:
            if st.button("⬅ Previous", use_container_width=True):
                st.session_state.current_page = max(0, st.session_state.current_page - 1)
        with c2:
            st.markdown(
                f"""<div style="text-align:center; padding-top: 5px; font-size:1.1rem; color: #444;">
                    Page <b>{st.session_state.current_page + 1}</b> / {st.session_state.processor.total_pages}
                </div>""", unsafe_allow_html=True
            )
        with c3:
            if st.button("Next ➡", use_container_width=True):
                st.session_state.current_page = min(
                    st.session_state.processor.total_pages - 1, st.session_state.current_page + 1
                )

        # 2. Render Image
        img = st.session_state.processor.render_page(st.session_state.current_page)

        # Smart Scaling for Preview
        base_size = int(preview_width)
        if img.width > img.height:
            target_h = base_size
            target_w = int(target_h * (img.width / img.height))
        else:
            target_w = base_size
            target_h = int(target_w * (img.height / img.width))

        preview_img = img.copy().resize((target_w, target_h), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(preview_img)
        draw.rectangle([(0, 0), (target_w - 1, target_h - 1)], outline="black", width=2)

        with io.BytesIO() as buffer:
            preview_img.save(buffer, format="PNG")
            img_b64 = base64.b64encode(buffer.getvalue()).decode()

        st.markdown(
            f"""<div style="display: flex; justify-content: center; margin-top: 15px;">
                <img src="data:image/png;base64,{img_b64}" width="{target_w}" style="max-width: 100%; box-shadow: 0px 4px 15px rgba(0,0,0,0.15);">
            </div>""", unsafe_allow_html=True
        )

        # 3. Bottom Nav (Go To Page)
        st.divider()
        b1, b2, b3 = st.columns([5, 2, 5])
        with b2:
            def update_page():
                val = st.session_state.goto_input
                if 0 < val <= st.session_state.processor.total_pages:
                    st.session_state.current_page = val - 1

            st.number_input(
                "Jump to page:",
                min_value=1,
                max_value=st.session_state.processor.total_pages,
                value=st.session_state.current_page + 1,
                key="goto_input",
                on_change=update_page
            )

    else:
        st.info("👈 Please upload an EPUB file in the sidebar to begin.")


if __name__ == "__main__":
    main()
