import streamlit as st
import os
import struct
import fitz  # PyMuPDF
from PIL import Image, ImageEnhance, ImageDraw, ImageFont, ImageOps, ImageFilter
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, NavigableString
import pyphen
import base64
import re
import tempfile
import io
import json
import zipfile
import shutil
from urllib.parse import unquote

# --- CONFIGURATION DEFAULTS ---
DEFAULT_SCREEN_WIDTH = 480
DEFAULT_SCREEN_HEIGHT = 800
DEFAULT_RENDER_SCALE = 3.0
DEFAULT_FONT_SIZE = 28
DEFAULT_MARGIN = 20
DEFAULT_LINE_HEIGHT = 1.4
DEFAULT_FONT_WEIGHT = 400
DEFAULT_BOTTOM_PADDING = 45
DEFAULT_TOP_PADDING = 15
DEFAULT_TEXT_THRESHOLD = 130
DEFAULT_TEXT_BLUR = 0.7

# --- SYSTEM FONTS (FITZ / BASE-14) ---
FITZ_FONTS = {
    "--- SERIF (Book Standard) ---": "Times-Roman",
    "Serif: Times Roman": "Times-Roman",
    "Serif: Times Bold (High Contrast)": "Times-Bold",
    "Serif: Generic": "serif",

    "--- SANS-SERIF (Clean/Modern) ---": "Helvetica",
    "Sans: Helvetica": "Helvetica",
    "Sans: Helvetica Bold (High Contrast)": "Helvetica-Bold",
    "Sans: Generic": "sans-serif",

    "--- MONOSPACE (Typewriter) ---": "Courier",
    "Mono: Courier": "Courier",
    "Mono: Courier Bold": "Courier-Bold",
    "Mono: Generic": "monospace",
}


# --- UTILITY FUNCTIONS ---

def fix_css_font_paths(css_text, target_font_family="'CustomFont'"):
    if target_font_family is None:
        return css_text
    css_text = re.sub(r'font-family\s*:\s*[^;!]+', f'font-family: {target_font_family}', css_text)
    return css_text


def get_font_variants(directory):
    try:
        all_files = [f for f in os.listdir(directory) if f.lower().endswith((".ttf", ".otf"))]
    except:
        return {}

    candidates = {
        "regular": [],
        "italic": [],
        "bold": [],
        "bold_italic": []
    }

    for f in all_files:
        full_path = os.path.join(directory, f).replace("\\", "/")
        name_lower = f.lower()

        has_bold = any(x in name_lower for x in ["bold", "bd"])
        has_italic = any(x in name_lower for x in ["italic", "oblique", "obl"])

        if has_bold and has_italic:
            candidates["bold_italic"].append(full_path)
        elif has_bold:
            candidates["bold"].append(full_path)
        elif has_italic:
            candidates["italic"].append(full_path)
        else:
            candidates["regular"].append(full_path)

    def pick_best(file_list):
        if not file_list: return None
        return sorted(file_list, key=len)[0]

    results = {
        "regular": pick_best(candidates["regular"]),
        "italic": pick_best(candidates["italic"]),
        "bold": pick_best(candidates["bold"]),
        "bold_italic": pick_best(candidates["bold_italic"])
    }

    if not results["regular"] and all_files:
        results["regular"] = os.path.join(directory, all_files[0]).replace("\\", "/")

    return results


def get_pil_font(font_identifier, size):
    if font_identifier and os.path.exists(font_identifier):
        try:
            return ImageFont.truetype(font_identifier, size)
        except:
            pass

    is_bold = "Bold" in str(font_identifier) or "bd" in str(font_identifier).lower()
    is_serif = "Times" in str(font_identifier) or "serif" in str(font_identifier).lower()
    is_mono = "Courier" in str(font_identifier) or "mono" in str(font_identifier).lower()

    candidates = []

    if is_mono:
        candidates = ["Courier New.ttf", "consola.ttf", "cour.ttf"]
        if is_bold: candidates = ["courbd.ttf", "consolab.ttf"] + candidates
    elif is_serif:
        candidates = ["Times New Roman.ttf", "times.ttf", "Georgia.ttf"]
        if is_bold: candidates = ["timesbd.ttf", "georgiab.ttf"] + candidates
    else:
        candidates = ["Arial.ttf", "Helvetica.ttf", "Verdana.ttf"]
        if is_bold: candidates = ["arialbd.ttf", "verdanab.ttf"] + candidates

    candidates.extend(["DejaVuSans.ttf", "FreeSans.ttf", "Arial.ttf", "arial.ttf"])

    for font_name in candidates:
        try:
            return ImageFont.truetype(font_name, size)
        except:
            continue

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
    """
    Returns a mapping of {filename: [(anchor, title), ...]}
    """
    mapping = {}

    def add_entry(href, title):
        if '#' in href:
            href_clean, anchor = href.split('#', 1)
        else:
            href_clean, anchor = href, None

        filename = os.path.basename(href_clean)

        if filename not in mapping:
            mapping[filename] = []

        mapping[filename].append((anchor, title))

    # 1. Try Standard TOC
    def process_toc_item(item):
        if isinstance(item, tuple):
            if len(item) > 1 and isinstance(item[1], list):
                for sub in item[1]: process_toc_item(sub)
        elif isinstance(item, epub.Link):
            add_entry(item.href, item.title)

    for item in book.toc:
        process_toc_item(item)

    # 2. Fallback to Nav Document if empty
    if not mapping:
        nav_item = next((item for item in book.get_items()
                         if item.get_type() == ebooklib.ITEM_NAVIGATION), None)
        if nav_item:
            try:
                soup = BeautifulSoup(nav_item.get_content(), 'html.parser')
                nav_element = soup.find('nav', attrs={'epub:type': 'toc'}) or soup.find('nav')
                if nav_element:
                    for link in nav_element.find_all('a', href=True):
                        add_entry(link['href'], link.get_text().strip())
            except:
                pass

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
        self.cover_image_obj = None
        self.global_id_map = {}

        # Step 2 Data
        self.fitz_docs = []
        self.toc_data_final = []
        self.toc_pages_images = []
        self.page_map = []
        self.total_pages = 0
        self.toc_items_per_page = 18
        self.is_ready = False
        self.temp_dir = tempfile.TemporaryDirectory()

        # Layout Settings
        self.layout_settings = {}

        # Font Data
        self.font_data = {}
        self.ui_font_ref = None

    # --- FOOTNOTE & CONTENT EXTRACTION HELPERS ---
    def _smart_extract_content(self, elem):
        if elem.name == 'a':
            parent = elem.parent
            if parent and parent.name not in ['body', 'html', 'section']:
                return parent
            return elem
        if elem.name in ['aside', 'li', 'dd', 'div']:
            return elem
        text = elem.get_text(strip=True)
        if len(text) > 1:
            return elem
        parent = elem.parent
        if parent:
            if parent.name in ['body', 'html', 'section']:
                return elem
            return parent
        return elem

    def _build_global_id_map(self, book):
        id_map = {}
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            try:
                soup = BeautifulSoup(item.get_content(), 'html.parser')
                filename = os.path.basename(item.get_name())
                for elem in soup.find_all(id=True):
                    target_node = self._smart_extract_content(elem)
                    import copy
                    content_node = copy.copy(target_node)
                    original_raw_html = content_node.decode_contents().strip()

                    for a in content_node.find_all('a'):
                        if a.get('role') in ['doc-backlink', 'doc-noteref']:
                            a.decompose()
                            continue
                        text = a.get_text(strip=True)
                        if any(x in text for x in ['↑', 'site', 'back', 'return', '↩']):
                            a.decompose()
                            continue
                        if len(text) < 5 and re.match(r'^[\s\[\(]*\d+[\.\)\]]*$', text):
                            a.decompose()
                            continue

                    final_html = content_node.decode_contents().strip()
                    if not final_html and original_raw_html:
                        final_html = original_raw_html
                    if final_html:
                        id_map[f"{filename}#{elem['id']}"] = final_html
            except Exception:
                pass
        return id_map

    def _inject_inline_footnotes(self, soup, current_filename):
        if not self.global_id_map: return soup
        links = soup.find_all('a', href=True)
        for link in reversed(list(links)):
            raw_href = link['href']
            href = unquote(raw_href)
            text = link.get_text(strip=True)
            if not text and not link.find('sup'): continue

            parent_classes = []
            for parent in link.parents:
                if parent.get('class'): parent_classes.extend(parent.get('class'))
            if any(x in [c.lower() for c in parent_classes] for x in
                   ['footnote', 'endnote', 'reflist', 'bibliography']):
                continue

            is_footnote = False
            if 'noteref' in link.get('epub:type', '') or link.get('role') == 'doc-noteref': is_footnote = True
            css = link.get('class', [])
            if isinstance(css, list): css = " ".join(css)
            if any(x in css.lower() for x in ['footnote', 'noteref', 'ref']): is_footnote = True
            if not is_footnote and text:
                clean_t = text.strip()
                if re.match(r'^[\(\[]?\d+[\)\]]?$', clean_t) or clean_t == '*':
                    is_footnote = True
                elif re.match(r'^[\(\[]?[ivx]+[\)\]]?$', clean_t.lower()):
                    is_footnote = True

            if not is_footnote: continue

            content = None
            if '#' in href:
                parts = href.rsplit('#', 1)
                href_path = parts[0]
                href_id = parts[1]
                f_name = os.path.basename(href_path) if href_path else current_filename
                key = f"{f_name}#{href_id}"
                content = self.global_id_map.get(key)
                if not content:
                    suffix = f"#{href_id}"
                    for k, v in self.global_id_map.items():
                        if k.endswith(suffix):
                            content = v
                            break

            if content:
                new_marker = soup.new_tag("sup")
                new_marker.string = text if text else "*"
                new_marker['class'] = "fn-marker"
                link.replace_with(new_marker)
                note_box = soup.new_tag("div")
                note_box['class'] = "inline-footnote-box"
                header = soup.new_tag("strong")
                header.string = f"{text}: "
                note_box.append(header)
                content_soup = BeautifulSoup(content, 'html.parser')
                note_box.append(content_soup)
                parent_block = new_marker.find_parent(['p', 'div', 'li', 'h1', 'h2', 'blockquote'])
                if parent_block:
                    parent_block.insert_after(note_box)
                else:
                    new_marker.insert_after(note_box)
        return soup

    def _find_cover_image(self, book):
        # 1. Try Metadata
        try:
            cover_data = book.get_metadata('OPF', 'cover')
            if cover_data:
                cover_id = cover_data[0][1]
                item = book.get_item_with_id(cover_id)
                if item:
                    return Image.open(io.BytesIO(item.get_content()))
        except:
            pass
        # 2. Try Item Names
        for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            name = item.get_name().lower()
            if 'cover' in name:
                return Image.open(io.BytesIO(item.get_content()))
        # 3. Fallback: First image
        for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            return Image.open(io.BytesIO(item.get_content()))
        return None

    def _split_html_by_toc(self, soup, toc_entries):
        """
        Splits a single BeautifulSoup object into multiple soup objects
        based on the anchors provided in toc_entries.
        """
        # Sort entries to ensure we process in order of appearance
        # We need to find where these anchors physically sit in the DOM

        chunks = []

        # If the file has no anchors (just 1 chapter), return as is
        if len(toc_entries) == 1 and not toc_entries[0][0]:
            return [{'title': toc_entries[0][1], 'soup': soup}]

        # 1. Locate all split points
        split_points = []
        for anchor, title in toc_entries:
            target = None
            if anchor:
                target = soup.find(id=anchor)

            # If we found the target, or if it's the start (anchor=None), add to list
            if target or not anchor:
                split_points.append({'node': target, 'title': title})

        if not split_points:
            # Fallback: couldn't find IDs, return whole file as first chapter
            return [{'title': toc_entries[0][1], 'soup': soup}]

        # 2. Iterate and extract content
        # Strategy: Iterate over body children. Assign them to current_chapter
        # until we hit a child that contains (or is) the next split_point.

        current_idx = 0
        current_soup = BeautifulSoup("<body></body>", 'html.parser')

        body_children = list(soup.body.children) if soup.body else []

        for child in body_children:
            if isinstance(child, NavigableString) and not child.strip():
                # Just whitespace, append to current
                if current_soup.body: current_soup.body.append(child.extract() if hasattr(child, 'extract') else child)
                continue

            # Check if we moved to the next section
            # Is this child the start node? Or does it contain the start node?
            if current_idx + 1 < len(split_points):
                next_node = split_points[current_idx + 1]['node']

                # --- FIX STARTS HERE ---
                # Safe check: Only look inside if the child supports searching (e.g. is a Tag)
                is_nested_target = False
                if hasattr(child, 'find_all'):
                    if next_node in child.find_all():
                        is_nested_target = True

                if next_node and (child == next_node or is_nested_target):
                    # --- FIX ENDS HERE ---

                    # We hit the next chapter marker!
                    # Save current chunk
                    chunks.append({'title': split_points[current_idx]['title'], 'soup': current_soup})

                    # Start new chunk
                    current_idx += 1
                    current_soup = BeautifulSoup("<body></body>", 'html.parser')

            # Append child to current soup
            # We copy it to avoid destroying original if needed, but extraction is faster
            if current_soup.body:
                current_soup.body.append(child)

        # Add the final chunk
        chunks.append({'title': split_points[current_idx]['title'], 'soup': current_soup})

        return chunks

    # --- STEP 1: PARSE STRUCTURE (FAST) ---
    def parse_structure(self, epub_bytes):
        self.raw_chapters = []
        self.cover_image_obj = None

        # Save bytes to temp file because EbookLib needs a file path usually,
        # or we use BytesIO but your code used temp file strategy.
        epub_temp_path = os.path.join(self.temp_dir.name, "input.epub")
        with open(epub_temp_path, "wb") as f:
            f.write(epub_bytes)

        try:
            book = epub.read_epub(epub_temp_path)
        except Exception as e:
            return False, f"Error reading EPUB: {e}"

        self.cover_image_obj = self._find_cover_image(book)
        self.global_id_map = self._build_global_id_map(book)

        try:
            self.book_lang = book.get_metadata('DC', 'language')[0][0]
        except:
            self.book_lang = 'en'

        self.book_images = extract_images_to_base64(book)
        self.book_css = extract_all_css(book)

        # NEW: Get the list-based mapping
        toc_mapping = get_official_toc_mapping(book)

        items = [book.get_item_with_id(item_ref[0]) for item_ref in book.spine
                 if isinstance(book.get_item_with_id(item_ref[0]), epub.EpubHtml)]

        for item in items:
            item_filename = os.path.basename(item.get_name())
            raw_html = item.get_content().decode('utf-8', errors='replace')
            soup = BeautifulSoup(raw_html, 'html.parser')
            has_image = bool(soup.find('img'))

            # Check if this file maps to TOC entries
            toc_entries = toc_mapping.get(item_filename)

            if toc_entries and len(toc_entries) > 1:
                # SPLIT MODE: File contains multiple chapters
                split_chapters = self._split_html_by_toc(soup, toc_entries)
                for chunk in split_chapters:
                    self.raw_chapters.append({
                        'title': chunk['title'],
                        'soup': chunk['soup'],
                        'has_image': bool(chunk['soup'].find('img')),
                        'filename': item_filename
                    })
            else:
                # STANDARD MODE: 1 File = 1 Chapter
                chapter_title = toc_entries[0][1] if toc_entries else None

                # Fallback Title logic
                if not chapter_title:
                    for tag in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
                        header = soup.find(tag)
                        if header:
                            t = header.get_text().strip()
                            if t and len(t) < 150:
                                chapter_title = t
                                break
                    if not chapter_title:
                        chapter_title = f"Section {len(self.raw_chapters) + 1}"

                self.raw_chapters.append({
                    'title': chapter_title,
                    'soup': soup,
                    'has_image': has_image,
                    'filename': item_filename
                })

        self.is_parsed = True
        return True, "Success"

    # --- HEADER / FOOTER DRAWING FUNCTIONS ---
    def _draw_progress_bar(self, draw, y, height, global_page_index):
        if self.total_pages <= 0: return
        s = self.layout_settings

        show_ticks = s.get("bar_show_ticks", True)
        tick_h = s.get("bar_tick_height", 6)
        show_marker = s.get("bar_show_marker", True)
        marker_r = s.get("bar_marker_radius", 5)
        marker_col_str = s.get("bar_marker_color", "Black")
        marker_fill = (255, 255, 255) if marker_col_str == "White" else (0, 0, 0)

        draw.rectangle([10, y, self.screen_width - 10, y + height], fill=(255, 255, 255), outline=(0, 0, 0))

        if show_ticks:
            bar_center_y = y + (height / 2)
            t_top = bar_center_y - (tick_h / 2)
            t_bot = bar_center_y + (tick_h / 2)
            chapter_pages = [item[1] for item in self.toc_data_final]
            for cp in chapter_pages:
                mx = int(((cp - 1) / self.total_pages) * (self.screen_width - 20)) + 10
                draw.line([mx, t_top, mx, t_bot], fill=(0, 0, 0), width=1)

        curr_page_disp = global_page_index + 1
        bar_width_px = self.screen_width - 20
        fill_width = int((curr_page_disp / self.total_pages) * bar_width_px)

        draw.rectangle([10, y, 10 + fill_width, y + height], fill=(0, 0, 0))

        if show_marker:
            cx = 10 + fill_width
            cy = y + (height / 2)
            draw.ellipse([cx - marker_r, cy - marker_r, cx + marker_r, cy + marker_r],
                         fill=marker_fill, outline=(0, 0, 0))

    def _get_page_text_elements(self, global_page_index):
        page_num_disp = global_page_index + 1
        percent = int((page_num_disp / self.total_pages) * 100) if self.total_pages > 0 else 0
        current_title = ""

        num_toc = len(self.toc_pages_images)
        if global_page_index < num_toc:
            current_title = "Table of Contents"
            chap_page_disp = f"{global_page_index + 1}/{num_toc}"
        else:
            for title, start_pg in reversed(self.toc_data_final):
                if page_num_disp >= start_pg:
                    current_title = title
                    break

            pm_idx = global_page_index - num_toc
            if 0 <= pm_idx < len(self.page_map):
                doc_idx, page_idx = self.page_map[pm_idx]
                doc_ref = self.fitz_docs[doc_idx][0]
                chap_total = len(doc_ref)
                chap_page_disp = f"{page_idx + 1}/{chap_total}"
            else:
                chap_page_disp = "1/1"

        return {
            'pagenum': f"{page_num_disp}/{self.total_pages}",
            'title': current_title,
            'chap_page': chap_page_disp,
            'percent': f"{percent}%"
        }

    # --- ROBUST TEXT LAYOUT ENGINE ---
    def _draw_text_line(self, draw, y, font, elements_list, align):
        if not elements_list: return

        margin_x = 20
        canvas_width = self.screen_width - (margin_x * 2)
        separator = "  |  "
        sep_w = font.getlength(separator)

        title_item = None
        fixed_items = []
        for key, txt in elements_list:
            if key == 'title':
                title_item = txt
            else:
                fixed_items.append(txt)

        fixed_text_w = sum(font.getlength(txt) for txt in fixed_items)
        total_seps_w = sep_w * (len(elements_list) - 1) if len(elements_list) > 1 else 0

        available_for_title = canvas_width - fixed_text_w - total_seps_w

        display_title = title_item if title_item else ""
        if title_item:
            if font.getlength(title_item) > available_for_title:
                t = title_item
                while len(t) > 0 and font.getlength(t + "...") > available_for_title:
                    t = t[:-1]
                display_title = t + "..." if t else ""

        final_strings = []
        for key, txt in elements_list:
            if key == 'title':
                final_strings.append(display_title)
            else:
                final_strings.append(txt)

        final_strings = [s for s in final_strings if s]

        if align == "Justify" and len(final_strings) > 1:
            draw.text((margin_x, y), final_strings[0], font=font, fill=(0, 0, 0))
            last_txt = final_strings[-1]
            last_w = font.getlength(last_txt)
            draw.text((self.screen_width - margin_x - last_w, y), last_txt, font=font, fill=(0, 0, 0))
            if len(final_strings) > 2:
                mid_txt = separator.join(final_strings[1:-1])
                mid_w = font.getlength(mid_txt)
                mid_x = (self.screen_width - mid_w) // 2
                draw.text((mid_x, y), mid_txt, font=font, fill=(0, 0, 0))
        else:
            full_line = separator.join(final_strings)
            line_w = font.getlength(full_line)
            if align == "Center":
                x = (self.screen_width - line_w) // 2
            elif align == "Right":
                x = self.screen_width - margin_x - line_w
            else:
                x = margin_x
            draw.text((x, y), full_line, font=font, fill=(0, 0, 0))

    def _draw_header(self, draw, global_page_index):
        s = self.layout_settings
        font_size = s.get("header_font_size", 16)
        margin = s.get("header_margin", 10)
        align = s.get("header_align", "Center")
        bar_h = s.get("bar_height", 4)
        pos_prog = s.get("pos_progress", "Footer (Below Text)")
        text_data = self._get_page_text_elements(global_page_index)
        elements = self._get_active_elements("Header", text_data)
        font_ui = self._get_ui_font(font_size)
        curr_y = margin
        gap = 6
        if "Header" in pos_prog:
            if "Above" in pos_prog:
                self._draw_progress_bar(draw, curr_y, bar_h, global_page_index)
                curr_y += bar_h + gap
                if elements: self._draw_text_line(draw, curr_y, font_ui, elements, align)
            else:
                if elements:
                    self._draw_text_line(draw, curr_y, font_ui, elements, align)
                    curr_y += font_size + gap
                self._draw_progress_bar(draw, curr_y, bar_h, global_page_index)
        elif elements:
            self._draw_text_line(draw, curr_y, font_ui, elements, align)

    def _draw_footer(self, draw, global_page_index):
        s = self.layout_settings
        font_size = s.get("footer_font_size", 16)
        margin = s.get("footer_margin", 10)
        align = s.get("footer_align", "Center")
        bar_h = s.get("bar_height", 4)
        pos_prog = s.get("pos_progress", "Footer (Below Text)")
        text_data = self._get_page_text_elements(global_page_index)
        elements = self._get_active_elements("Footer", text_data)
        font_ui = self._get_ui_font(font_size)
        gap = 6
        base_y = self.screen_height - margin
        if "Footer" in pos_prog:
            if "Below" in pos_prog:
                bar_y = base_y - bar_h
                text_y = bar_y - gap - font_size
                self._draw_progress_bar(draw, bar_y, bar_h, global_page_index)
                if elements: self._draw_text_line(draw, text_y, font_ui, elements, align)
            else:
                text_y = base_y - font_size
                bar_y = text_y - gap - bar_h
                if elements: self._draw_text_line(draw, text_y, font_ui, elements, align)
                self._draw_progress_bar(draw, bar_y, bar_h, global_page_index)
        elif elements:
            self._draw_text_line(draw, base_y - font_size, font_ui, elements, align)

    def _get_active_elements(self, bar_role, text_data):
        s = self.layout_settings
        active = []
        for key in ['title', 'pagenum', 'chap_page', 'percent']:
            pos_val = s.get(f"pos_{key}", "Hidden")
            if pos_val == bar_role:
                order = int(s.get(f"order_{key}", 99))
                content = text_data.get(key, "")
                if content:
                    active.append((order, key, content))
        active.sort(key=lambda x: x[0])
        return [(x[1], x[2]) for x in active]

    # --- STEP 2: RENDER (SLOW) ---
    def render_chapters(self, selected_indices_set, font_data_input, font_size, margin, line_height, font_weight,
                        bottom_padding, top_padding, text_align, orientation, add_toc, layout_settings=None,
                        show_footnotes=True):

        # --- FONT LOGIC ---
        is_custom_font = isinstance(font_data_input, dict)

        if is_custom_font:
            self.font_data = font_data_input
            self.ui_font_ref = font_data_input.get("regular")
        else:
            # It's a system font name string
            self.font_data = font_data_input
            self.ui_font_ref = font_data_input  # Pass string to pillow loader

        self.font_size = int(font_size)
        self.margin = margin
        self.line_height = line_height
        self.font_weight = font_weight
        self.bottom_padding = bottom_padding
        self.top_padding = top_padding
        self.text_align = text_align
        self.layout_settings = layout_settings if layout_settings else {}

        if orientation == "Landscape":
            self.screen_width = DEFAULT_SCREEN_HEIGHT
            self.screen_height = DEFAULT_SCREEN_WIDTH
        else:
            self.screen_width = DEFAULT_SCREEN_WIDTH
            self.screen_height = DEFAULT_SCREEN_HEIGHT

        for doc, _ in self.fitz_docs: doc.close()
        self.fitz_docs, self.page_map = [], []

        # --- CSS Font Generation ---
        font_rules = []
        font_family_val = "serif"  # Default fallback

        if is_custom_font:
            # Generate @font-face rules for local files
            def add_font_rule(path, weight="normal", style="normal"):
                if path and os.path.exists(path):
                    css_path = path.replace("\\", "/")
                    return f"""@font-face {{ 
                        font-family: "CustomFont"; 
                        src: url("{css_path}"); 
                        font-weight: {weight}; 
                        font-style: {style}; 
                    }}"""
                return ""

            if self.font_data.get("regular"):
                font_rules.append(add_font_rule(self.font_data["regular"], "normal", "normal"))
            if self.font_data.get("bold"):
                font_rules.append(add_font_rule(self.font_data["bold"], "bold", "normal"))
            if self.font_data.get("italic"):
                font_rules.append(add_font_rule(self.font_data["italic"], "normal", "italic"))
            if self.font_data.get("bold_italic"):
                font_rules.append(add_font_rule(self.font_data["bold_italic"], "bold", "italic"))

            font_family_val = '"CustomFont"'
        else:
            # Use System Font Name
            # If it's a generic family (lowercase), don't quote it
            if self.font_data in ["serif", "sans-serif", "monospace", "cursive", "fantasy"]:
                font_family_val = self.font_data
            else:
                font_family_val = f'"{self.font_data}"'

        font_face_block = "\n".join(font_rules)

        patched_css = fix_css_font_paths(self.book_css, font_family_val)
        custom_css = f"""
        <style>
            {font_face_block}
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
            .fn-marker {{ font-weight: bold; font-size: 0.7em !important; vertical-align: super; color: solid black !important; }}
            .inline-footnote-box {{ display: block; margin: 15px 0px; padding: 0px 15px; border-left: 4px solid solid black; font-size: {int(self.font_size * 0.85)}pt !important; line-height: {self.line_height} !important; }}
            .inline-footnote-box p {{ margin: 0 !important; padding: 0 !important; font-size: inherit !important; display: inline; }}
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
            if show_footnotes: soup = self._inject_inline_footnotes(soup, chapter.get('filename', ''))
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
        # We pass self.ui_font_ref to Pillow
        return get_pil_font(self.ui_font_ref, int(size))

    def _render_toc_pages(self, toc_entries):
        pages = []
        main_size = self.font_size
        header_size = int(self.font_size * 1.2)
        font_main = self._get_ui_font(main_size)
        font_header = self._get_ui_font(header_size)
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
            line_y = header_y + int(header_size * 1.5)
            draw.line((left_margin, line_y, self.screen_width - right_margin, line_y), fill=0)
            y = line_y + int(main_size * 1.2)
            for title, pg_num in chunk:
                pg_str = str(pg_num)
                pg_w = font_main.getlength(pg_str)
                max_title_w = self.screen_width - left_margin - right_margin - pg_w - column_gap
                display_title = title
                if font_main.getlength(display_title) > max_title_w:
                    while font_main.getlength(display_title + "...") > max_title_w and len(display_title) > 0:
                        display_title = display_title[:-1]
                    display_title += "..."
                draw.text((left_margin, y), display_title, font=font_main, fill=0)
                title_end_x = left_margin + font_main.getlength(display_title) + 5
                dots_end_x = self.screen_width - right_margin - pg_w - 10
                if dots_end_x > title_end_x:
                    try:
                        dot_w = font_main.getlength(".")
                        if dot_w > 0:
                            dots_count = int((dots_end_x - title_end_x) / dot_w)
                            draw.text((title_end_x, y), "." * dots_count, font=font_main, fill=0)
                    except:
                        pass
                draw.text((self.screen_width - right_margin - pg_w, y), pg_str, font=font_main, fill=0)
                y += self.toc_row_height
            pages.append(img)
        return pages

    def render_page(self, global_page_index):
        if not self.is_ready: return None

        # 1. GET SETTINGS
        threshold_val = self.layout_settings.get("text_threshold", 160)
        blur_val = self.layout_settings.get("text_blur", 0.7)
        mode = "Auto"  # HARDCODED

        num_toc = len(self.toc_pages_images)
        footer_padding = max(0, self.bottom_padding)
        header_padding = max(0, self.top_padding)
        content_height = self.screen_height - footer_padding - header_padding

        # --- STEP A: PREPARE CONTENT LAYER (The part we might blur) ---
        if global_page_index < num_toc:
            # TOC is treated as "Interface" - NO BLUR needed usually
            # But since it's an image, we convert it to L
            img_content = self.toc_pages_images[global_page_index].copy().convert("L")
            is_toc = True
            has_image_content = False
        else:
            is_toc = False
            doc_idx, page_idx = self.page_map[global_page_index - num_toc]
            doc, has_image_content = self.fitz_docs[doc_idx]
            page = doc[page_idx]

            # Render High Quality
            mat = fitz.Matrix(DEFAULT_RENDER_SCALE, DEFAULT_RENDER_SCALE)
            pix = page.get_pixmap(matrix=mat, alpha=False)

            # Create the body content image
            img_content_raw = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            img_content = img_content_raw.resize((self.screen_width, content_height), Image.Resampling.LANCZOS).convert(
                "L")

        # --- STEP B: APPLY FILTERS TO BODY CONTENT ONLY ---
        # We only blur/threshold the book text, not the headers/footers

        should_dither = False
        if mode == "Image (Dither)":
            should_dither = True
        elif mode == "Text Only (Sharp)":
            should_dither = False
        else:
            should_dither = has_image_content

        # Create a canvas for the full page
        full_page = Image.new("L", (self.screen_width, self.screen_height), 255)

        # Paste content into place
        paste_y = 0 if is_toc else header_padding
        full_page.paste(img_content, (0, paste_y))

        if not is_toc:
            # Only apply effects if it's NOT the table of contents
            if should_dither:
                # Dither logic (Images)
                full_page = ImageEnhance.Contrast(full_page).enhance(1.2)
                full_page = full_page.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")
            else:
                # Text Rounding Logic (Blur + Threshold)
                if blur_val > 0:
                    # BLUR THE CONTENT
                    full_page = full_page.filter(ImageFilter.GaussianBlur(radius=blur_val))

                # THRESHOLD THE CONTENT
                full_page = full_page.point(lambda p: 255 if p > threshold_val else 0, mode='L')

        # --- STEP C: DRAW HEADERS/FOOTERS ON TOP (SHARP) ---
        # We convert to RGB momentarily to allow drawing text
        img_final = full_page.convert("RGB")
        draw = ImageDraw.Draw(img_final)

        # Clear the header/footer zones to white (erasing any blurred bleed)
        if not is_toc:
            if header_padding > 0:
                draw.rectangle([0, 0, self.screen_width, header_padding], fill=(255, 255, 255))
            if footer_padding > 0:
                draw.rectangle([0, self.screen_height - footer_padding, self.screen_width, self.screen_height],
                               fill=(255, 255, 255))

            # Draw crisp UI text
            self._draw_header(draw, global_page_index)
            self._draw_footer(draw, global_page_index)

        return img_final

    def get_xtc_bytes(self):
        if not self.is_ready: return None
        blob, idx = bytearray(), bytearray()
        data_off = 56 + (16 * self.total_pages)
        prog_text = st.empty()
        for i in range(self.total_pages):
            if i % 10 == 0: prog_text.text(f"Exporting page {i + 1}/{self.total_pages}...")

            # Get the fully composed page (with blurred body and sharp header)
            img_rgb = self.render_page(i)

            # Convert to 1-bit. Since render_page already did the thresholding, this is lossless.
            img = img_rgb.convert("1")

            w, h = img.size
            xtg = struct.pack("<IHHBBIQ", 0x00475458, w, h, 0, 0, ((w + 7) // 8) * h, 0) + img.tobytes()
            idx.extend(struct.pack("<QIHH", data_off + len(blob), len(xtg), w, h))
            blob.extend(xtg)
        header = struct.pack("<IHHBBBBIQQQQQ", 0x00435458, 0x0100, self.total_pages, 0, 0, 0, 0, 0, 0, 56, data_off, 0,
                             0)
        prog_text.empty()
        return io.BytesIO(header + idx + blob)


# --- STREAMLIT APP ---

KEY_MAP = {
    "top_pad": "top_padding",
    "bot_pad": "bottom_padding",
    "align": "text_align",
    "use_toc": "generate_toc",
    "pos_perc": "pos_percent",
    "ord_title": "order_title",
    "ord_pagenum": "order_pagenum",
    "ord_chap": "order_chap_page",
    "ord_perc": "order_percent",
    "pos_chap": "pos_chap_page",
    "font_size": "font_size",
    "margin": "margin",
    "line_height": "line_height",
    "font_weight": "font_weight",
    "orientation": "orientation",
    "show_footnotes": "show_footnotes",
    "pos_title": "pos_title",
    "pos_pagenum": "pos_pagenum",
    "pos_progress": "pos_progress",
    "bar_height": "bar_height",
    "bar_tick_height": "bar_tick_height",
    "bar_marker_radius": "bar_marker_radius",
    "bar_marker_color": "bar_marker_color",
    "bar_show_ticks": "bar_show_ticks",
    "bar_show_marker": "bar_show_marker",
    "header_font_size": "header_font_size",
    "header_align": "header_align",
    "header_margin": "header_margin",
    "footer_font_size": "footer_font_size",
    "footer_align": "footer_align",
    "footer_margin": "footer_margin",
    "text_threshold": "text_threshold",
    "text_blur": "text_blur"
}


def get_current_settings_for_export():
    """Gathers settings from Session State and maps to CTK keys."""
    export_data = {}
    for st_key, ctk_key in KEY_MAP.items():
        if st_key in st.session_state:
            export_data[ctk_key] = st.session_state[st_key]
    if "font_name" not in export_data: export_data["font_name"] = "Default (System)"
    if "preview_zoom" not in export_data: export_data["preview_zoom"] = 300
    # Include render mode
    export_data["render_mode"] = "Auto"
    return json.dumps(export_data, indent=4)


def main():
    st.set_page_config(page_title="EPUB to XTC Live", layout="wide", initial_sidebar_state="expanded")

    st.markdown("""
    <style>
        section[data-testid="stSidebar"] { width: 450px !important; }
        .block-container { padding-top: 1rem; padding-bottom: 1rem; }
        header[data-testid="stHeader"] { background-color: rgba(0,0,0,0); }
        header[data-testid="stHeader"] > div:first-child { background: transparent; }
        div[data-testid="stExpander"] div[role="button"] p { font-size: 1rem; font-weight: 600; }
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
        with st.expander("Presets (Save/Load)", expanded=False):
            uploaded_preset = st.file_uploader("Load Preset (JSON)", type=["json"])
            if uploaded_preset:
                preset_id = f"{uploaded_preset.name}_{uploaded_preset.size}"
                if st.session_state.get('applied_preset_id') != preset_id:
                    try:
                        loaded_data = json.load(uploaded_preset)
                        REVERSE_MAP = {v: k for k, v in KEY_MAP.items()}
                        for k, v in loaded_data.items():
                            target_key = REVERSE_MAP.get(k, k)
                            st.session_state[target_key] = v
                        st.session_state['applied_preset_id'] = preset_id
                        st.success("Preset applied!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error loading preset: {e}")
            elif 'applied_preset_id' in st.session_state:
                del st.session_state['applied_preset_id']

            st.download_button(
                label="💾 Download Current Preset",
                data=get_current_settings_for_export(),
                file_name="epub_2_xtc_preset.json",
                mime="application/json",
                use_container_width=True
            )

        st.divider()

        current_config = {}
        if st.session_state.processor.is_ready:
            st.success("✅ Book Ready")
            col_dl, col_cov = st.columns(2)
            with col_dl:
                if st.button("Download XTC", type="primary", use_container_width=True):
                    with st.spinner("Generating..."):
                        xtc_data = st.session_state.processor.get_xtc_bytes()

                        # 1. Get original name, strip extension, add .xtc
                        original_name = st.session_state.file_key.rsplit('_', 1)[0]  # Removes the _size suffix we added
                        base_name = os.path.splitext(original_name)[0]
                        out_name = f"{base_name}.xtc"

                        # 2. Pass out_name to file_name
                        st.download_button("Save XTC", data=xtc_data, file_name=out_name,
                                           mime="application/octet-stream")
            with col_cov:
                with st.popover("Export Cover", use_container_width=True):
                    if st.session_state.processor.cover_image_obj:
                        st.write("Cover Settings")
                        cv_w = st.number_input("Width", value=480)
                        cv_h = st.number_input("Height", value=800)
                        cv_mode = st.selectbox("Mode", ["Crop to Fill", "Fit", "Stretch"])
                        if st.button("Generate BMP"):
                            img = st.session_state.processor.cover_image_obj.convert("RGB")
                            if cv_mode == "Stretch":
                                img = img.resize((cv_w, cv_h), Image.Resampling.LANCZOS)
                            elif cv_mode == "Fit":
                                img = ImageOps.pad(img, (cv_w, cv_h), color="white", centering=(0.5, 0.5))
                            else:
                                img = ImageOps.fit(img, (cv_w, cv_h), centering=(0.5, 0.5))
                            img = img.convert("L")
                            img = ImageEnhance.Contrast(img).enhance(1.3)
                            img = ImageEnhance.Brightness(img).enhance(1.05)
                            img = img.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
                            buf = io.BytesIO()
                            img.save(buf, format="BMP")
                            st.download_button("Download BMP", data=buf.getvalue(), file_name="cover.bmp")
                    else:
                        st.warning("No cover found.")
            st.divider()

        st.header("1. Input")
        uploaded_file = st.file_uploader("Upload EPUB", type=["epub"])

        # --- NEW FONT SELECTION UI ---
        font_mode = st.radio("Font Source", ["System (Built-in)", "Custom (Upload)"], horizontal=True)

        final_font_data = None
        uploaded_font_zip = None

        if font_mode == "Custom (Upload)":
            uploaded_font_zip = st.file_uploader("Custom Font Family (ZIP)", type=["zip"],
                                                 help="Upload a ZIP file containing TTF or OTF files.")
        else:
            # System font selection
            # Sort keys so headers appear first, but separators are handled
            display_keys = list(FITZ_FONTS.keys())
            selected_sys_font = st.selectbox("Select System Font", display_keys, index=1)  # Default to Times Roman

            # Handle headers/separators selection gracefully
            if "---" in selected_sys_font:
                st.warning("Please select a valid font, not a category header.")
                final_font_data = "Times-Roman"
            else:
                final_font_data = FITZ_FONTS[selected_sys_font]

            current_config['font_source'] = "system"
            current_config['system_font_name'] = final_font_data

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

        st.divider()
        st.header("2. Settings")

        if st.session_state.processor.is_parsed:
            with st.expander("Chapter Visibility (TOC)", expanded=False):
                st.info("Unchecked chapters are hidden from navigation but remain in book.")

                selected_indices = []
                for idx, chapter in enumerate(st.session_state.processor.raw_chapters):
                    title = chapter['title']

                    # Default Rule: If title starts with "Section ", default to False (Unchecked)
                    # We use the file_key in the widget key to ensure it resets when you upload a new book
                    is_auto_section = title.strip().startswith("Section ")
                    default_val = not is_auto_section
                    widget_key = f"toc_cb_{st.session_state.file_key}_{idx}"

                    # Render Checkbox
                    if st.checkbox(f"{idx + 1}. {title}", value=default_val, key=widget_key):
                        selected_indices.append(idx)

                st.session_state.selected_chapter_indices = selected_indices

        def get_state(key, default):
            if key not in st.session_state:
                st.session_state[key] = default
            return st.session_state[key]

        with st.expander("Page Body Layout", expanded=True):
            c1, c2 = st.columns(2)
            current_config['orientation'] = c1.selectbox("Orientation", ["Portrait", "Landscape"], key="orientation",
                                                         index=0 if get_state("orientation",
                                                                              "Portrait") == "Portrait" else 1)

            align_opts = ["justify", "left"]
            align_idx = 0 if get_state("align", "justify") == "justify" else 1
            current_config['align'] = c2.selectbox("Alignment", align_opts, key="align", index=align_idx)

            current_config['use_toc'] = st.checkbox("Generate TOC", value=get_state("use_toc", True), key="use_toc")
            current_config['show_footnotes'] = st.checkbox("Inline Footnotes", value=get_state("show_footnotes", False),
                                                           key="show_footnotes")
            st.subheader("Typography")
            t1, t2 = st.columns(2)
            current_config['font_size'] = t1.number_input("Size", 10, 50, get_state("font_size", DEFAULT_FONT_SIZE),
                                                          key="font_size")
            current_config['font_weight'] = t2.number_input("Weight", 100, 900,
                                                            get_state("font_weight", DEFAULT_FONT_WEIGHT), step=100,
                                                            key="font_weight")
            current_config['line_height'] = st.number_input("Line Height", 1.0, 3.0,
                                                            get_state("line_height", DEFAULT_LINE_HEIGHT), step=0.1,
                                                            key="line_height")

            # --- NEW SLIDERS HERE ---
            st.markdown("##### Rendering Adjustments")
            r1, r2 = st.columns(2)
            current_config['text_threshold'] = r1.slider("Text Sharpness", 100, 200,
                                                         get_state("text_threshold", DEFAULT_TEXT_THRESHOLD),
                                                         key="text_threshold")
            current_config['text_blur'] = r2.slider("Corner Softness", 0.0, 3.0,
                                                    get_state("text_blur", DEFAULT_TEXT_BLUR), step=0.1,
                                                    key="text_blur")

            st.subheader("Margins & Padding")

            pad_col1, pad_col2 = st.columns(2)
            with pad_col1:
                current_config['top_pad'] = st.number_input("Top Padding", 0, 150,
                                                            get_state("top_pad", DEFAULT_TOP_PADDING), key="top_pad")

            with pad_col2:
                current_config['bot_pad'] = st.number_input("Bottom Padding", 0, 150,
                                                            get_state("bot_pad", DEFAULT_BOTTOM_PADDING),
                                                            key="bot_pad")

            current_config['margin'] = st.number_input("Side Margin", 0, 100, get_state("margin", DEFAULT_MARGIN),
                                                       key="margin")

        with st.expander("Header & Footer Content", expanded=False):
            st.caption("Decide where each element appears.")

            def elem_row(label, key_pos, key_ord, def_pos, def_ord):
                c1, c2 = st.columns([2, 1])
                opts = ["Header", "Footer", "Hidden"]
                curr_pos = get_state(key_pos, def_pos)
                try:
                    def_idx = opts.index(curr_pos)
                except:
                    def_idx = 2
                pos = c1.selectbox(label, opts, index=def_idx, key=key_pos)
                ord_val = c2.number_input("Order", value=get_state(key_ord, def_ord), key=key_ord)
                return pos, ord_val

            current_config['pos_title'], current_config['order_title'] = elem_row("Chapter Title", "pos_title",
                                                                                  "ord_title", "Footer", 2)
            current_config['pos_pagenum'], current_config['order_pagenum'] = elem_row("Page Number (X/Y)",
                                                                                      "pos_pagenum", "ord_pagenum",
                                                                                      "Footer", 1)
            current_config['pos_chap_page'], current_config['order_chap_page'] = elem_row("Chapter Page (i/n)",
                                                                                          "pos_chap", "ord_chap",
                                                                                          "Hidden", 3)
            current_config['pos_percent'], current_config['order_percent'] = elem_row("Reading %", "pos_perc",
                                                                                      "ord_perc", "Hidden", 4)
            st.divider()
            st.markdown("#### Progress Bar Configuration")

            prog_opts = ["Footer (Below Text)", "Footer (Above Text)", "Header (Below Text)", "Header (Above Text)",
                         "Hidden"]
            prog_curr = get_state("pos_progress", "Footer (Below Text)")
            try:
                prog_idx = prog_opts.index(prog_curr)
            except:
                prog_idx = 0

            current_config['pos_progress'] = st.selectbox("Position", prog_opts, index=prog_idx, key="pos_progress")

            st.caption("Dimensions")
            p1, p2 = st.columns(2)
            current_config['bar_height'] = p1.number_input("Bar Thickness", 1, 10, get_state("bar_height", 4),
                                                           key="bar_height")
            current_config['bar_tick_height'] = p2.number_input("Tick Height", 2, 20, get_state("bar_tick_height", 6),
                                                                key="bar_tick_height")
            st.caption("Marker")
            p3, p4 = st.columns(2)
            current_config['bar_marker_radius'] = p3.number_input("Marker Radius", 2, 10,
                                                                  get_state("bar_marker_radius", 5),
                                                                  key="bar_marker_radius")

            mark_col_opts = ["Black", "White"]
            mark_col_curr = get_state("bar_marker_color", "Black")
            mark_col_idx = 0 if mark_col_curr == "Black" else 1
            current_config['bar_marker_color'] = p4.selectbox("Marker Color", mark_col_opts, index=mark_col_idx,
                                                              key="bar_marker_color")

            st.caption("Visibility")
            c_tick, c_mark = st.columns(2)
            current_config['bar_show_ticks'] = c_tick.checkbox("Show Chapter Ticks",
                                                               value=get_state("bar_show_ticks", True),
                                                               key="bar_show_ticks")
            current_config['bar_show_marker'] = c_mark.checkbox("Show Current Marker",
                                                                value=get_state("bar_show_marker", True),
                                                                key="bar_show_marker")

        with st.expander("Header & Footer Styling", expanded=False):
            st.subheader("Header Styling")
            h1, h2 = st.columns(2)
            current_config['header_font_size'] = h1.number_input("Font Size", 8, 30, get_state("header_font_size", 16),
                                                                 key="header_font_size")

            align_opts = ["Center", "Left", "Right", "Justify"]
            h_align_curr = get_state("header_align", "Center")
            h_idx = align_opts.index(h_align_curr) if h_align_curr in align_opts else 0

            current_config['header_align'] = h2.selectbox("Alignment", align_opts, index=h_idx, key="header_align")
            current_config['header_margin'] = st.number_input("Header Y-Offset", 0, 100, get_state("header_margin", 10),
                                                              key="header_margin")
            st.divider()
            st.subheader("Footer Styling")
            f1, f2 = st.columns(2)
            current_config['footer_font_size'] = f1.number_input("Font Size ", 8, 30, get_state("footer_font_size", 16),
                                                                 key="footer_font_size")

            f_align_curr = get_state("footer_align", "Center")
            f_idx = align_opts.index(f_align_curr) if f_align_curr in align_opts else 0

            current_config['footer_align'] = f2.selectbox("Alignment ", align_opts, index=f_idx, key="footer_align")
            current_config['footer_margin'] = st.number_input("Footer Y-Offset", 0, 100, get_state("footer_margin", 10),
                                                              key="footer_margin")

        st.divider()
        if st.session_state.processor.is_parsed:
            if st.button("Apply Changes / Render", type="primary", use_container_width=True):
                st.session_state.force_render = True

    # --- MAIN RENDER LOGIC ---

    # Process Font Upload if in Custom Mode
    if font_mode == "Custom (Upload)" and uploaded_font_zip:
        font_temp_dir = os.path.join(tempfile.gettempdir(), "epub_xtc_fonts")
        if os.path.exists(font_temp_dir):
            try:
                shutil.rmtree(font_temp_dir)
            except:
                pass
        os.makedirs(font_temp_dir, exist_ok=True)

        try:
            with zipfile.ZipFile(uploaded_font_zip) as z:
                z.extractall(font_temp_dir)

            # Use helper to scan directory
            scanned_fonts = get_font_variants(font_temp_dir)

            if scanned_fonts.get("regular"):
                final_font_data = scanned_fonts  # Assign DICT to variable
                current_config['font_source'] = "custom"
                current_config['font_sig'] = uploaded_font_zip.name
                st.success(f"Font loaded! Variants found: {[k for k, v in scanned_fonts.items() if v]}")
            else:
                st.warning("No font files found in ZIP.")
        except Exception as e:
            st.error(f"Failed to process font ZIP: {e}")

    # Fallback to default system font if nothing selected/uploaded
    if final_font_data is None:
        final_font_data = "Times-Roman"  # Default Fallback

    current_config['selected_indices_tuple'] = tuple(sorted(st.session_state.selected_chapter_indices))
    should_render = (st.session_state.processor.is_parsed and (
            current_config != st.session_state.last_config or not st.session_state.processor.is_ready or st.session_state.get(
        'force_render', False)))

    if should_render:
        st.session_state.force_render = False
        relative_pos = 0.0
        if st.session_state.processor.is_ready and st.session_state.processor.total_pages > 0:
            relative_pos = st.session_state.current_page / st.session_state.processor.total_pages

        with st.spinner("Rendering layout... (Step 2/2)"):
            success = st.session_state.processor.render_chapters(
                set(st.session_state.selected_chapter_indices),
                final_font_data,  # Can be Dict or String now
                current_config['font_size'],
                current_config['margin'],
                current_config['line_height'],
                current_config['font_weight'],
                current_config['bot_pad'],
                current_config['top_pad'],
                current_config['align'],
                current_config['orientation'],
                current_config['use_toc'],
                layout_settings=current_config,
                show_footnotes=current_config['show_footnotes']
            )
            if success:
                st.session_state.last_config = current_config
                new_total = st.session_state.processor.total_pages
                st.session_state.current_page = int(relative_pos * new_total)
                st.session_state.current_page = min(max(0, st.session_state.current_page), new_total - 1)
                st.rerun()

    # --- DISPLAY AREA ---
    if st.session_state.processor.is_ready:
        c1, c2, c3 = st.columns([1, 2, 1])
        with c1:
            if st.button("⬅ Previous", use_container_width=True):
                st.session_state.current_page = max(0, st.session_state.current_page - 1)
        with c2:
            st.markdown(f"""<div style="text-align:center; padding-top: 5px; font-size:1.1rem; color: #444;">
                    Page <b>{st.session_state.current_page + 1}</b> / {st.session_state.processor.total_pages}
                </div>""", unsafe_allow_html=True)
        with c3:
            if st.button("Next ➡", use_container_width=True):
                st.session_state.current_page = min(st.session_state.processor.total_pages - 1,
                                                    st.session_state.current_page + 1)

        # Main Preview Image
        img = st.session_state.processor.render_page(st.session_state.current_page)

        # Pull zoom value from session state slider (below) or default
        preview_width_val = st.session_state.get("preview_zoom_slider", 350)

        base_size = int(preview_width_val)
        if img.width > img.height:
            target_h = base_size
            target_w = int(target_h * (img.width / img.height))
        else:
            target_w = base_size
            target_h = int(target_w * (img.height / img.width))

        preview_img = img.copy().resize((target_w, target_h), Image.Resampling.LANCZOS)
        with io.BytesIO() as buffer:
            preview_img.save(buffer, format="PNG")
            img_b64 = base64.b64encode(buffer.getvalue()).decode()

        st.markdown(f"""<div style="display: flex; justify-content: center; margin-top: 15px;">
                <img src="data:image/png;base64,{img_b64}" width="{target_w}" style="max-width: 100%; box-shadow: 0px 4px 15px rgba(0,0,0,0.15);">
            </div>""", unsafe_allow_html=True)

        st.columns([1, 2, 1])[1].slider("Preview Zoom", 200, 800, 350, key="preview_zoom_slider")

        b1, b2, b3 = st.columns([5, 2, 5])
        with b2:
            def update_page():
                val = st.session_state.goto_input
                if 0 < val <= st.session_state.processor.total_pages:
                    st.session_state.current_page = val - 1

            st.number_input("Jump to page:", min_value=1, max_value=st.session_state.processor.total_pages,
                            value=st.session_state.current_page + 1, key="goto_input", on_change=update_page)
    else:
        st.info("👈 Please upload an EPUB file in the sidebar to begin.")


if __name__ == "__main__":
    main()
