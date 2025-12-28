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
import concurrent.futures

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

# --- RENDER DEFAULTS ---
DEFAULT_RENDER_MODE = "Threshold"
DEFAULT_TEXT_THRESHOLD = 130
DEFAULT_TEXT_BLUR = 1.0
DEFAULT_WHITE_CLIP = 220
DEFAULT_CONTRAST = 1.2

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
    all_files_paths = []
    for root, dirs, files in os.walk(directory):
        for f in files:
            if f.lower().endswith((".ttf", ".otf")):
                all_files_paths.append(os.path.join(root, f).replace("\\", "/"))

    if not all_files_paths:
        return {}

    candidates = {"regular": [], "italic": [], "bold": [], "bold_italic": []}

    for full_path in all_files_paths:
        name_lower = os.path.basename(full_path).lower()
        has_bold = any(x in name_lower for x in ["bold", "bd", "-b", "_b"])
        has_italic = any(x in name_lower for x in ["italic", "oblique", "obl", "-i", "_i"])

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

    if not results["regular"] and all_files_paths:
        results["regular"] = all_files_paths[0]

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

    def process_toc_item(item):
        if isinstance(item, tuple):
            if len(item) > 1 and isinstance(item[1], list):
                for sub in item[1]: process_toc_item(sub)
        elif isinstance(item, epub.Link):
            add_entry(item.href, item.title)

    for item in book.toc:
        process_toc_item(item)

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
        self.raw_chapters = []
        self.book_css = ""
        self.book_images = {}
        self.book_lang = 'en'
        self.is_parsed = False
        self.cover_image_obj = None
        self.global_id_map = {}
        self.fitz_docs = []
        self.toc_data_final = []
        self.toc_pages_images = []
        self.page_map = []
        self.total_pages = 0
        self.toc_items_per_page = 18
        self.is_ready = False
        self.temp_dir = tempfile.TemporaryDirectory()
        self.layout_settings = {}
        self.font_data = {}
        self.ui_font_ref = None

    def _smart_extract_content(self, elem):
        if elem.name == 'a':
            parent = elem.parent
            if parent and parent.name not in ['body', 'html', 'section']: return parent
            return elem
        if elem.name in ['aside', 'li', 'dd', 'div']: return elem
        text = elem.get_text(strip=True)
        if len(text) > 1: return elem
        parent = elem.parent
        if parent:
            if parent.name in ['body', 'html', 'section']: return elem
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
                    if not final_html and original_raw_html: final_html = original_raw_html
                    if final_html: id_map[f"{filename}#{elem['id']}"] = final_html
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
                   ['footnote', 'endnote', 'reflist', 'bibliography']): continue
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
                        if k.endswith(suffix): content = v; break
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
        try:
            cover_data = book.get_metadata('OPF', 'cover')
            if cover_data:
                cover_id = cover_data[0][1]
                item = book.get_item_with_id(cover_id)
                if item: return Image.open(io.BytesIO(item.get_content()))
        except:
            pass
        for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            if 'cover' in item.get_name().lower(): return Image.open(io.BytesIO(item.get_content()))
        for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            return Image.open(io.BytesIO(item.get_content()))
        return None

    def _split_html_by_toc(self, soup, toc_entries):
        chunks = []
        if len(toc_entries) == 1 and not toc_entries[0][0]:
            return [{'title': toc_entries[0][1], 'soup': soup}]
        split_points = []
        for anchor, title in toc_entries:
            target = None
            if anchor: target = soup.find(id=anchor)
            if target or not anchor: split_points.append({'node': target, 'title': title})
        if not split_points: return [{'title': toc_entries[0][1], 'soup': soup}]
        current_idx = 0
        current_soup = BeautifulSoup("<body></body>", 'html.parser')
        body_children = list(soup.body.children) if soup.body else []
        for child in body_children:
            if isinstance(child, NavigableString) and not child.strip():
                if current_soup.body: current_soup.body.append(child.extract() if hasattr(child, 'extract') else child)
                continue
            if current_idx + 1 < len(split_points):
                next_node = split_points[current_idx + 1]['node']
                is_nested_target = False
                if hasattr(child, 'find_all'):
                    if next_node in child.find_all(): is_nested_target = True
                if next_node and (child == next_node or is_nested_target):
                    chunks.append({'title': split_points[current_idx]['title'], 'soup': current_soup})
                    current_idx += 1
                    current_soup = BeautifulSoup("<body></body>", 'html.parser')
            if current_soup.body: current_soup.body.append(child)
        chunks.append({'title': split_points[current_idx]['title'], 'soup': current_soup})
        return chunks

    def parse_structure(self, epub_bytes):
        self.raw_chapters = []
        self.cover_image_obj = None
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
        toc_mapping = get_official_toc_mapping(book)
        items = [book.get_item_with_id(i[0]) for i in book.spine if
                 isinstance(book.get_item_with_id(i[0]), epub.EpubHtml)]
        for item in items:
            item_filename = os.path.basename(item.get_name())
            raw_html = item.get_content().decode('utf-8', errors='replace')
            soup = BeautifulSoup(raw_html, 'html.parser')
            has_image = bool(soup.find('img'))
            toc_entries = toc_mapping.get(item_filename)
            if toc_entries and len(toc_entries) > 1:
                split_chapters = self._split_html_by_toc(soup, toc_entries)
                for chunk in split_chapters:
                    self.raw_chapters.append(
                        {'title': chunk['title'], 'soup': chunk['soup'], 'has_image': bool(chunk['soup'].find('img')),
                         'filename': item_filename})
            else:
                chapter_title = toc_entries[0][1] if toc_entries else None
                if not chapter_title:
                    for tag in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
                        header = soup.find(tag)
                        if header:
                            t = header.get_text().strip()
                            if t and len(t) < 150: chapter_title = t; break
                    if not chapter_title: chapter_title = f"Section {len(self.raw_chapters) + 1}"
                self.raw_chapters.append(
                    {'title': chapter_title, 'soup': soup, 'has_image': has_image, 'filename': item_filename})
        self.is_parsed = True
        return True, "Success"

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
            draw.ellipse([cx - marker_r, cy - marker_r, cx + marker_r, cy + marker_r], fill=marker_fill,
                         outline=(0, 0, 0))

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
                if page_num_disp >= start_pg: current_title = title; break
            pm_idx = global_page_index - num_toc
            if 0 <= pm_idx < len(self.page_map):
                doc_idx, page_idx = self.page_map[pm_idx]
                doc_ref = self.fitz_docs[doc_idx][0]
                chap_total = len(doc_ref)
                chap_page_disp = f"{page_idx + 1}/{chap_total}"
            else:
                chap_page_disp = "1/1"
        return {'pagenum': f"{page_num_disp}/{self.total_pages}", 'title': current_title, 'chap_page': chap_page_disp,
                'percent': f"{percent}%"}

    def _draw_text_line(self, draw, y, font, elements_list, align):
        if not elements_list: return
        margin_x = 20
        canvas_width = self.screen_width - (margin_x * 2)
        separator = "  |  "
        title_item = None
        fixed_items = []
        for key, txt in elements_list:
            if key == 'title':
                title_item = txt
            else:
                fixed_items.append(txt)
        fixed_text_w = sum(font.getlength(txt) for txt in fixed_items)
        sep_w = font.getlength(separator)
        total_seps_w = sep_w * (len(elements_list) - 1) if len(elements_list) > 1 else 0
        available_for_title = canvas_width - fixed_text_w - total_seps_w
        display_title = title_item if title_item else ""
        if title_item:
            if font.getlength(title_item) > available_for_title:
                t = title_item
                while len(t) > 0 and font.getlength(t + "...") > available_for_title: t = t[:-1]
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
        font_size, margin, align = s.get("header_font_size", 16), s.get("header_margin", 10), s.get("header_align",
                                                                                                    "Center")
        bar_h, pos_prog = s.get("bar_height", 4), s.get("pos_progress", "Footer (Below Text)")
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
        font_size, margin, align = s.get("footer_font_size", 16), s.get("footer_margin", 10), s.get("footer_align",
                                                                                                    "Center")
        bar_h, pos_prog = s.get("bar_height", 4), s.get("pos_progress", "Footer (Below Text)")
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
                if content: active.append((order, key, content))
        active.sort(key=lambda x: x[0])
        return [(x[1], x[2]) for x in active]

    def render_chapters(self, selected_indices_set, font_data_input, font_size, margin, line_height, font_weight,
                        bottom_padding, top_padding, text_align, orientation, add_toc, layout_settings=None,
                        show_footnotes=True):
        is_custom_font = isinstance(font_data_input, dict)
        if is_custom_font:
            self.font_data = font_data_input
            self.ui_font_ref = font_data_input.get("regular")
        else:
            self.font_data = font_data_input
            self.ui_font_ref = font_data_input
        self.font_size, self.margin, self.line_height = int(font_size), margin, line_height
        self.font_weight, self.bottom_padding, self.top_padding = font_weight, bottom_padding, top_padding
        self.text_align = text_align
        self.layout_settings = layout_settings if layout_settings else {}
        if orientation == "Landscape":
            self.screen_width, self.screen_height = DEFAULT_SCREEN_HEIGHT, DEFAULT_SCREEN_WIDTH
        else:
            self.screen_width, self.screen_height = DEFAULT_SCREEN_WIDTH, DEFAULT_SCREEN_HEIGHT
        for doc, _ in self.fitz_docs: doc.close()
        self.fitz_docs, self.page_map = [], []
        font_rules = []
        font_family_val = "serif"
        if is_custom_font:
            def add_font_rule(path, weight="normal", style="normal"):
                if path and os.path.exists(path):
                    css_path = path.replace("\\", "/")
                    return f'@font-face {{ font-family: "CustomFont"; src: url("{css_path}"); font-weight: {weight}; font-style: {style}; }}'
                return ""

            if self.font_data.get("regular"): font_rules.append(
                add_font_rule(self.font_data["regular"], "normal", "normal"))
            if self.font_data.get("bold"): font_rules.append(add_font_rule(self.font_data["bold"], "bold", "normal"))
            if self.font_data.get("italic"): font_rules.append(
                add_font_rule(self.font_data["italic"], "normal", "italic"))
            if self.font_data.get("bold_italic"): font_rules.append(
                add_font_rule(self.font_data["bold_italic"], "bold", "italic"))
            font_family_val = '"CustomFont"'
        else:
            if self.font_data in ["serif", "sans-serif", "monospace", "cursive", "fantasy"]:
                font_family_val = self.font_data
            else:
                font_family_val = f'"{self.font_data}"'
        font_face_block = "\n".join(font_rules)
        patched_css = fix_css_font_paths(self.book_css, font_family_val)
        custom_css = f"""<style>
            {font_face_block}
            @page {{ size: {self.screen_width}pt {self.screen_height}pt; margin: 0; }}
            body, p, div, span, li, blockquote, dd, dt {{ font-family: {font_family_val} !important; font-size: {self.font_size}pt !important; font-weight: {self.font_weight} !important; line-height: {self.line_height} !important; text-align: {self.text_align} !important; color: black !important; overflow-wrap: break-word; }}
            body {{ margin: 0 !important; padding: {self.margin}px !important; background-color: white !important; width: 100% !important; height: 100% !important; }}
            img {{ max-width: 95% !important; height: auto !important; display: block; margin: 20px auto !important; }}
            h1, h2, h3 {{ text-align: center !important; margin-top: 1em; font-weight: {min(900, self.font_weight + 200)} !important; }}
            .fn-marker {{ font-weight: bold; font-size: 0.7em !important; vertical-align: super; color: solid black !important; }}
            .inline-footnote-box {{ display: block; margin: 15px 0px; padding: 0px 15px; border-left: 4px solid solid black; font-size: {int(self.font_size * 0.85)}pt !important; line-height: {self.line_height} !important; }}
            .inline-footnote-box p {{ margin: 0 !important; padding: 0 !important; font-size: inherit !important; display: inline; }}
        </style>"""
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
        status_text.empty();
        progress_bar.empty();
        self.is_ready = True
        return True

    def _get_ui_font(self, size):
        return get_pil_font(self.ui_font_ref, int(size))

    def _render_toc_pages(self, toc_entries):
        pages = []
        main_size, header_size = self.font_size, int(self.font_size * 1.2)
        font_main, font_header = self._get_ui_font(main_size), self._get_ui_font(header_size)
        left_margin, right_margin, column_gap, limit = 40, 40, 20, self.toc_items_per_page
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
                    while font_main.getlength(display_title + "...") > max_title_w and len(
                        display_title) > 0: display_title = display_title[:-1]
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

        # --- 1. GET RENDER SETTINGS ---
        mode = self.layout_settings.get("render_mode", DEFAULT_RENDER_MODE)
        threshold_val = self.layout_settings.get("text_threshold", DEFAULT_TEXT_THRESHOLD)
        sharpness_val = self.layout_settings.get("text_blur", DEFAULT_TEXT_BLUR)
        white_clip = self.layout_settings.get("white_clip", DEFAULT_WHITE_CLIP)
        contrast = self.layout_settings.get("contrast", DEFAULT_CONTRAST)

        num_toc = len(self.toc_pages_images)
        footer_padding = max(0, self.bottom_padding)
        header_padding = max(0, self.top_padding)
        content_height = self.screen_height - footer_padding - header_padding
        if content_height < 1: content_height = 1

        # --- 2. PREPARE CONTENT LAYER ---
        has_image_content = False

        if global_page_index < num_toc:
            # Table of Contents
            img_content = self.toc_pages_images[global_page_index].copy().convert("L")
            is_toc = True
        else:
            is_toc = False
            doc_idx, page_idx = self.page_map[global_page_index - num_toc]
            doc, has_image_content = self.fitz_docs[doc_idx]
            page = doc[page_idx]

            # --- OPTIMIZATION: Direct Rendering ---
            src_w = page.rect.width
            src_h = page.rect.height

            # Calculate exact scale to fill width/height
            sx = self.screen_width / src_w
            sy = content_height / src_h

            mat = fitz.Matrix(sx, sy)
            pix = page.get_pixmap(matrix=mat, alpha=False)

            # Fast creation
            img_content = Image.frombytes("RGB", [pix.width, pix.height], pix.samples).convert("L")

        # --- 3. APPLY FILTERS ---
        full_page = Image.new("L", (self.screen_width, self.screen_height), 255)
        paste_y = 0 if is_toc else header_padding

        # Center horizontally if there's a slight pixel mismatch
        paste_x = (self.screen_width - img_content.width) // 2
        full_page.paste(img_content, (paste_x, paste_y))

        if not is_toc:
            use_dither = False
            if mode == "Dither":
                use_dither = True
            elif mode == "Threshold":
                use_dither = has_image_content

            if use_dither:
                if contrast != 1.0:
                    full_page = ImageEnhance.Contrast(full_page).enhance(contrast)
                if white_clip < 255:
                    full_page = full_page.point(lambda p: 255 if p > white_clip else p)
                full_page = full_page.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")
            else:
                if sharpness_val > 0:
                    enhancer = ImageEnhance.Sharpness(full_page)
                    full_page = enhancer.enhance(1.0 + (sharpness_val * 0.5))
                full_page = full_page.point(lambda p: 255 if p > threshold_val else 0).convert("L")

        # --- 4. OVERLAYS ---
        img_final = full_page.convert("RGB")
        draw = ImageDraw.Draw(img_final)

        if not is_toc:
            # Mask out header/footer areas
            if header_padding > 0:
                draw.rectangle([0, 0, self.screen_width, header_padding], fill=(255, 255, 255))
            if footer_padding > 0:
                draw.rectangle([0, self.screen_height - footer_padding, self.screen_width, self.screen_height],
                               fill=(255, 255, 255))

            self._draw_header(draw, global_page_index)
            self._draw_footer(draw, global_page_index)

        return img_final

    def get_xtc_bytes(self):
        if not self.is_ready: return None

        # We accumulate results in a list first to ensure order is maintained
        blob_parts = [None] * self.total_pages
        idx_parts = [None] * self.total_pages

        data_off_start = 56 + (16 * self.total_pages)
        current_data_offset = data_off_start

        prog_text = st.empty()
        prog_bar = st.progress(0)

        # Worker function for parallel execution
        def process_single_page(i):
            img_rgb = self.render_page(i)
            img = img_rgb.convert("1")
            w, h = img.size
            img_bytes = img.tobytes()
            # XTG Header
            xtg = struct.pack("<IHHBBIQ", 0x00475458, w, h, 0, 0, ((w + 7) // 8) * h, 0) + img_bytes
            return i, xtg, w, h

        # Parallel Execution
        # Use simple os.cpu_count() for max workers
        with concurrent.futures.ThreadPoolExecutor() as executor:
            # Submit all tasks
            futures = [executor.submit(process_single_page, i) for i in range(self.total_pages)]

            count = 0
            for future in concurrent.futures.as_completed(futures):
                count += 1
                # Update UI periodically (every 5% or so to reduce overhead)
                if count % max(1, self.total_pages // 20) == 0:
                    prog_text.text(f"Exporting page {count}/{self.total_pages}...")
                    prog_bar.progress(count / self.total_pages)

                # Retrieve result
                i, xtg_blob, w, h = future.result()
                blob_parts[i] = xtg_blob

                # We can't generate the Index here yet because the offset depends on previous pages.
                # We store the size/dims and calculate offsets sequentially after the loop.
                idx_parts[i] = (len(xtg_blob), w, h)

        # Sequential Offset Calculation
        final_idx = bytearray()
        final_blob = bytearray()

        for i in range(self.total_pages):
            size, w, h = idx_parts[i]
            chunk = blob_parts[i]

            # Add Index Entry
            final_idx.extend(struct.pack("<QIHH", current_data_offset, size, w, h))

            # Add Blob
            final_blob.extend(chunk)

            current_data_offset += size

        # Final Header
        header = struct.pack("<IHHBBBBIQQQQQ",
                             0x00435458, 0x0100, self.total_pages,
                             0, 0, 0, 0, 0, 0,
                             56, data_off_start,
                             0, 0)

        prog_text.empty()
        prog_bar.empty()

        return io.BytesIO(header + final_idx + final_blob)


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
    "render_mode": "render_mode",
    "text_threshold": "text_threshold",
    "text_blur": "text_blur",
    "white_clip": "white_clip",
    "contrast": "contrast"
}


def get_current_settings_for_export():
    """Gathers settings from Session State and maps to CTK keys."""
    export_data = {}
    for st_key, ctk_key in KEY_MAP.items():
        if st_key in st.session_state:
            export_data[ctk_key] = st.session_state[st_key]
    if "font_name" not in export_data: export_data["font_name"] = "Default (System)"
    if "preview_zoom" not in export_data: export_data["preview_zoom"] = 300
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
                        original_name = st.session_state.file_key.rsplit('_', 1)[0]
                        base_name = os.path.splitext(original_name)[0]
                        out_name = f"{base_name}.xtc"
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

        font_mode = st.radio("Font Source", ["System (Built-in)", "Custom (Upload)"], horizontal=True)
        final_font_data = None
        uploaded_font_zip = None

        if font_mode == "Custom (Upload)":
            uploaded_font_zip = st.file_uploader("Custom Font Family (ZIP)", type=["zip"],
                                                 help="Upload a ZIP file containing TTF or OTF files.")
        else:
            display_keys = list(FITZ_FONTS.keys())
            selected_sys_font = st.selectbox("Select System Font", display_keys, index=1)
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

        def get_state(key, default):
            if key not in st.session_state: st.session_state[key] = default
            return st.session_state[key]

        if st.session_state.processor.is_parsed:
            with st.expander("Chapter Visibility (TOC)", expanded=False):
                st.info("Unchecked chapters are hidden from navigation but remain in book.")
                selected_indices = []
                for idx, chapter in enumerate(st.session_state.processor.raw_chapters):
                    title = chapter['title']
                    is_auto_section = title.strip().startswith("Section ")
                    default_val = not is_auto_section
                    widget_key = f"toc_cb_{st.session_state.file_key}_{idx}"
                    if st.checkbox(f"{idx + 1}. {title}", value=default_val, key=widget_key): selected_indices.append(
                        idx)
                st.session_state.selected_chapter_indices = selected_indices

        with st.expander("3. Render Options", expanded=True):
            render_mode = st.radio("Pipeline Mode", ["Threshold", "Dither"],
                                   horizontal=True, key="render_mode",
                                   help="Threshold: Crisper text, dithered images.\nDither: Everything is dithered (newspaper style).")
            current_config["render_mode"] = render_mode



            if render_mode == "Threshold":
                st.caption("Settings for Text Only (Images are auto-dithered)")
                r1, r2 = st.columns(2)
                current_config['text_threshold'] = r1.slider("Text Weight (Threshold)", 50, 200,
                                                             get_state("text_threshold", DEFAULT_TEXT_THRESHOLD),
                                                             key="text_threshold")
                current_config['text_blur'] = r2.slider("Definition (Sharpness)", 0.0, 3.0,
                                                        get_state("text_blur", DEFAULT_TEXT_BLUR), step=0.1,
                                                        key="text_blur")
                # Default dither settings for images in Smart Mode
                current_config['white_clip'] = DEFAULT_WHITE_CLIP
                current_config['contrast'] = DEFAULT_CONTRAST
            else:
                st.caption("Settings for Full Page Dithering")
                d1, d2 = st.columns(2)
                current_config['contrast'] = d1.slider("Contrast Boost", 0.5, 2.0,
                                                       get_state("contrast", DEFAULT_CONTRAST), step=0.1,
                                                       key="contrast")
                current_config['white_clip'] = d2.slider("White Clipping", 150, 255,
                                                         get_state("white_clip", DEFAULT_WHITE_CLIP), key="white_clip")
                # Default threshold settings if ignored
                current_config['text_threshold'] = DEFAULT_TEXT_THRESHOLD
                current_config['text_blur'] = DEFAULT_TEXT_BLUR

        with st.expander("Page Body Layout", expanded=False):
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
            st.subheader("Margins & Padding")
            pad_col1, pad_col2 = st.columns(2)
            with pad_col1: current_config['top_pad'] = st.number_input("Top Padding", 0, 150,
                                                                       get_state("top_pad", DEFAULT_TOP_PADDING),
                                                                       key="top_pad")
            with pad_col2: current_config['bot_pad'] = st.number_input("Bottom Padding", 0, 150,
                                                                       get_state("bot_pad", DEFAULT_BOTTOM_PADDING),
                                                                       key="bot_pad")
            current_config['margin'] = st.number_input("Side Margin", 0, 100, get_state("margin", DEFAULT_MARGIN),
                                                       key="margin")

        with st.expander("Header & Footer Content", expanded=False):
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
            scanned_fonts = get_font_variants(font_temp_dir)
            if scanned_fonts.get("regular"):
                final_font_data = scanned_fonts
                current_config['font_source'] = "custom"
                current_config['font_sig'] = uploaded_font_zip.name
                st.success(f"Font loaded! Variants found: {[k for k, v in scanned_fonts.items() if v]}")
            else:
                st.warning("No font files found in ZIP.")
        except Exception as e:
            st.error(f"Failed to process font ZIP: {e}")

    if final_font_data is None: final_font_data = "Times-Roman"

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
                final_font_data,
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

        img = st.session_state.processor.render_page(st.session_state.current_page)
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
