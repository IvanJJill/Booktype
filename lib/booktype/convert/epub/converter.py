# This file is part of Booktype.
# Copyright (c) 2013 Borko Jandras <borko.jandras@sourcefabric.org>
#
# Booktype is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Booktype is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Booktype.  If not, see <http://www.gnu.org/licenses/>.

import os
import json
import logging
import urlparse
import ebooklib
import datetime

from copy import deepcopy
from lxml import etree

from django.template.loader import render_to_string, Template, Context

from booktype.apps.themes.utils import (
    read_theme_style, read_theme_assets, read_theme_asset_content)
from booktype.apps.convert.templatetags.convert_tags import (
    get_refines, get_metadata)
from ..base import BaseConverter
from ..utils.epub import parse_toc_nav

from .writer import Writer
from .writerplugin import WriterPlugin
from .cover import add_cover, COVER_FILE_NAME
from booktype.apps.convert import plugin

from .constants import (
    IMAGES_DIR, STYLES_DIR, FONTS_DIR,
    DOCUMENTS_DIR, DEFAULT_LANG
)

logger = logging.getLogger("booktype.convert.epub")
TOC_TITLE = 'toc'


class EpubConverter(BaseConverter):
    name = 'epub'

    DEFAULT_STYLE = 'style1'
    css_dir = os.path.join(os.path.dirname(__file__), 'styles/')

    def __init__(self, *args, **kwargs):
        super(EpubConverter, self).__init__(*args, **kwargs)

        self.theme_name = ''
        self.theme_plugin = None

    def _init_theme_plugin(self):
        if 'theme' in self.config:
            self.theme_name = self.config['theme'].get('id', '')
            tp = plugin.load_theme_plugin(self.name, self.theme_name)
            if tp:
                self.theme_plugin = tp(self)
        else:
            self.theme_name = None

    def pre_convert(self, original_book, book):
        if self.theme_plugin:
            try:
                self.theme_plugin.pre_convert(original_book, book)
            except NotImplementedError:
                pass

    def post_convert(self, original_book, book, output_path):
        if self.theme_plugin:
            try:
                self.theme_plugin.post_convert(original_book, book, output_path)
            except NotImplementedError:
                pass

    def convert(self, original_book, output_path):
        convert_start = datetime.datetime.now()

        logger.debug('[EPUB] EpubConverter.convert')

        self._init_theme_plugin()

        epub_book = ebooklib.epub.EpubBook()
        epub_book.FOLDER_NAME = 'OEBPS'

        self.pre_convert(original_book, epub_book)

        epub_book.uid = original_book.uid
        epub_book.title = original_book.title

        # we should define better uri for this
        epub_book.add_prefix('bkterms', 'http://booktype.org/')

        epub_book.metadata = deepcopy(original_book.metadata)
        epub_book.toc = []

        self.direction = self._get_dir(epub_book)

        logger.debug('[EPUB] Edit metadata')
        self._edit_metadata(epub_book)

        logger.debug('[EPUB] Copy items')
        self._copy_items(epub_book, original_book)

        logger.debug('[EPUB] Make navigation')
        self._make_nav(epub_book, original_book)

        logger.debug('[EPUB] Add cover')
        self._add_cover(epub_book)

        logger.debug('[EPUB] Writer plugin')

        writer_plugin = WriterPlugin()
        writer_plugin.options['style'] = self.config.get(
            'style', self.DEFAULT_STYLE)

        writer_plugin.options['lang'] = self.config.get('lang', DEFAULT_LANG)
        writer_plugin.options['preview'] = self.config.get('preview', True)

        logger.debug('[EPUB] Adding default and custom css')
        book_css = self._add_css_styles(epub_book)
        writer_plugin.options['css'] = book_css

        if self.theme_name:
            self._add_theme_assets(epub_book)

        self.post_convert(original_book, epub_book, output_path)

        writer_options = {
            'plugins': [writer_plugin, ]
        }

        logger.debug('[EPUB] Writer')
        epub_writer = Writer(
            output_path, epub_book, options=writer_options)

        logger.debug('[EPUB] Process')
        epub_writer.process()

        logger.debug('[EPUB] Write')
        epub_writer.write()

        logger.debug('[END] EPUBConverter.convert')

        convert_end = datetime.datetime.now()
        logger.info('Conversion lasted %s.', convert_end - convert_start)

        return {"size": os.path.getsize(output_path)}

    def _get_dir(self, epub_book):
        m = epub_book.metadata[ebooklib.epub.NAMESPACES["OPF"]]
        def _check(x):
            return x[1] and x[1].get('property', '') == 'bkterms:dir'

        values = filter(_check, m[None])
        if len(values) > 0 and len(values[0]) > 0:
            return values[0][0].lower()

        return 'ltr'

    def _edit_metadata(self, epub_book):
        """
        Modifies original metadata.
        """

        # delete existing 'modified' tag
        m = epub_book.metadata[ebooklib.epub.NAMESPACES["OPF"]]
        m[None] = filter(lambda (_, x): not (isinstance(x, dict) and x.get("property") == "dcterms:modified"), m[None])  # noqa

        # we also need to remove the `additional metadata` which here is just garbage
        m[None] = filter(lambda (_, x): not (isinstance(x, dict) and x.get("property").startswith("add_meta_terms:")), m[None]) # noqa

        # NOTE: probably going to extend this function in future

    def _make_nav(self, epub_book, original_book):
        """
        Creates navigational stuff (guide, ncx, nav) by copying the original.
        """

        # maps TOC items to sections and links
        self._num_of_text = 0

        def mapper(toc_item):
            add_to_guide = True

            if isinstance(toc_item[1], list):
                section_title, chapters = toc_item

                section = ebooklib.epub.Section(section_title)
                links = map(mapper, chapters)

                return (section, links)
            else:
                chapter_title, chapter_href = toc_item

                chapter_href = "{}/{}".format(DOCUMENTS_DIR, chapter_href)
                chapter_path = urlparse.urlparse(chapter_href).path

                book_item = self.items_by_path[chapter_path]
                book_item.title = chapter_title

                if self._num_of_text > 0:
                    add_to_guide = False

                self._num_of_text += 1

                if add_to_guide:
                    epub_book.guide.append({
                        'type': 'text',
                        'href': chapter_href,
                        'title': chapter_title,
                    })

                return ebooklib.epub.Link(
                    href=chapter_href, title=chapter_title, uid=book_item.id)

        # filters-out empty sections
        def _empty_sec(item):
            if isinstance(item, tuple) and len(item[1]) == 0:
                return False
            else:
                return True

        # filters-out existing cover
        def _skip_cover(item):
            if type(item[1]) in (str, unicode):
                if os.path.basename(item[1]) == COVER_FILE_NAME:
                    return False
            return True

        toc = filter(_skip_cover, parse_toc_nav(original_book))
        toc = map(mapper, toc)
        toc = filter(_empty_sec, toc)

        epub_book.toc = toc

    def _copy_items(self, epub_book, original_book):
        """
        Populates the book by copying items from the original book
        """
        self.items_by_path = {}

        for orig_item in original_book.items:
            item = deepcopy(orig_item)
            item_type = item.get_type()
            file_name = os.path.basename(item.file_name)

            # do not copy cover
            if self._is_cover_item(item):
                continue

            if item_type == ebooklib.ITEM_IMAGE:
                item.file_name = '{}/{}'.format(IMAGES_DIR, file_name)

            elif item_type == ebooklib.ITEM_STYLE:
                item.file_name = '{}/{}'.format(STYLES_DIR, file_name)

            elif item_type == ebooklib.ITEM_DOCUMENT:
                item.file_name = '{}/{}'.format(DOCUMENTS_DIR, file_name)
                if isinstance(item, ebooklib.epub.EpubNav):
                    epub_book.spine.insert(0, item)
                    epub_book.guide.insert(0, {
                        'type': 'toc',
                        'href': item.file_name,
                        'title': self.config.get('toc_title', TOC_TITLE)
                    })
                else:
                    epub_book.spine.append(item)

                    if self.theme_plugin:
                        try:
                            content = ebooklib.utils.parse_html_string(item.content)
                            cnt = self.theme_plugin.fix_content(content)
                            item.content = etree.tostring(cnt, method='html', encoding='utf-8', pretty_print=True)
                        except NotImplementedError:
                            pass

            if isinstance(item, ebooklib.epub.EpubNcx):
                item = ebooklib.epub.EpubNcx()

            epub_book.add_item(item)
            self.items_by_path[item.file_name] = item

    def _add_cover(self, epub_book):
        """
        Adds cover image if present in config to the resulting EPUB
        """

        if 'cover_image' in self.config.keys():
            cover_asset = self.get_asset(self.config['cover_image'])
            add_cover(
                epub_book, cover_asset, self.config.get('lang', DEFAULT_LANG))

    def _add_css_styles(self, epub_book):
        """
        Adds default css styles and custom css text if exists in config
        """

        book_css = []

        try:
            content = render_to_string('themes/style_{}.css'.format(self.name),
                {'dir': self.direction})

            item = ebooklib.epub.EpubItem(
                uid='default.css',
                content=content,
                file_name='{}/{}'.format(STYLES_DIR, 'default.css'),
                media_type='text/css'
            )

            epub_book.add_item(item)
            book_css.append('default.css')
        except:
            pass

        if self.theme_name:
            content = read_theme_style(self.theme_name, self.name)

            if self.theme_name == 'custom':
                try:
                    data = json.loads(self.config['theme']['custom'].encode('utf8'))

                    tmpl = Template(content)
                    ctx = Context(data)
                    content = tmpl.render(ctx)
                except:
                    logger.exception("Fails with custom theme.")


            item = ebooklib.epub.EpubItem(
                uid='theme.css',
                content=content,
                file_name='{}/{}'.format(STYLES_DIR, 'theme.css'),
                media_type='text/css'
            )

            epub_book.add_item(item)
            book_css.append('theme.css')

        # time to add custom css :)
        if 'css_text' in self.config.keys():
            item = ebooklib.epub.EpubItem(
                uid='custom.css',
                content=self.config.get('css_text'),
                file_name='{}/{}'.format(STYLES_DIR, 'custom.css'),
                media_type='text/css'
            )

            epub_book.add_item(item)
            book_css.append('custom.css')

        return book_css

    def _add_theme_assets(self, epub_book):
        import uuid
        assets = read_theme_assets(self.theme_name, self.name)

        for asset_type, asset_list in assets.iteritems():
            if asset_type == 'images':
                for image_name in asset_list:
                    name = os.path.basename(image_name)
                    content = read_theme_asset_content(self.theme_name, image_name)

                    if content:
                        image = ebooklib.epub.EpubImage()
                        image.file_name = "{}/{}".format(IMAGES_DIR, name)
                        image.id = 'theme_image_%s' % uuid.uuid4().hex[:5]
                        image.set_content(content)

                        epub_book.add_item(image)
            elif asset_type == 'fonts':
                for font_name in asset_list:
                    name = os.path.basename(font_name)
                    content = read_theme_asset_content(self.theme_name, font_name)

                    if content:
                        image = ebooklib.epub.EpubItem()
                        image.file_name = "{}/{}".format(FONTS_DIR, name)
                        image.set_content(content)

                        epub_book.add_item(image)

    def _get_data(self, book):
        """Returns default data for the front and end matter templates.

        It mainly has default metadata from the book.

        :Returns:
          - Dictionary with default data for the templates
        """

        return {
            "title": get_refines(book.metadata, 'title-type', 'main'),
            "subtitle": get_refines(book.metadata, 'title-type', 'subtitle'),
            "shorttitle": get_refines(book.metadata, 'title-type', 'short'),
            "author": get_refines(book.metadata, 'role', 'aut'),

            "publisher": get_metadata(book.metadata, 'publisher'),
            "isbn": get_metadata(book.metadata, 'identifier'),
            "language": get_metadata(book.metadata, 'language'),

            "metadata": book.metadata,
        }


    def _is_cover_item(self, item):
        """
        Determines if an given item is cover type
        """

        file_name = os.path.basename(item.file_name)

        cover_types = [
            ebooklib.epub.EpubCover,
            ebooklib.epub.EpubCoverHtml
        ]

        return (type(item) in cover_types or file_name == 'cover.xhtml')
