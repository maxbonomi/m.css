#!/usr/bin/env python3

#
#   This file is part of m.css.
#
#   Copyright © 2017, 2018 Vladimír Vondruš <mosra@centrum.cz>
#
#   Permission is hereby granted, free of charge, to any person obtaining a
#   copy of this software and associated documentation files (the "Software"),
#   to deal in the Software without restriction, including without limitation
#   the rights to use, copy, modify, merge, publish, distribute, sublicense,
#   and/or sell copies of the Software, and to permit persons to whom the
#   Software is furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included
#   in all copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
#   THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
#   FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
#   DEALINGS IN THE SOFTWARE.
#

import argparse
import copy
import docutils
import docutils.transforms
import http.server
import inspect
import jinja2
import logging
import multiprocessing
import os
import shutil
import sys
import time
import urllib

from importlib.machinery import SourceFileLoader
from types import SimpleNamespace as Empty

sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), '../plugins'))
import m.htmlsanity
import m.code
import m.components
import m.dot
import m.images
import m.math
import m.link
import m.plots
import latex2svgextra # TODO: make this part of m.math again

from docutils.parsers import rst
from docutils.parsers.rst import directives
from docutils.parsers.rst.roles import set_classes
from docutils import nodes

class PresenterDirective(rst.Directive):
    has_content = True
    optional_arguments = 0

    def run(self):
        set_classes(self.options)

        text = '\n'.join(self.content)
        topic_node = nodes.topic(text, **self.options)
        topic_node['classes'] += ['m-presenter']

        self.state.nested_parse(self.content, self.content_offset,
                                topic_node)
        return [topic_node]

default_templates = os.path.dirname(os.path.realpath(__file__))
default_config = {
    # TODO: mimic pelican with these?
    'DEFAULT_LANG': 'en',
    'M_HTMLSANITY_SMART_QUOTES': True,
    #'M_MATH_CACHE_FILE': 'm.math.cache', # TODO: make this optional only if plugin exists
    #'FORMATTED_FIELDS': d,
    'M_EXTRA_FILES': [
        '../css/m-grid.css',
        '../css/m-components.css',
        '../css/m-presentation.css',
        '../css/m-theme-dark.css',
        '../css/pygments-dark.css',
        '../css/pygments-console.css'],
    'M_CSS_FILES': [
        'https://fonts.googleapis.com/css?family=Source+Code+Pro:400,400i,600%7CSource+Sans+Pro:400,400i,600,600i',
        '../css/m-dark-presentation.css']
    #'M_EXTRA_FILES': [
        #'../css/m-grid.css',
        #'../css/m-components.css',
        #'../css/m-presentation.css',
        #'../css/m-theme-light.css',
        #'../css/pygments-console.css'],
    #'M_CSS_FILES': [
        #'https://fonts.googleapis.com/css?family=Source+Code+Pro:400,400i,600%7CSource+Sans+Pro:400,400i,600,600i',
        #'../css/m-light-presentation.css']
}

class SectionMetadata(docutils.transforms.Transform):
    # Max Docutils priority is 990, be sure that this is applied at the very
    # last
    default_priority = 991

    def __init__(self, document, startnode):
        docutils.transforms.Transform.__init__(self, document, startnode=startnode)

    def apply(self):
        pyphen_for_lang = {}

        # Go through all section-specific metadata and use them
        for section in self.document.traverse(nodes.section):
            field_list = section.first_child_matching_class(nodes.field_list)
            if not field_list: continue

            for field in section[field_list]:
                if field[0][0] == 'background_color':
                    if 'style' not in section: section['style'] = []
                    section['style'] += ['background-color: {};'.format(field[1][0].astext())]
                    section['classes'] += ['m-presentation-background']

            section.remove(section[field_list]) # TODO: remove

class PresentationHtmlTranslator(m.htmlsanity._SaneFieldBodyTranslator):
    # Copied from .m.htmlsanity, plus making it possible to add arbitrary
    # styles to sections
    def visit_section(self, node):
        atts = {}
        if 'style' in node: atts['style'] = node['style']
        self.section_level += 1
        self.body.append(
            self.starttag(node, 'section', **atts))

    def depart_section(self, node):
        self.section_level -= 1
        self.body.append('</section>\n')

    # Hide fields from the output
    #def visit_field_body(self, node):
        #pass
    #def depart_field_body(self, node):
        #pass

class PresentationWriter(m.htmlsanity.SaneHtmlWriter):
    def __init__(self):
        m.htmlsanity.SaneHtmlWriter.__init__(self)

        self.translator_class = PresentationHtmlTranslator

    def get_transforms(self):
        return m.htmlsanity.SaneHtmlWriter.get_transforms(self) + [SectionMetadata]

class Presenter:
    def __init__(self, templates):
        # TODO: ugh
        m.code.register()
        m.components.register()
        m.dot.register()
        m.images.register()
        m.link.register()
        m.math.register()
        m.plots.register()

        rst.directives.register_directive('presenter', PresenterDirective)

        extra_params = {'initial_header_level': '2',
                        'syntax_highlight': 'short',
                        'input_encoding': 'utf-8',
                        'language_code': 'en',
                        # TODO: five only when autoreload, should exit with
                        # failure in batch mode -- look at the Pelican PR
                        'exit_status_level': 5,
                        'sectsubtitle_xform': True,
                        'embed_stylesheet': False}
        self.pub = docutils.core.Publisher(
            #writer=docutils.writers.html4css1.Writer(),
            writer=PresentationWriter(),
            source_class=docutils.io.StringInput,
            destination_class=docutils.io.StringOutput)
        self.pub.set_components('standalone', 'restructuredtext', 'html')
        #self.pub.writer.translator_class = m.htmlsanity.SaneHtmlTranslator
        self.pub.process_programmatic_settings(None, extra_params, None)

        self.env = jinja2.Environment(loader=jinja2.FileSystemLoader(templates),
            trim_blocks=True, lstrip_blocks=True, enable_async=True)
        def basename_or_url(path):
            if urllib.parse.urlparse(path).netloc: return path
            return os.path.basename(path)
        self.env.filters['basename_or_url'] = basename_or_url

    # Returns list of files to watch for changes, input is always the first of
    # them
    def present(self, input, output, config, presenter_view):
        basedir = os.path.dirname(input)

        # TODO ugh
        m.htmlsanity.configure(config)
        m.dot.configure(config)
        m.images.configure(config)
        m.plots.configure(config)

        # TODO: configurable
        math_cache = os.path.join(basedir, "m.math.cache")
        if os.path.exists(math_cache):
            latex2svgextra.unpickle_cache(math_cache)
        else:
            latex2svgextra.unpickle_cache(None)

        logging.debug("reading {}".format(input))
        with open(input, 'r') as f: source = f.read()

        self.pub.set_source(source=source, source_path=input)
        self.pub.publish(enable_exit_status=True)

        metadata = {}
        for docinfo in self.pub.document.traverse(docutils.nodes.docinfo):
            for element in docinfo.children:
                # Custom named field
                if element.tagname == 'field':
                    name_elem, body_elem = element.children
                    name = name_elem.astext()
                    #if name in formatted_fields:
                        #value = render_node_to_html(
                            #document, body_elem,
                            #self.field_body_translator_class)
                    #else:
                    value = body_elem.astext()
                # Author list
                elif element.tagname == 'authors':
                    name = element.tagname
                    value = [element.astext() for element in element.children]
                # Standard field
                else:
                    name = element.tagname
                    value = element.astext()
                name = name.lower()

                metadata[name] = value

        # Add extra bundled files
        extra_files = []
        if 'css' in metadata:
            extra_files += [i.strip() for i in metadata['css'].strip().split('\n')]
        if 'js' in metadata:
            extra_files += [i.strip() for i in metadata['js'].strip().split('\n')]
        if 'bundle' in metadata:
            extra_files += [i.strip() for i in metadata['bundle'].strip().split('\n')]
            del metadata['bundle'] # not need to expose this to the template
        if 'cover' in metadata:
            extra_files += [metadata['cover']]

        # Add images
        for image in self.pub.document.traverse(docutils.nodes.image):
            extra_files += [image['uri']]

        # Set up the page structure for the template
        page = Empty()
        page.title = self.pub.writer.parts.get('title')
        page.subtitle = self.pub.writer.parts.get('subtitle')
        page.content = self.pub.writer.parts.get('body')
        for key, value in metadata.items(): setattr(page, key, value)

        # Write normal view
        template = self.env.get_template('template.html')
        rendered = template.render(page=page, PRESENTER_VIEW=False, **config)

        if not os.path.exists(output): os.makedirs(output)
        output_file = os.path.join(output, 'index.html')
        logging.debug("writing {}".format(output_file))
        with open(output_file, 'w') as f: f.write(rendered)

        # Write presenter view, if requested
        if presenter_view:
            rendered = template.render(page=page, PRESENTER_VIEW=True, **config)

            output_file = os.path.join(output, presenter_view)
            logging.debug("writing {}".format(output_file))
            with open(output_file, 'w') as f: f.write(rendered)

        # Copy all referenced files
        files_to_watch = []
        for i in config['M_EXTRA_FILES'] + config['M_CSS_FILES'] + ['presentation.js'] + extra_files:
            # Skip absolute URLs
            if urllib.parse.urlparse(i).netloc: continue

            # If file is found relative to the input file, use that. Also add
            # it to the watched list
            if os.path.exists(os.path.join(basedir, i)):
                i = os.path.join(basedir, i)
                files_to_watch += [i]

            # Otherwise use path relative to script directory
            else:
                i = os.path.join(os.path.dirname(os.path.realpath(__file__)), i)

            logging.debug("copying {} to {}".format(i, output))
            shutil.copy(i, os.path.join(output, os.path.basename(i)))

        # TODO pickle/unpickle on init only
        latex2svgextra.pickle_cache(math_cache)

        return [input] + files_to_watch

def file_watcher(paths):
    # TODO: test this
    logging.info("watching {} paths".format(len(paths)))
    last_mtime = [0]*len(paths)
    modified = None
    while not modified:
        for i, path in enumerate(paths):
            mtime = os.stat(path).st_mtime
            # Avoid reporting the file has modified right after start
            if not last_mtime[i]:
                last_mtime[i] = mtime
            elif mtime > last_mtime[i]:
                last_mtime[i] = mtime
                modified = path
        yield modified

# Paths[0] has to be the input file
def autoreload(presenter, paths, output, config, presenter_view):
    input = paths[0]
    while True:
        for modified in file_watcher(paths):
            if modified:
                logging.info("modified {}, updating".format(os.path.basename(modified)))
                paths = presenter.present(input, output, config, presenter_view)
            else:
                time.sleep(1)

def listen(output, port):
    os.chdir(output)
    # TODO: too specific, move this away
    http.server.SimpleHTTPRequestHandler.extensions_map['.wasm'] = 'application/wasm'
    httpd = http.server.HTTPServer(('', port), http.server.SimpleHTTPRequestHandler)
    httpd.serve_forever()

if __name__ == '__main__': # pragma: no cover
    parser = argparse.ArgumentParser()
    parser.add_argument('input', help="input reST file with the presentation")
    parser.add_argument('-o', '--output', help="output directory (relative to input)", default='output/')
    parser.add_argument('--presenter', nargs='?', const='presenter.html', default=None, help="generate a presenter view")
    parser.add_argument('--templates', help="template directory", default=default_templates)
    parser.add_argument('-c', '--config', help='config file')
    parser.add_argument('--debug', help="verbose debug output", action='store_true')
    parser.add_argument('-r', '--autoreload', help="reload on input file change", action='store_true')
    parser.add_argument('-l', '--listen', help="serve the output via a webserver", action='store_true')
    parser.add_argument('-p', '--port', help="", default='8000', type=int)
    args = parser.parse_args()

    #os.chdir(os.path.dirname(args.input))

    # Load configuration from a file or use the default
    if args.config:
        name, _ = os.path.splitext(os.path.basename(args.config))
        module = SourceFileLoader(name, args.config).load_module()
        config = copy.deepcopy(default_config)
        if module is not None:
            # hum? TODO what is this
            config.update((k, v) for k, v in inspect.getmembers(module) if k.isupper())
    else:
        config = copy.deepcopy(default_config)

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    output = os.path.join(os.path.dirname(args.input), args.output)

    presenter = Presenter(args.templates)
    paths = presenter.present(args.input, output, config, args.presenter)

    if args.autoreload and args.listen:
        logging.info("serving on http://localhost:{} with autoreload ...".format(args.port))
        queue = multiprocessing.Queue()
        reloader = multiprocessing.Process(target=autoreload, args=(presenter, paths, output, config, args.presenter))
        server = multiprocessing.Process(target=listen, args=(output, args.port))
        reloader.start()
        server.start()
        e = queue.get()
        reloader.terminate()
        server.terminate()
        logging.critical(e)
    elif args.autoreload:
        logging.info("started autoreload...")
        autoreload(presenter, paths, output, config, args.presenter)
    elif args.listen:
        logging.info("serving on http://localhost:{} ...".format(args.port))
        listen(output, args.port)
